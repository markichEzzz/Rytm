from django.contrib.auth import get_user_model
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from io import StringIO
from rest_framework.test import APITestCase
from .models import TourRoute, TourRouteStop
from .admin import TourRouteAdmin, RouteStopInline

class TourRouteTests(APITestCase):
    def route(self, title='Класика', kind='culture', duration=120, lon=0.01, published=True):
        route=TourRoute.objects.create(title=title,route_type=kind,estimated_duration_minutes=duration)
        TourRouteStop.objects.create(route=route,order=2,title='Друга зупинка',latitude=0,longitude=lon,address='Площа Ринок')
        TourRouteStop.objects.create(route=route,order=1,title='Оперний театр',latitude=0,longitude=0)
        if published: route.is_published=True;route.save()
        return route
    def test_draft_may_be_incomplete(self):
        self.assertFalse(TourRoute.objects.create().is_published)
    def test_creation_and_stop_order(self):
        route=self.route();self.assertEqual([s.order for s in route.stops.all()],[1,2])
    def test_publishing_needs_two_stops(self):
        route=TourRoute(title='Test',route_type='culture',estimated_duration_minutes=20,is_published=True)
        with self.assertRaises(ValidationError): route.save()
        route.is_published=False;route.save()
        TourRouteStop.objects.create(route=route,order=1,title='One',latitude=49,longitude=24)
        route.is_published=True
        with self.assertRaises(ValidationError):route.save()
    def test_publication_requires_metadata(self):
        route=self.route(published=False)
        for field,value in [('title',' '),('estimated_duration_minutes',None),('route_type','invalid')]:
            original=getattr(route,field);setattr(route,field,value);route.is_published=True
            with self.assertRaises(ValidationError):route.save()
            setattr(route,field,original)
    def test_unpublished_hidden(self):
        route=self.route(published=False)
        self.assertEqual(self.client.get('/api/routes/').data,[])
        self.assertEqual(self.client.get(f'/api/routes/{route.pk}/').status_code,404)
    def test_list_and_detail_contract(self):
        route=self.route()
        listed=self.client.get('/api/routes/').data
        self.assertEqual(len(listed),1);self.assertEqual(listed[0]['stop_count'],2)
        self.assertEqual(listed[0]['distance_kind'],'straight_line')
        self.assertNotIn('stops',listed[0]);self.assertNotIn('demo_key',listed[0])
        detail=self.client.get(f'/api/routes/{route.pk}/').data
        self.assertEqual([s['order'] for s in detail['stops']],[1,2])
        self.assertEqual(detail['stops'][0]['title'],'Оперний театр')
    def test_type_filter(self):
        a=self.route();self.route('Парк','nature')
        self.assertEqual([x['id'] for x in self.client.get('/api/routes/?route_type=culture').data],[a.pk])
    def test_duration_filters(self):
        a=self.route(duration=100);self.route('Long',duration=240)
        self.assertEqual([x['id'] for x in self.client.get('/api/routes/?duration_min=60&duration_max=120').data],[a.pk])
    def test_distance_filters_in_kilometers(self):
        a=self.route(lon=.01);b=self.route('Long',lon=1)
        self.assertEqual([x['id'] for x in self.client.get('/api/routes/?distance_min=1&distance_max=2').data],[a.pk])
        self.assertEqual([x['id'] for x in self.client.get('/api/routes/?distance_min=100').data],[b.pk])
    def test_difficulty_and_combined_filters(self):
        a=self.route(duration=100);b=self.route('Other','nature',duration=100)
        b.difficulty='hard';b.save()
        data=self.client.get('/api/routes/?route_type=culture&duration_max=120&distance_max=2&difficulty=easy').data
        self.assertEqual([x['id'] for x in data],[a.pk])
    def test_search_title_description_stop_and_address(self):
        route=self.route(title='Маршрут класика');route.description='Зелені двори';route.save()
        for q in ['класика','Зелені','Оперний','Ринок']:
            with self.subTest(q=q):self.assertEqual(len(self.client.get('/api/routes/',{'q':q}).data),1)
        self.assertEqual(self.client.get('/api/routes/?q=missing').data,[])
    def test_search_does_not_duplicate_routes(self):
        self.route();self.assertEqual(len(self.client.get('/api/routes/?q=зупинка').data),1)
    def test_geodesic_distance(self):
        route=self.route(lon=1)
        self.assertAlmostEqual(route.distance_meters,111319.5,delta=1)
    def test_sum_of_consecutive_legs_changes_with_order(self):
        route=self.route(lon=1,published=False)
        TourRouteStop.objects.create(route=route,order=3,title='Third',latitude=0,longitude=2)
        self.assertAlmostEqual(route.distance_meters,222639,delta=2)
        TourRouteStop.objects.filter(route=route,order=3).update(longitude=-1)
        route=TourRoute.objects.get(pk=route.pk)
        self.assertAlmostEqual(route.distance_meters,333958.5,delta=2)
    def test_invalid_coordinates_rejected(self):
        route=TourRoute.objects.create()
        for lat,lon in [(91,24),(49,181),(float('nan'),24),(49,float('inf'))]:
            with self.subTest(lat=lat,lon=lon),self.assertRaises(ValidationError):
                TourRouteStop.objects.create(route=route,order=1,title='Bad',latitude=lat,longitude=lon)
    def test_db_coordinate_constraint(self):
        route=TourRoute.objects.create()
        with self.assertRaises(IntegrityError),transaction.atomic():
            TourRouteStop.objects.bulk_create([TourRouteStop(route=route,order=1,title='Bad',latitude=100,longitude=24)])
    def test_unique_stop_order(self):
        route=self.route()
        with self.assertRaises(IntegrityError),transaction.atomic():
            TourRouteStop.objects.create(route=route,order=1,title='Duplicate',latitude=49,longitude=24)
            # Deferrable constraint is checked at the transaction boundary in production.
            from django.db import connection
            with connection.cursor() as cursor:cursor.execute('SET CONSTRAINTS tour_unique_stop_order IMMEDIATE')
    def test_broken_unsafe_bulk_publication_stays_private(self):
        route=TourRoute.objects.create(title='Broken',estimated_duration_minutes=20)
        TourRoute.objects.filter(pk=route.pk).update(is_published=True)
        self.assertEqual(self.client.get('/api/routes/').data,[])
        self.assertEqual(self.client.get(f'/api/routes/{route.pk}/').status_code,404)
    def test_cannot_delete_required_published_stop(self):
        route=self.route()
        with self.assertRaises(ValidationError):route.stops.first().delete()
    def test_invalid_filters(self):
        for params in [{'route_type':'bad'},{'difficulty':'bad'},{'duration_max':'text'},{'duration_max':'9'*500},{'distance_max':'nan'}, {'distance_min':-1},{'duration_min':120,'duration_max':20}]:
            with self.subTest(params=params):self.assertEqual(self.client.get('/api/routes/',params).status_code,400)
    def test_public_api_readonly(self):
        self.assertEqual(self.client.post('/api/routes/',{'title':'Hack'}).status_code,405)
    def test_demo_command_idempotent_and_draft_only(self):
        call_command('seed_demo_routes',stdout=StringIO());count=TourRouteStop.objects.count()
        route=TourRoute.objects.first();route.title='Curated';route.save()
        call_command('seed_demo_routes',stdout=StringIO())
        self.assertEqual(TourRoute.objects.count(),4);self.assertEqual(TourRouteStop.objects.count(),count)
        route.refresh_from_db();self.assertEqual(route.title,'Curated')
        self.assertFalse(TourRoute.objects.filter(is_published=True).exists())
    def admin_payload(self, route=None, orders=(1,2), delete=False):
        data={'title':'Admin route','short_description':'Neutral','description':'Description','route_type':'culture','estimated_duration_minutes':'90','difficulty':'easy','is_published':'on',
            'stops-TOTAL_FORMS':'2','stops-INITIAL_FORMS':'2' if route else '0','stops-MIN_NUM_FORMS':'0','stops-MAX_NUM_FORMS':'1000'}
        stops=list(route.stops.all()) if route else []
        for i,order in enumerate(orders):
            prefix=f'stops-{i}-'
            data.update({prefix+'order':str(order),prefix+'title':f'Stop {i}',prefix+'latitude':'49.84',prefix+'longitude':str(24.03+i*.01),prefix+'description':'',prefix+'address':'',prefix+'image_url':'',prefix+'recommended_stop_minutes':'0'})
            if route:data[prefix+'id']=str(stops[i].pk);data[prefix+'route']=str(route.pk)
        if delete:data['stops-1-DELETE']='on'
        return data
    def login_admin(self):
        staff=get_user_model().objects.create_superuser('admin','admin@example.com','test')
        self.client.force_login(staff)
    def test_admin_configuration(self):
        self.assertIsInstance(admin.site._registry[TourRoute],TourRouteAdmin)
        self.assertIn(RouteStopInline,admin.site._registry[TourRoute].inlines)
    def test_admin_can_create_and_publish_with_inlines(self):
        self.login_admin();response=self.client.post('/admin/tour_routes/tourroute/add/',self.admin_payload())
        self.assertEqual(response.status_code,302,str(response.content[:1000]))
        route=TourRoute.objects.get();self.assertTrue(route.is_published);self.assertEqual(route.stops.count(),2)
    def test_admin_cannot_remove_required_stop_while_published(self):
        route=self.route();self.login_admin()
        response=self.client.post(f'/admin/tour_routes/tourroute/{route.pk}/change/',self.admin_payload(route,delete=True))
        self.assertEqual(response.status_code,200);route.refresh_from_db()
        self.assertTrue(route.is_published);self.assertEqual(route.stops.count(),2)
    def test_admin_reorders_stops(self):
        route=self.route();self.login_admin();first=route.stops.first().pk
        response=self.client.post(f'/admin/tour_routes/tourroute/{route.pk}/change/',self.admin_payload(route,orders=(2,1)))
        self.assertEqual(response.status_code,302)
        self.assertEqual(route.stops.last().pk,first)
