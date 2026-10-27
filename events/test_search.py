from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from django.contrib.gis.geos import Point
from django.utils import timezone
from rest_framework.test import APITestCase
from .models import Event


class PublicSearchTests(APITestCase):
    def setUp(self):
        self.day=(timezone.now()+timedelta(days=7)).astimezone(ZoneInfo('Europe/Kyiv')).date()
        start=datetime.combine(self.day,datetime.min.time(),tzinfo=ZoneInfo('Europe/Kyiv'))+timedelta(hours=19)
        for title,category,status,description in [('ДЖАЗ у Львові','concert','free','Місце: Jazz Club.'),
            ('Кінопоказ','performance','unknown','Документальний фільм'),('Майстер-клас','education','paid','Кераміка')]:
            Event.objects.create(title=title,category=category,price_status=status,price=100 if status=='paid' else None,
                description=description,start_time=start,location=Point(24.03,49.84),address='вул. Лесі Українки, 1',status='approved')

    def titles(self,**params):
        response=self.client.get('/api/events/',params)
        self.assertEqual(response.status_code,200,response.data)
        return [event['title'] for event in response.data]

    def test_ukrainian_case_insensitive_and_public_fields_only(self):
        self.assertEqual(self.titles(q='джаз'),['ДЖАЗ у Львові'])
        self.assertEqual(self.titles(q='Jazz Club'),['ДЖАЗ у Львові'])
        self.assertEqual(len(self.titles(q='Лесі Українки')),3)
        Event.objects.filter(title='Кінопоказ').update(source_url='https://example.com/private-source')
        self.assertEqual(self.titles(q='private-source'),[])

    def test_combined_category_dates_and_price(self):
        self.assertEqual(self.titles(q='джаз',category='concert',date_from=self.day,date_to=self.day,price_status='free'),['ДЖАЗ у Львові'])
        self.assertEqual(self.titles(date_from=self.day+timedelta(days=1)),[])
        self.assertEqual(self.titles(date_to=self.day-timedelta(days=1)),[])
        self.assertEqual(len(self.titles(date_from=self.day,date_to=self.day)),3)

    def test_unknown_is_not_free(self):
        self.assertEqual(self.titles(price_status='unknown'),['Кінопоказ'])
        self.assertEqual(self.titles(price_status='free'),['ДЖАЗ у Львові'])
        self.assertEqual(self.titles(price_status='paid'),['Майстер-клас'])

    def test_kyiv_calendar_boundary_is_inclusive(self):
        Event.objects.filter(title='Кінопоказ').update(start_time=datetime.combine(self.day,datetime.min.time(),tzinfo=ZoneInfo('Europe/Kyiv')))
        self.assertIn('Кінопоказ',self.titles(date_from=self.day,date_to=self.day))

    def test_invalid_filters_return_400(self):
        for params in ({'date_from':'oops'},{'date_to':'9999-12-31'},{'date_from':self.day+timedelta(days=1),'date_to':self.day},{'price_status':'zero'}):
            self.assertEqual(self.client.get('/api/events/',params).status_code,400)

    def test_visibility_preserved(self):
        Event.objects.filter(title='Кінопоказ').update(status='pending')
        Event.objects.filter(title='Майстер-клас').update(start_time=timezone.now()-timedelta(days=1))
        self.assertEqual(self.titles(),['ДЖАЗ у Львові'])
