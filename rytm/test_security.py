"""Local security regressions: exercise actual API and Django admin dispatch."""
from datetime import timedelta
from io import BytesIO
from unittest.mock import patch
from PIL import Image
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.gis.geos import Point
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken
from events.models import Event
from tour_routes.models import TourRoute


class SecurityTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = get_user_model().objects.create_user(username='security-owner', password='LocalSecurity!9462')
        self.other = get_user_model().objects.create_user(username='security-other')
        self.event = Event.objects.create(title='Existing event', description='Original', category='party',
            start_time=timezone.now()+timedelta(days=2), location=Point(24,49.8), address='Lviv',
            submitted_by=self.other, status='approved')
        no_network = patch('requests.sessions.Session.request', side_effect=AssertionError('Network forbidden'))
        no_network.start(); self.addCleanup(no_network.stop)

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION='Bearer '+str(RefreshToken.for_user(self.user).access_token))

    def event_payload(self, **values):
        stream=BytesIO(); Image.new('RGB',(2,2)).save(stream,format='JPEG')
        return {'title':'Test event', 'description':'A local test description', 'category':'party',
            'start_time':(timezone.now()+timedelta(days=3)).isoformat(), 'location':'POINT(24 49.8)',
            'address':'Lviv centre','photo':SimpleUploadedFile('event.jpg',stream.getvalue(),content_type='image/jpeg'), **values}

    def test_anonymous_writes_denied(self):
        for path, method in [('/api/events/','post'),('/api/routes/submit/','post'),
                ('/api/me/profile/','patch'),('/api/me/account/','delete'),
                (f'/api/events/{self.event.pk}/report/','post'),(f'/api/events/{self.event.pk}/interaction/','post')]:
            with self.subTest(path=path): self.assertEqual(getattr(self.client,method)(path,{},format='json').status_code,401)

    def test_wrong_user_cannot_update_or_delete_event(self):
        self.authenticate()
        for method in ('patch','put','delete'):
            with self.subTest(method=method):
                self.assertEqual(getattr(self.client,method)(f'/api/events/{self.event.pk}/',{'title':'Hijacked'},format='json').status_code,405)
        self.event.refresh_from_db(); self.assertEqual(self.event.title,'Existing event')
        self.assertEqual(self.event.submitted_by,self.other)

    def test_profile_cannot_target_other_user_or_grant_staff(self):
        self.authenticate()
        for data in ({'id':self.other.pk,'display_name':'Hijacked'},{'is_staff':True},{'is_superuser':True}):
            self.assertEqual(self.client.patch('/api/me/profile/',data,format='json').status_code,400)
        self.other.profile.refresh_from_db(); self.assertEqual(self.other.profile.display_name,'')
        self.user.refresh_from_db(); self.assertFalse(self.user.is_staff or self.user.is_superuser)

    def test_malformed_and_expired_tokens_rejected(self):
        expired=RefreshToken.for_user(self.user).access_token
        expired.set_exp(from_time=timezone.now()-timedelta(hours=1),lifetime=timedelta(seconds=1))
        for token in ('invalid.token.value',str(expired),str(RefreshToken.for_user(self.user))):
            self.client.credentials(HTTP_AUTHORIZATION='Bearer '+token)
            self.assertEqual(self.client.get('/api/me/profile/').status_code,401)

    def test_event_upload_rejects_oversize_before_storage(self):
        self.authenticate()
        payload=self.event_payload()
        payload['photo']=SimpleUploadedFile('large.jpg',payload['photo'].read()+b'\0'*(10*1024*1024),content_type='image/jpeg')
        with patch('accounts.storage.AccountMediaStorage._save', return_value='security-test.jpg') as save:
            response=self.client.post('/api/events/',payload,format='multipart')
        self.assertEqual(response.status_code,400); self.assertIn('photo',response.data); save.assert_not_called()

    def test_event_upload_rejects_spoofed_mime_and_executable_extension(self):
        self.authenticate()
        for filename,mime in [('photo.exe','image/jpeg'),('photo.jpg','application/octet-stream')]:
            payload=self.event_payload(); payload['photo'].name=filename;payload['photo'].content_type=mime
            with self.subTest(filename=filename),patch('accounts.storage.AccountMediaStorage._save', return_value='security-test.jpg') as save:
                response=self.client.post('/api/events/',payload,format='multipart')
                self.assertEqual(response.status_code,400);self.assertIn('photo',response.data);save.assert_not_called()

    def test_event_upload_rejects_large_pixel_count(self):
        self.authenticate(); stream=BytesIO();Image.new('RGB',(4097,4097)).save(stream,format='PNG')
        payload=self.event_payload(photo=SimpleUploadedFile('large.png',stream.getvalue(),content_type='image/png'))
        with patch('accounts.storage.AccountMediaStorage._save', return_value='security-test.jpg') as save:
            response=self.client.post('/api/events/',payload,format='multipart')
        self.assertEqual(response.status_code,400);self.assertIn('photo',response.data);save.assert_not_called()

    def test_event_description_is_bounded(self):
        self.authenticate()
        with patch('accounts.storage.AccountMediaStorage._save', return_value='security-test.jpg') as save:
            response=self.client.post('/api/events/',self.event_payload(description='x'*20001),format='multipart')
        self.assertEqual(response.status_code,400);self.assertIn('description',response.data);save.assert_not_called()

    def test_route_description_is_bounded(self):
        self.authenticate()
        payload={'title':'Route','description':'x'*20001,'route_type':'culture','difficulty':'easy',
            'estimated_duration_minutes':30,'stops':[{'order':i,'title':'Stop','latitude':49.8,'longitude':24} for i in (1,2)]}
        self.assertEqual(self.client.post('/api/routes/submit/',payload,format='json').status_code,400)
        self.assertFalse(TourRoute.objects.exists())

    def test_token_refresh_has_ip_throttle(self):
        statuses=[self.client.post('/api/token/refresh/',{'refresh':'invalid'},format='json').status_code for _ in range(101)]
        self.assertEqual(statuses[0],401);self.assertEqual(statuses[-1],429)

    def test_route_creation_is_throttled_per_user(self):
        self.authenticate()
        statuses=[self.client.post('/api/routes/submit/',{},format='json').status_code for _ in range(21)]
        self.assertEqual(statuses[0],400);self.assertEqual(statuses[-1],429)

    def test_view_only_staff_cannot_approve_event_via_admin_post(self):
        self.user.is_staff=True;self.user.save(update_fields=['is_staff'])
        self.user.user_permissions.add(Permission.objects.get(codename='view_event',content_type__app_label='events'))
        self.client.force_login(self.user)
        self.event.status='pending';self.event.save(update_fields=['status'])
        self.client.post('/admin/events/event/',{'action':'approve_selected','_selected_action':[self.event.pk]})
        self.event.refresh_from_db();self.assertEqual(self.event.status,'pending')

    def test_nonstaff_cannot_access_admin_or_internal_models(self):
        self.client.force_login(self.user)
        for path in ('/admin/','/admin/ingestion/rawsourceitem/','/admin/events/event/'):
            self.assertEqual(self.client.get(path).status_code,302)
        self.authenticate()
        self.assertEqual(self.client.get('/api/ingestion/').status_code,404)

    def test_valid_event_photo_still_creates_owned_pending_submission(self):
        self.authenticate()
        with patch('accounts.storage.AccountMediaStorage._save',return_value='security-test.jpg'):
            response=self.client.post('/api/events/',self.event_payload(status='approved',submitted_by=self.other.pk),format='multipart')
        self.assertEqual(response.status_code,201)
        event=Event.objects.get(pk=response.data['id'])
        self.assertEqual(event.submitted_by,self.user);self.assertEqual(event.status,'pending')

    def test_invalid_coordinates_and_dates_rejected(self):
        self.authenticate()
        for values in ({'location':'POINT(181 49)'},{'location':'POINT(24 NaN)'},
                {'location':'SRID=3857;POINT(24 49)'},{'location':'LINESTRING(0 0,1 1)'},
                {'end_time':(timezone.now()-timedelta(days=1)).isoformat()}, {'price':'-1'}):
            with self.subTest(values=values),patch('accounts.storage.AccountMediaStorage._save') as save:
                response=self.client.post('/api/events/',self.event_payload(**values),format='multipart')
                self.assertEqual(response.status_code,400);save.assert_not_called()

    def test_geojson_external_crs_rejected_without_general_geometry_parser(self):
        import json
        self.authenticate()
        # Synthetic URL, with general geometry parsing mocked to prevent native/network work.
        geometry={'type':'Point','coordinates':[24,49.8],
            'crs':{'type':'link','properties':{'href':'https://geometry.invalid/crs'}}}
        with patch('django.contrib.gis.geos.GEOSGeometry',side_effect=AssertionError('Untrusted geometry reached native parser')):
            response=self.client.post('/api/events/',self.event_payload(location=json.dumps(geometry)),format='multipart')
        self.assertEqual(response.status_code,400);self.assertIn('location',response.data)

    def test_valid_wkt_and_geojson_point_inputs(self):
        from events.geometry import WGS84PointField
        field=WGS84PointField()
        for value in ('POINT(24 49.8)','SRID=4326;POINT(24 49.8)',
                '{"type":"Point","coordinates":[24,49.8]}',{'type':'Point','coordinates':[24,49.8]}):
            with self.subTest(value=value):self.assertEqual(field.run_validation(value),Point(24,49.8,srid=4326))

    def test_oversized_json_rejected_before_route_creation(self):
        self.authenticate()
        response=self.client.post('/api/routes/submit/',{'description':'x'*(1024*1024)},format='json')
        self.assertEqual(response.status_code,400);self.assertFalse(TourRoute.objects.exists())

    def test_auth_throttle_cannot_be_bypassed_by_forged_forwarded_header(self):
        from rytm.throttling import AuthIPThrottle
        with patch.dict(AuthIPThrottle.THROTTLE_RATES,{'account_auth':'2/min'}):
            results=[self.client.post('/api/token/refresh/',{'refresh':'invalid'},format='json',
                HTTP_X_FORWARDED_FOR=f'192.0.2.{i}').status_code for i in range(3)]
        self.assertEqual(results,[401,401,429])

    def test_password_token_endpoint_is_throttled_before_authentication(self):
        from rytm.throttling import AuthIPThrottle
        with patch.dict(AuthIPThrottle.THROTTLE_RATES,{'account_auth':'2/min'}):
            # Missing fields exercise request throttling, never guess credentials.
            results=[self.client.post('/api/token/',{},format='json').status_code for _ in range(3)]
        self.assertEqual(results,[400,400,429])

    def test_admin_login_is_throttled_without_guessing_credentials(self):
        from rytm.throttling import AdminLoginThrottle
        with patch.dict(AdminLoginThrottle.THROTTLE_RATES,{'admin_login':'2/min'}):
            results=[self.client.post('/admin/login/',{}).status_code for _ in range(3)]
        self.assertEqual(results,[200,200,429])

    def test_authorized_staff_can_still_moderate(self):
        self.user.is_staff=True;self.user.save(update_fields=['is_staff'])
        self.user.user_permissions.add(Permission.objects.get(codename='change_event',content_type__app_label='events'))
        self.client.force_login(self.user)
        self.event.status='pending';self.event.save(update_fields=['status'])
        self.client.post('/admin/events/event/',{'action':'approve_selected','_selected_action':[self.event.pk]})
        self.event.refresh_from_db();self.assertEqual(self.event.status,'approved')

    def test_compression_codec_cannot_expand_json_request(self):
        import zlib
        self.authenticate()
        # Small, harmless fixture: proves a byte-transform codec is rejected.
        payload = zlib.compress(b'{"title":"test"}')
        response = self.client.post('/api/routes/submit/', payload,
            content_type='application/json; charset=zlib_codec')
        self.assertEqual(response.status_code,400)
        self.assertFalse(TourRoute.objects.exists())
