"""Moderator-owned public copy stays separate from extraction drafts and warnings."""
from datetime import timedelta
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.forms import modelform_factory
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from .admin import EventCandidateAdmin
from .extraction import extract_raw_source_item
from .extractors.contracts import ExtractionResult
from .forms import EventCandidateAdminForm
from .models import EventCandidate, RawSourceItem, SourceAccount
from .services import approve_candidate, build_candidate_description, final_candidate_description


class PublicDescriptionTests(TestCase):
    def setUp(self):
        media = TemporaryDirectory()
        self.addCleanup(media.cleanup)
        setting = override_settings(MEDIA_ROOT=media.name, STORAGES={
            'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
        })
        setting.enable()
        self.addCleanup(setting.disable)
        self.reviewer = get_user_model().objects.create_superuser('copy-reviewer', password=None)
        source = SourceAccount.objects.create(username='description_fixture')
        self.raw = RawSourceItem.objects.create(source_account=source, external_id='copy',
            source_url='https://example.com/post', raw_text='PRIVATE CAPTION')
        self.candidate = EventCandidate.objects.create(raw_source_item=self.raw, title='Concert',
            description='PRIVATE EXTRACTION DRAFT', extraction_notes='PRIVATE INTERNAL NOTE',
            start_time=timezone.now() + timedelta(days=7), address='Lviv, Test Street 1',
            latitude=49.8397, longitude=24.0297, category='concert',
            public_photo='candidate_photos/owned.jpg')
        self.form_class = modelform_factory(EventCandidate, form=EventCandidateAdminForm,
            fields=('title', 'public_description'))

    def test_generation_is_default_and_internal_text_is_private(self):
        form = self.form_class(instance=self.candidate)
        self.assertEqual(form.initial['public_description'], build_candidate_description(self.candidate))
        self.assertEqual(final_candidate_description(self.candidate), build_candidate_description(self.candidate))
        self.assertNotIn('PRIVATE', form.initial['public_description'])

    def test_admin_edit_persists_without_publishing(self):
        form = self.form_class({'title': 'Concert', 'public_description': '  Живий концерт у Львові.  '}, instance=self.candidate)
        self.assertTrue(form.is_valid(), form.errors)
        candidate = form.save()
        candidate.refresh_from_db()
        self.assertEqual(candidate.public_description, 'Живий концерт у Львові.')
        self.assertEqual(candidate.description, 'PRIVATE EXTRACTION DRAFT')
        self.assertEqual(candidate.status, 'new')
        self.assertIsNone(candidate.event_id)

    def test_unedited_generated_value_does_not_freeze_old_facts(self):
        generated = build_candidate_description(self.candidate)
        form = self.form_class({'title': 'New title', 'public_description': generated}, instance=self.candidate)
        self.assertTrue(form.is_valid(), form.errors)
        candidate = form.save()
        self.assertEqual(candidate.public_description, '')
        self.assertIn('New title', final_candidate_description(candidate))

    def test_approval_and_api_use_moderator_copy(self):
        self.candidate.public_description = 'Вечір живої музики. Приходьте з друзями!'
        self.candidate.save()
        event, _ = approve_candidate(self.candidate, self.reviewer)
        response = APIClient().get(f'/api/events/{event.pk}/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['description'], self.candidate.public_description)
        self.assertEqual(event.description, self.candidate.public_description)
        self.assertNotIn('PRIVATE', str(response.data))
        self.assertNotIn('extraction_notes', response.data)

    def test_blank_override_falls_back_at_approval(self):
        self.candidate.public_description = '  '
        self.candidate.save()
        event, _ = approve_candidate(self.candidate, self.reviewer)
        self.assertEqual(event.description, build_candidate_description(self.candidate))

    def test_reextraction_preserves_moderator_copy(self):
        self.candidate.public_description = 'Moderator-approved wording'
        self.candidate.start_time = None
        self.candidate.save()
        provider = Mock()
        provider.extract.return_value = ExtractionResult(True, title='Concert',
            start_time=timezone.now() + timedelta(days=7), confidence=0.9)
        extract_raw_source_item(self.raw, provider=provider, reextract=True)
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.public_description, 'Moderator-approved wording')
        self.assertEqual(self.candidate.status, 'new')

    def test_public_copy_does_not_bypass_missing_fields_or_ambiguity(self):
        for updates in ({'start_time': None}, {'public_photo': ''},
                        {'extraction_notes': '[Extraction ambiguity] Verify event time.'}):
            with self.subTest(updates=updates):
                candidate = EventCandidate.objects.get(pk=self.candidate.pk)
                candidate.public_description = 'Ready to publish, according to this text'
                for field, value in updates.items():
                    setattr(candidate, field, value)
                candidate.save()
                with self.assertRaises(ValidationError):
                    approve_candidate(candidate, self.reviewer)
                candidate.refresh_from_db()
                self.assertEqual(candidate.status, 'new')
                self.assertIsNone(candidate.event_id)
                # Restore the fixture for the next independent validation case.
                candidate.start_time = self.candidate.start_time
                candidate.public_photo = self.candidate.public_photo
                candidate.extraction_notes = self.candidate.extraction_notes
                candidate.save()

    def test_public_field_is_editable_until_approval(self):
        model_admin = EventCandidateAdmin(EventCandidate, admin.site)
        request = RequestFactory().get('/admin/')
        request.user = self.reviewer
        self.assertNotIn('public_description', model_admin.get_readonly_fields(request, self.candidate))
        self.assertIn('public_description', model_admin.get_form(request, self.candidate).base_fields)
        self.candidate.status = 'approved'
        self.assertIn('public_description', model_admin.get_readonly_fields(request, self.candidate))
