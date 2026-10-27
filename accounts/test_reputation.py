from datetime import timedelta
from types import SimpleNamespace
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.gis.geos import Point
from django.db import IntegrityError, transaction
from django.test import RequestFactory
from django.utils import timezone
from rest_framework.test import APITestCase
from events.models import Event, EventReport, Organizer
from events.admin import EventReportAdmin
from ingestion.models import EventCandidate
from .reputation import reputation_summary, reputation_label

class ReputationTests(APITestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user('owner', email='private@example.com')
        self.reporter = User.objects.create_user('reporter', email='reporter@example.com')
        self.staff = User.objects.create_superuser('reviewer', 'admin@example.com', 'test')
        self.client.force_authenticate(self.reporter)

    def event(self, status='approved', owner=True):
        return Event.objects.create(title='Public event', description='Details', category='concert',
            start_time=timezone.now()+timedelta(days=3), location=Point(24.03,49.84),
            address='Lviv', status=status, submitted_by=self.owner if owner else None)

    def score(self, user=None):
        return reputation_summary((user or self.owner).pk)['reputation']['score']

    def report(self, event=None, status='pending'):
        return EventReport.objects.create(event=event or self.event(), reporter=self.reporter,
            reason='other', status=status, moderator_note='PRIVATE NOTE')

    def test_new_user_neutral(self): self.assertEqual(self.score(), 50)
    def test_approved_improves_gradually(self):
        self.event(); self.assertEqual(self.score(), 52)
    def test_rejected_lowers(self):
        self.event('rejected'); self.assertEqual(self.score(), 45)
    def test_pending_neutral(self):
        self.event('pending'); self.assertEqual(self.score(), 50)
    def test_pending_report_neutral(self):
        self.report(); self.assertEqual(self.score(), 52)
    def test_under_review_report_neutral(self):
        self.report(status='under_review'); self.assertEqual(self.score(), 52)
    def test_dismissed_report_neutral(self):
        self.report(status='dismissed'); self.assertEqual(self.score(), 52)
    def test_confirmed_report_lowers(self):
        self.report(status='confirmed'); self.assertEqual(self.score(), 42)
    def test_score_clamps(self):
        for _ in range(26): self.event()
        self.assertEqual(self.score(), 100)
        Event.objects.filter(submitted_by=self.owner).update(status='rejected')
        self.assertEqual(self.score(), 0)
    def test_labels(self):
        for score, label in [(0,'Низька надійність'),(29,'Низька надійність'),(30,'Новий організатор'),
                (59,'Новий організатор'),(60,'Надійний організатор'),(79,'Надійний організатор'),
                (80,'Висока надійність'),(100,'Висока надійність')]:
            with self.subTest(score=score): self.assertEqual(reputation_label(score), label)
    def test_linked_candidates_count_once(self):
        event=self.event()
        EventCandidate.objects.create(title='Mirror', submitted_by=self.owner, event=event, status='approved')
        self.assertEqual(reputation_summary(self.owner.pk)['stats']['approved_events'], 1)
        self.assertEqual(self.score(), 52)
    def test_unlinked_pending_rejected_candidates(self):
        EventCandidate.objects.create(title='Pending', submitted_by=self.owner)
        EventCandidate.objects.create(title='Rejected', submitted_by=self.owner, status='rejected')
        stats=reputation_summary(self.owner.pk)['stats']
        self.assertEqual((stats['pending_events'],stats['rejected_events']), (1,1))
        self.assertEqual(self.score(),45)
    def test_report_authenticated(self):
        event=self.event()
        response=self.client.post(f'/api/events/{event.pk}/report/', {'reason':'wrong_location','description':'Check location','status':'confirmed','reporter':self.owner.pk}, format='json')
        self.assertEqual(response.status_code,201)
        report=EventReport.objects.get()
        self.assertEqual(report.reporter,self.reporter); self.assertEqual(report.status,'pending')
        self.assertEqual(set(response.data),{'id','status','created_at'})
    def test_anonymous_report_rejected(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.post(f'/api/events/{self.event().pk}/report/',{'reason':'other'}).status_code,401)
    def test_invalid_reason(self):
        self.assertEqual(self.client.post(f'/api/events/{self.event().pk}/report/',{'reason':'invalid'}).status_code,400)
    def test_invalid_description(self):
        self.assertEqual(self.client.post(f'/api/events/{self.event().pk}/report/',{'reason':'other','description':'x'*2001}).status_code,400)
    def test_missing_or_private_event(self):
        for pk in [99999,self.event('pending').pk,self.event('rejected').pk]:
            self.assertEqual(self.client.post(f'/api/events/{pk}/report/',{'reason':'other'}).status_code,404)
    def test_duplicates_api(self):
        report=self.report()
        for status in ['pending','under_review','confirmed']:
            report.status=status;report.save()
            response=self.client.post(f'/api/events/{report.event_id}/report/',{'reason':'other'})
            self.assertEqual(response.status_code,409); self.assertEqual(response.data['code'],'duplicate_report')
        self.assertEqual(EventReport.objects.count(),1)
    def test_duplicates_db(self):
        report=self.report()
        with self.assertRaises(IntegrityError),transaction.atomic(): self.report(event=report.event)
    def test_dismissed_allows_new_report(self):
        report=self.report(status='dismissed')
        self.assertEqual(self.client.post(f'/api/events/{report.event_id}/report/',{'reason':'other'}).status_code,201)
    def test_multiple_reporters_allowed(self):
        report=self.report();self.client.force_authenticate(self.owner)
        self.assertEqual(self.client.post(f'/api/events/{report.event_id}/report/',{'reason':'other'}).status_code,201)
    def test_admin_confirm(self):
        report=self.report(); request=RequestFactory().post('/');request.user=self.staff
        model_admin=EventReportAdmin(EventReport,admin.site)
        model_admin.confirm_reports(request,EventReport.objects.filter(pk=report.pk))
        report.refresh_from_db();self.assertEqual(report.status,'confirmed')
        self.assertEqual(report.reviewed_by,self.staff); self.assertIsNotNone(report.reviewed_at)
        self.assertEqual(self.score(),42)
    def test_admin_dismiss(self):
        report=self.report(status='confirmed');request=RequestFactory().post('/');request.user=self.staff
        EventReportAdmin(EventReport,admin.site).dismiss_reports(request,EventReport.objects.all())
        self.assertEqual(self.score(),52)
        report.refresh_from_db();self.assertEqual(report.reviewed_by,self.staff)
    def test_admin_form_tracks_review(self):
        report=self.report();report.status='under_review'
        request=RequestFactory().post('/');request.user=self.staff
        EventReportAdmin(EventReport,admin.site).save_model(request,report,SimpleNamespace(changed_data=['status']),True)
        report.refresh_from_db();self.assertEqual(report.reviewed_by,self.staff)
        self.assertIsNotNone(report.reviewed_at)
    def test_real_admin_pages(self):
        self.report();self.client.force_login(self.staff)
        self.assertEqual(self.client.get('/admin/events/eventreport/').status_code,200)
        self.assertEqual(self.client.get(f'/admin/events/eventreport/{EventReport.objects.get().pk}/change/').status_code,200)
    def test_own_profile_email_stats_and_link(self):
        self.client.force_authenticate(self.owner)
        data=self.client.get('/api/me/profile/').data
        self.assertEqual(data['email'],'private@example.com');self.assertEqual(data['reputation']['score'],50)
        self.assertEqual(set(data['stats']),{'approved_events','pending_events','rejected_events','confirmed_complaints'})
        self.assertFalse(data['google_linked']);self.assertFalse(data['verified_organizer'])
    def test_public_profile_privacy(self):
        self.report(status='confirmed')
        data=self.client.get(f'/api/users/{self.owner.pk}/profile/').data
        self.assertEqual(set(data),{'id','display_name','avatar_url','bio','reputation','stats','verified_organizer'})
        self.assertEqual(set(data['stats']),{'approved_events','confirmed_complaints'})
        self.assertEqual(data['reputation']['score'],42)
        self.assertNotIn('PRIVATE',str(data));self.assertNotIn('reporter',str(data))
    def test_public_event_organizer_safe(self):
        event=self.event();self.report(event,status='confirmed')
        data=self.client.get(f'/api/events/{event.pk}/').data
        self.assertEqual(set(data['organizer']),{'id','display_name','avatar_url','reputation_score','reputation_label','verified_organizer'})
        self.assertEqual(data['organizer']['reputation_score'],42)
        self.assertNotIn('reports',data);self.assertNotIn('private@example.com',str(data))
    def test_only_actual_owner_affected(self):
        self.report(status='confirmed');self.assertEqual(self.score(self.reporter),50)
        self.assertEqual(self.score(self.staff),50)
    def test_machine_null_owner(self):
        event=self.event(owner=False);self.report(event,status='confirmed')
        self.assertEqual(self.score(),50)
        self.assertIsNone(self.client.get(f'/api/events/{event.pk}/').data['organizer'])
    def test_reputation_cannot_self_verify_or_edit(self):
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.client.patch('/api/me/profile/',{'reputation':{'score':100},'verified_organizer':True},format='json').status_code,400)
        for _ in range(25):self.event()
        self.assertEqual(self.score(),100)
        self.assertFalse(self.client.get('/api/me/profile/').data['verified_organizer'])

    def test_admin_cannot_reactivate_dismissed_duplicate(self):
        dismissed=self.report(status='dismissed');self.report(event=dismissed.event)
        self.client.force_login(self.staff)
        response=self.client.post(f'/admin/events/eventreport/{dismissed.pk}/change/',
                                  {'status':'confirmed','moderator_note':'Check'})
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'Для цього користувача вже є активна скарга')
        dismissed.refresh_from_db();self.assertEqual(dismissed.status,'dismissed')
    def test_admin_actual_form_confirmation(self):
        report=self.report();self.client.force_login(self.staff)
        response=self.client.post(f'/admin/events/eventreport/{report.pk}/change/',
                                  {'status':'confirmed','moderator_note':'Confirmed evidence'})
        self.assertEqual(response.status_code,302)
        report.refresh_from_db();self.assertEqual(report.status,'confirmed')
        self.assertEqual(report.reviewed_by,self.staff);self.assertIsNotNone(report.reviewed_at)
        self.assertEqual(self.score(),42)
    def test_non_staff_cannot_moderate(self):
        report=self.report();self.client.force_login(self.reporter)
        self.assertEqual(self.client.post(f'/admin/events/eventreport/{report.pk}/change/',
            {'status':'confirmed'}).status_code,302)
        report.refresh_from_db();self.assertEqual(report.status,'pending')
    def test_google_link_and_verification_are_server_owned(self):
        from .models import UserIdentity
        UserIdentity.objects.create(user=self.owner,provider='google',provider_subject='subject')
        Organizer.objects.create(user=self.owner,name='Verified',contact_email=self.owner.email,verified=True)
        self.client.force_authenticate(self.owner)
        data=self.client.get('/api/me/profile/').data
        self.assertTrue(data['google_linked']);self.assertTrue(data['verified_organizer'])
        public=self.client.get(f'/api/users/{self.owner.pk}/profile/').data
        self.assertNotIn('google_linked',public)
