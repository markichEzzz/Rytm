from datetime import timedelta
from django.contrib.auth import get_user_model
from django.contrib.gis.geos import Point
from django.db import IntegrityError, transaction, connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APITestCase
from .models import Event, Organizer, UserEventInteraction as Signal
from .recommendations import rank_events, event_score, affinities

class RecommendationTests(APITestCase):
    def setUp(self):
        User=get_user_model()
        self.user=User.objects.create_user('viewer',email='private@example.com')
        self.other=User.objects.create_user('other',email='other@example.com')
        self.now=timezone.now()
    def event(self, category='concert',days=2,owner=None,**kwargs):
        return Event.objects.create(title=kwargs.pop('title','Event'),description='Description',category=category,
            start_time=self.now+timedelta(days=days),location=Point(24.03,49.84),address='Lviv',status=kwargs.pop('status','approved'),submitted_by=owner,**kwargs)
    def signal(self,event,kind='interested',user=None):
        return Signal.objects.create(user=user or self.user,event=event,interaction_type=kind)
    def feed(self,ordering='for_you',**params):
        response=self.client.get('/api/events/',{'ordering':ordering,**params})
        self.assertEqual(response.status_code,200,response.data)
        return response
    def ids(self,**params): return [row['id'] for row in self.feed(**params).data]
    def test_anonymous_for_you_is_normal_catalogue(self):
        later=self.event(days=4);early=self.event('party',days=1)
        self.assertEqual(self.ids(),[early.pk,later.pk])
        self.assertNotIn('user_interest',self.feed().data[0])
    def test_cold_start_is_useful_and_complete(self):
        later=self.event(days=4);early=self.event(days=1)
        self.client.force_authenticate(self.user)
        self.assertEqual(self.ids(),[early.pk,later.pk])
    def test_category_affinity_boosts_similar_events(self):
        history=self.event(days=-3);self.signal(history)
        concert=self.event('concert');party=self.event('party',days=1.9)
        self.client.force_authenticate(self.user)
        self.assertEqual(self.ids(),[concert.pk,party.pk])
    def test_not_interested_reduces_category_and_exact_event(self):
        concert=self.event('concert');party=self.event('party',days=2.1)
        self.signal(concert,'not_interested')
        self.client.force_authenticate(self.user)
        self.assertEqual(self.ids(),[party.pk,concert.pk])
        self.assertEqual(len(self.ids()),2)
    def test_explicit_interest_stronger_than_view(self):
        viewed=self.event();saved=self.event();self.signal(viewed,'view');self.signal(saved)
        self.client.force_authenticate(self.user)
        self.assertEqual(self.ids()[0],saved.pk)
    def test_organizer_affinity_uses_actual_owner(self):
        owner=get_user_model().objects.create_user('owner')
        history=self.event(days=-1,owner=owner);self.signal(history)
        same=self.event('nature',owner=owner);other=self.event('nature',days=1.99)
        self.client.force_authenticate(self.user)
        self.assertEqual(self.ids()[0],same.pk)
    def test_machine_events_have_no_fake_organizer_affinity(self):
        self.signal(self.event(days=-1))
        _,owners,_=affinities(self.user,self.now)
        self.assertEqual(dict(owners),{})
    def test_unrelated_catalogue_is_preserved_with_exploration(self):
        self.signal(self.event(days=-1))
        concerts=[self.event() for _ in range(16)]
        other=[self.event('nature',days=3) for _ in range(4)]
        self.client.force_authenticate(self.user)
        ids=self.ids();self.assertEqual(set(ids),{e.pk for e in concerts+other})
        self.assertEqual(len(ids),len(set(ids)))
        self.assertIn(ids[4],{e.pk for e in other});self.assertIn(ids[9],{e.pk for e in other})
    def test_visibility_ongoing_expired_unapproved(self):
        past=self.event(days=-2);self.event(status='pending');self.event(status='rejected')
        ongoing=self.event(days=-1,end_time=self.now+timedelta(hours=1))
        future=self.event()
        self.client.force_authenticate(self.user)
        self.assertEqual(set(self.ids()),{ongoing.pk,future.pk})
    def test_all_order_is_chronological_even_with_preferences(self):
        a=self.event('party',days=1);b=self.event(days=3);self.signal(b)
        self.client.force_authenticate(self.user)
        self.assertEqual(self.ids(ordering='all'),[a.pk,b.pk])
    def test_future_affinity_does_not_dominate_soon_catalogue(self):
        for _ in range(3):self.signal(self.event(days=-2))
        soon=self.event('party',days=1);far=self.event(days=100)
        self.client.force_authenticate(self.user)
        self.assertEqual(self.ids()[0],soon.pk)
    def test_popularity_is_first_party_limited_and_excludes_owner(self):
        owner=get_user_model().objects.create_user('owner');a=self.event(owner=owner);b=self.event()
        self.signal(a,user=owner)
        from .recommendations import popularity, POPULARITY_CAP
        self.assertEqual(popularity([a],self.now),{})
        self.signal(b,user=self.other)
        self.assertEqual(popularity([b],self.now)[b.pk],1)
        self.assertLessEqual(event_score(b,{}, {},{}, {b.pk:100000},self.now)-event_score(b,{}, {},{},{},self.now),POPULARITY_CAP)
    def test_rank_deterministic_with_fixed_clock(self):
        events=[self.event('concert'),self.event('party')];self.signal(events[0])
        self.assertEqual([e.pk for e in rank_events(events,self.user,{},self.now)],[e.pk for e in rank_events(events,self.user,{},self.now)])
    def test_filters_and_search_preserved(self):
        self.event('concert',title='Джаз',price_status='free',price=0);self.event('party')
        self.client.force_authenticate(self.user)
        data=self.feed(category='concert',q='Джаз',price_status='free',bbox='23.9,49.7,24.2,49.9').data
        self.assertEqual(len(data),1);self.assertEqual(data[0]['title'],'Джаз')
        self.assertEqual(self.feed(date_from=(self.now+timedelta(days=10)).date()).data,[])
    def test_old_history_does_not_build_affinity(self):
        signal=self.signal(self.event(days=-1));Signal.objects.filter(pk=signal.pk).update(last_interacted_at=self.now-timedelta(days=91))
        cats,owners,has_history=affinities(self.user,self.now)
        self.assertFalse(has_history);self.assertEqual(dict(cats),{})
    def test_negative_overrides_weak_signals_for_same_event(self):
        event=self.event();self.signal(event,'view');self.signal(event,'map_open');self.signal(event,'not_interested')
        cats,_,_=affinities(self.user,self.now);self.assertEqual(cats['concert'],-6)
    def test_private_history_not_in_public_profile(self):
        event=self.event();self.signal(event)
        data=self.client.get(f'/api/users/{self.user.pk}/profile/').data
        self.assertNotIn('interactions',data);self.assertNotIn('user_interest',data)
        self.assertNotIn('private@example.com',str(data))
    def test_query_user_id_does_not_select_someone_else_preferences(self):
        a=self.event('party');b=self.event();self.signal(a,user=self.other);self.signal(b)
        self.client.force_authenticate(self.user)
        self.assertEqual(self.ids(user_id=self.other.pk),self.ids())
    def test_response_has_no_scores_or_history_and_is_not_shared_cacheable(self):
        event=self.event();self.signal(event);self.client.force_authenticate(self.user)
        response=self.feed();data=response.data[0]
        self.assertEqual(data['user_interest'],'interested')
        self.assertNotIn('score',data);self.assertNotIn('interaction_history',data)
        self.assertIn('private',response['Cache-Control']);self.assertIn('Authorization',response['Vary'])
    def test_query_count_is_bounded_for_many_organizers(self):
        for i in range(12):self.event(owner=get_user_model().objects.create_user('owner'+str(i)))
        self.client.force_authenticate(self.user)
        with CaptureQueriesContext(connection) as queries: self.feed()
        self.assertLessEqual(len(queries),10,[q['sql'][:150] for q in queries])
    def test_interactions_require_auth(self):
        event=self.event()
        for method in [self.client.get,self.client.post]:
            self.assertEqual(method(f'/api/events/{event.pk}/interaction/',{'type':'view'}).status_code,401)
    def test_interaction_request_user_is_authoritative(self):
        event=self.event();self.client.force_authenticate(self.user)
        response=self.client.post(f'/api/events/{event.pk}/interaction/',{'type':'interested','user_id':self.other.pk})
        self.assertEqual(response.status_code,200)
        self.assertEqual(Signal.objects.get().user_id,self.user.pk)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(f'/api/events/{event.pk}/interaction/').data['user_interest'],'neutral')
    def test_duplicate_views_bounded_and_timestamp_cooldown(self):
        event=self.event();self.client.force_authenticate(self.user)
        url=f'/api/events/{event.pk}/interaction/'
        self.client.post(url,{'type':'view'});old=Signal.objects.get().last_interacted_at
        for _ in range(5):self.client.post(url,{'type':'view'})
        self.assertEqual(Signal.objects.count(),1);self.assertEqual(Signal.objects.get().last_interacted_at,old)
        Signal.objects.update(last_interacted_at=old-timedelta(days=2))
        self.client.post(url,{'type':'view'});self.assertGreater(Signal.objects.get().last_interacted_at,old-timedelta(days=1))
    def test_preference_swap_and_withdraw_are_idempotent(self):
        event=self.event();self.client.force_authenticate(self.user);url=f'/api/events/{event.pk}/interaction/'
        for _ in range(2):self.assertEqual(self.client.post(url,{'type':'interested'}).data['user_interest'],'interested')
        self.assertEqual(Signal.objects.count(),1)
        self.assertEqual(self.client.post(url,{'type':'not_interested'}).data['user_interest'],'not_interested')
        self.assertEqual(Signal.objects.count(),1)
        for _ in range(2):self.assertEqual(self.client.post(url,{'type':'not_interested','active':False}).data['user_interest'],'neutral')
        self.assertEqual(Signal.objects.count(),0)
    def test_invalid_interaction_and_nonpublic_target(self):
        self.client.force_authenticate(self.user);event=self.event();url=f'/api/events/{event.pk}/interaction/'
        self.assertEqual(self.client.post(url,{'type':'attendance'}).status_code,400)
        self.assertEqual(self.client.post(url,{'type':'view','active':False}).status_code,400)
        event.status='pending';event.save()
        self.assertEqual(self.client.post(url,{'type':'view'}).status_code,404)
    def test_duplicate_constraints(self):
        event=self.event();self.signal(event)
        with self.assertRaises(IntegrityError),transaction.atomic():self.signal(event,'not_interested')
    def test_detail_interest_is_only_current_user(self):
        event=self.event();self.signal(event)
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.get(f'/api/events/{event.pk}/').data['user_interest'],'interested')
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(f'/api/events/{event.pk}/').data['user_interest'],'neutral')
        self.client.force_authenticate(None)
        self.assertNotIn('user_interest',self.client.get(f'/api/events/{event.pk}/').data)
