from io import BytesIO
from unittest.mock import Mock, patch
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.forms.models import model_to_dict
from PIL import Image
from events.models import Event
from .admin import EventCandidateAdmin
from .forms import EventCandidateAdminForm
from .image_text import ImageTextError
from .models import EventCandidate
from .services import approve_candidate
from . import tests as fixtures

URL = 'https://scontent.test.cdninstagram.com/private.jpg?token=PRIVATE'


def image_bytes():
    output = BytesIO()
    Image.new('RGB', (20, 30), 'red').save(output, 'PNG')
    return output.getvalue()


class AdminMediaTests(TestCase):
    setUp = fixtures.IngestionTests.setUp
    candidate = fixtures.IngestionTests.candidate

    def test_admin_form_rejects_outside_lviv_and_invalid_coordinates(self):
        candidate = self.candidate()
        data = model_to_dict(candidate)
        for latitude, longitude in ((50.45, 30.52), (91, 24.03), ('nan', 24.03)):
            with self.subTest(latitude=latitude):
                form = EventCandidateAdminForm(data={**data, 'latitude': latitude, 'longitude': longitude}, instance=candidate)
                self.assertFalse(form.is_valid())
                self.assertIn('latitude', form.errors)

    def test_primary_fallback_and_missing_preview(self):
        candidate = self.candidate(public_photo='', source_image_url='', use_source_image_as_public_photo=True)
        self.raw.raw_image_url = URL
        self.raw.save()
        model_admin = EventCandidateAdmin(EventCandidate, admin.site)
        self.assertIn('candidate-source-image', model_admin.source_image_preview(candidate))
        with patch('ingestion.public_images.download_image', return_value=image_bytes()) as download:
            approve_candidate(candidate, self.reviewer)
        download.assert_called_once_with(URL)
        candidate.source_image_url = 'file:///etc/passwd'
        self.assertIn('No source image available', model_admin.source_image_preview(candidate))

    def test_map_form_media_and_existing_configuration(self):
        candidate = self.candidate()
        form = EventCandidateAdminForm(instance=candidate)
        self.assertIn('leaflet.js', str(form.media))
        self.assertIn('candidate.js', str(form.media))
        self.assertIn('49.8397', str(form['latitude']))
        model_admin = EventCandidateAdmin(EventCandidate, admin.site)
        html = model_admin.location_picker(candidate)
        self.assertIn('data-latitude="49.8397"', html)
        self.assertIn('data-longitude="24.0297"', html)

    def test_admin_image_and_consent_render_privately(self):
        candidate = self.candidate(source_image_url=URL)
        self.client.force_login(self.reviewer)
        response = self.client.get(reverse('admin:ingestion_eventcandidate_change', args=[candidate.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'candidate-location-map')
        self.assertContains(response, 'candidate-source-image')
        self.assertContains(response, 'Use source image as public event photo')
        self.client.logout()
        self.assertEqual(self.client.get(reverse('admin:ingestion_eventcandidate_change', args=[candidate.pk])).status_code, 302)
        self.assertNotIn('PRIVATE', self.client.get('/api/events/').content.decode())

    def test_consent_audited_on_admin_save(self):
        candidate = self.candidate(public_photo='', source_image_url=URL)
        candidate.use_source_image_as_public_photo = True
        request = RequestFactory().post('/')
        request.user = self.reviewer
        model_admin = EventCandidateAdmin(EventCandidate, admin.site)
        model_admin.save_model(request, candidate, Mock(changed_data=['use_source_image_as_public_photo']), True)
        candidate.refresh_from_db()
        self.assertEqual(candidate.source_photo_authorized_by, self.reviewer)
        self.assertIsNotNone(candidate.source_photo_authorized_at)
        self.assertIsNone(candidate.event_id)
        candidate.use_source_image_as_public_photo = False
        model_admin.save_model(request, candidate, Mock(changed_data=['use_source_image_as_public_photo']), True)
        self.assertIsNone(candidate.source_photo_authorized_at)

    def test_explicit_source_copy_uses_django_storage_and_is_idempotent(self):
        candidate = self.candidate(public_photo='', source_image_url=URL, use_source_image_as_public_photo=True)
        with patch('ingestion.public_images.download_image', return_value=image_bytes()) as download:
            event, created = approve_candidate(candidate, self.reviewer)
            self.assertTrue(created)
            self.assertTrue(event.photo.name.startswith('events_photos/source-'))
            self.assertTrue(event.photo.storage.exists(event.photo.name))
            self.assertNotIn('cdninstagram', event.photo.name)
            self.assertEqual(approve_candidate(candidate, self.reviewer), (event, False))
        download.assert_called_once_with(URL)
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, 'approved')
        response = self.client.get('/api/events/')
        self.assertNotIn('PRIVATE', response.content.decode())
        self.assertNotIn('source_photo_authorized', response.content.decode())
        self.assertNotIn('use_source_image', response.content.decode())

    def test_manual_upload_takes_priority(self):
        candidate = self.candidate(use_source_image_as_public_photo=True, source_image_url=URL)
        with patch('ingestion.public_images.download_image') as download:
            event, _ = approve_candidate(candidate, self.reviewer)
        download.assert_not_called()
        self.assertEqual(event.photo.name, candidate.public_photo.name)

    def test_download_mime_size_and_expiry_fail_without_partial_approval(self):
        candidate = self.candidate(public_photo='', source_image_url=URL, use_source_image_as_public_photo=True)
        for reason in ('image_http_failure', 'unsupported_image_mime', 'image_too_large'):
            with patch('ingestion.public_images.download_image', side_effect=ImageTextError(reason)):
                with self.assertRaisesMessage(ValidationError, 'Upload a public photo manually'):
                    approve_candidate(candidate, self.reviewer)
            candidate.refresh_from_db()
            self.assertEqual(candidate.status, 'new')
            self.assertIsNone(candidate.event_id)
            self.assertFalse(Event.objects.exists())
        with patch('ingestion.public_images.download_image', return_value=b'not an image'), self.assertRaises(ValidationError):
            approve_candidate(candidate, self.reviewer)
        self.assertFalse(Event.objects.exists())

    def test_storage_failure_rolls_back(self):
        candidate = self.candidate(public_photo='', source_image_url=URL, use_source_image_as_public_photo=True)
        storage = Event._meta.get_field('photo').storage
        with patch('ingestion.public_images.download_image', return_value=image_bytes()), patch.object(storage, 'save', side_effect=RuntimeError('SECRET')):
            with self.assertRaisesMessage(ValidationError, 'Photo storage failed'):
                approve_candidate(candidate, self.reviewer)
        self.assertFalse(Event.objects.exists())
        candidate.refresh_from_db()
        self.assertIsNone(candidate.event_id)

    def test_database_failure_cleans_up_only_new_storage_copy(self):
        candidate = self.candidate(public_photo='', source_image_url=URL, use_source_image_as_public_photo=True)
        storage = Event._meta.get_field('photo').storage
        with patch('ingestion.public_images.download_image', return_value=image_bytes()), patch.object(Event, 'save', side_effect=RuntimeError('DB failure')), patch.object(storage, 'delete', wraps=storage.delete) as delete:
            with self.assertRaises(RuntimeError):
                approve_candidate(candidate, self.reviewer)
        delete.assert_called_once()
        self.assertFalse(storage.exists(delete.call_args.args[0]))
        self.assertFalse(Event.objects.exists())

    def test_consent_and_lviv_coordinates_required(self):
        candidate = self.candidate(public_photo='', source_image_url=URL)
        with patch('ingestion.public_images.download_image') as download:
            with self.assertRaises(ValidationError):
                approve_candidate(candidate, self.reviewer)
            candidate.use_source_image_as_public_photo = True
            candidate.latitude, candidate.longitude = 50.45, 30.52
            candidate.save()
            with self.assertRaisesMessage(ValidationError, 'supported Lviv bounds'):
                approve_candidate(candidate, self.reviewer)
            candidate.latitude = None
            candidate.save()
            with self.assertRaises(ValidationError):
                approve_candidate(candidate, self.reviewer)
        download.assert_not_called()

    def test_bulk_does_not_assume_consent(self):
        candidate = self.candidate(public_photo='', source_image_url=URL)
        request = RequestFactory().post('/')
        request.user = self.reviewer
        model_admin = EventCandidateAdmin(EventCandidate, admin.site)
        with patch.object(model_admin, 'message_user') as message, patch('ingestion.public_images.download_image') as download:
            model_admin.approve_selected(request, EventCandidate.objects.filter(pk=candidate.pk))
        download.assert_not_called()
        self.assertIn('explicitly authorize', str(message.call_args_list))
        self.assertFalse(Event.objects.exists())
