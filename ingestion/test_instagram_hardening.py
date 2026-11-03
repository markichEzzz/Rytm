from datetime import timedelta
from io import StringIO
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import instaloader
from django.core.management import call_command, CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .instagram_policy import InstagramPolicy
from .instagram_schedule import InstagramSchedule
from .instagram_test_support import isolate_instagram_schedule
from .collectors import CollectedSourceItem
from .models import SourceAccount, RawSourceItem
from .providers.instagram import InstaloaderProvider, InstagramCollectionError
from .providers.instagram_playwright import PlaywrightInstagramProvider
from .providers.instagram_safety import failure, page_failure
from .test_instagram_playwright import FakeBrowser, FakePage


class ScheduleTests(SimpleTestCase):
    def setUp(self):
        self.directory = isolate_instagram_schedule(self)

    def test_safe_defaults_and_invalid_values_fail_closed(self):
        policy = InstagramPolicy.load()
        self.assertEqual((policy.max_sources, policy.max_posts, policy.source_delay, policy.navigation_delay), (2, 2, 60, 10))
        self.assertEqual(policy.block_cooldown, 86400)
        for name, value in [('INSTAGRAM_MAX_POSTS_PER_SOURCE', 4), ('INSTAGRAM_MAX_SOURCES_PER_RUN', 0), ('INSTAGRAM_SOURCE_DELAY_SECONDS', -1)]:
            with self.settings(**{name:value}), self.assertRaises(InstagramCollectionError):
                InstagramPolicy.load()

    def test_lock_excludes_other_runs_and_releases_on_exit(self):
        policy = InstagramPolicy.load()
        with InstagramSchedule(policy):
            with self.assertRaises(InstagramCollectionError) as caught:
                with InstagramSchedule(policy): pass
            self.assertEqual(caught.exception.reason, 'locked')
        with InstagramSchedule(policy): pass

    def test_schedule_persists_intervals_and_global_block(self):
        source = SimpleNamespace(pk=42, last_checked_at=None)
        now = timezone.now().timestamp()
        policy = InstagramPolicy.load()
        with InstagramSchedule(policy) as state:
            state.reserve(source)
            self.assertGreater(state.due_at(source), now + 3500)
            state.complete(source, 0)
            self.assertGreater(state.due_at(source), now + 43199)
            state.failed(source, 'rate_limit', True)
        with InstagramSchedule(policy) as state:
            self.assertGreater(state.data['global_until'], now + 86399)
            self.assertEqual(state.data['global_reason'], 'rate_limit')
            self.assertEqual(state.data['sources']['42']['failures'], 1)
        self.assertEqual((Path(self.directory)/'schedule.json').stat().st_mode & 0o777, 0o600)

    def test_database_checkpoint_cannot_be_bypassed_by_absent_local_state(self):
        now = timezone.now()
        with InstagramSchedule(InstagramPolicy.load()) as state:
            self.assertGreater(state.due_at(SimpleNamespace(pk=1,last_checked_at=now)), now.timestamp()+21599)

    def test_failure_backoff_is_bounded(self):
        source = SimpleNamespace(pk=1,last_checked_at=None)
        with InstagramSchedule(InstagramPolicy.load()) as state:
            delays=[]
            for _ in range(10):
                state.failed(source,'timeout',False)
                delays.append(state.due_at(source)-timezone.now().timestamp())
            self.assertGreater(delays[1],delays[0]*1.9)
            self.assertLessEqual(delays[-1],21600)
            self.assertEqual(state.data['global_until'],0)

    def test_corrupt_or_symlink_state_fails_closed(self):
        path=Path(self.directory)/'schedule.json'
        path.write_text('{broken')
        with self.assertRaises(InstagramCollectionError):
            with InstagramSchedule(InstagramPolicy.load()): pass
        path.unlink();path.symlink_to('/etc/passwd')
        with self.assertRaises(InstagramCollectionError):
            with InstagramSchedule(InstagramPolicy.load()): pass

    def test_failed_atomic_save_retains_old_state(self):
        source=SimpleNamespace(pk=1,last_checked_at=None)
        with InstagramSchedule(InstagramPolicy.load()) as state:
            state.reserve(source);before=state.path.read_bytes()
            with patch('ingestion.instagram_schedule.os.replace',side_effect=OSError('SECRET')):
                with self.assertRaises(InstagramCollectionError) as caught:state.complete(source,1)
            self.assertNotIn('SECRET',str(caught.exception))
            self.assertEqual(state.path.read_bytes(),before)


class RestrictionTests(SimpleTestCase):
    def setUp(self):
        isolate_instagram_schedule(self)
        sleeper=patch('ingestion.providers.instagram_playwright.time.sleep');sleeper.start();self.addCleanup(sleeper.stop)

    def test_url_and_http_restrictions_are_distinct_and_safe(self):
        for path,code in [('/accounts/login/','login_required'),('/challenge/SECRET/','challenge'),('/checkpoint/','challenge'),('/accounts/verify/','verification')]:
            page=FakePage();page.url='https://www.instagram.com'+path
            error=page_failure(page)
            self.assertEqual(error.reason,code);self.assertTrue(error.stop_run);self.assertNotIn('SECRET',str(error))
        for status,code,stop in [(429,'rate_limit',True),(403,'access_blocked',True),(401,'login_required',True),(404,'not_found',False),(503,'unavailable',False)]:
            error=page_failure(FakePage(),status)
            self.assertEqual((error.reason,error.stop_run),(code,stop))

    def test_visible_security_states_stop_without_any_navigation(self):
        for text,code in [('Try Again Later','temporary_block'),('We detected suspicious activity.','temporary_block'),("Confirm it's you",'verification'),('Checkpoint required','challenge'),('Please wait a few minutes before you try again','rate_limit')]:
            browser=FakeBrowser();browser.page.messages=[text]
            provider=PlaywrightInstagramProvider(browser_factory=lambda:browser)
            with self.assertRaises(InstagramCollectionError) as caught:provider.fetch_recent('one',2)
            self.assertEqual(caught.exception.reason,code)
            self.assertEqual(browser.page.visits,[]);browser.close.assert_called_once()

    def test_api_429_on_http_200_page_stops_without_post_requests(self):
        browser=FakeBrowser();original=browser.page.goto
        def goto(url,**kwargs):
            browser.page.handlers['response'](SimpleNamespace(status=429,url='https://www.instagram.com/api/v1/feed/?token=SECRET'))
            return original(url,**kwargs)
        browser.page.goto=goto
        with self.assertRaises(InstagramCollectionError) as caught:
            PlaywrightInstagramProvider(browser_factory=lambda:browser).fetch_recent('one',2)
        self.assertEqual(caught.exception.reason,'rate_limit');self.assertNotIn('SECRET',str(caught.exception))
        self.assertEqual(len(browser.page.visits),1)

    def test_post_challenge_carries_only_previously_normalized_progress(self):
        browser=FakeBrowser();original=browser.page.goto
        def goto(url,**kwargs):
            result=original(url,**kwargs)
            if '/reel/Second/' in url:browser.page.url='https://www.instagram.com/challenge/SECRET/'
            return result
        browser.page.goto=goto
        with self.assertRaises(InstagramCollectionError) as caught:
            PlaywrightInstagramProvider(browser_factory=lambda:browser).fetch_recent('one',2)
        self.assertTrue(caught.exception.stop_run)
        self.assertEqual([x.external_id for x in caught.exception.partial_items],['Good'])
        self.assertEqual(len(browser.page.visits),3)

    def test_known_links_never_open_post_pages(self):
        browser=FakeBrowser()
        provider=PlaywrightInstagramProvider(browser_factory=lambda:browser)
        self.assertEqual(provider.fetch_recent('one',2,known_ids={'Good','Second'}),[])
        self.assertEqual(browser.page.visits,['https://www.instagram.com/one/'])
        browser.page.mouse.wheel.assert_not_called()

    def test_valid_empty_profile_returns_no_new_not_structure_error(self):
        browser=FakeBrowser();browser.page.links=[];browser.page.messages=['No Posts Yet']
        self.assertEqual(PlaywrightInstagramProvider(browser_factory=lambda:browser).fetch_recent('one',2),[])
        browser.page.mouse.wheel.assert_not_called()

    def test_timeout_is_not_misdiagnosed_as_login(self):
        browser=FakeBrowser();browser.page.goto=Mock(side_effect=PlaywrightTimeoutError('SECRET'))
        with self.assertRaises(InstagramCollectionError) as caught:
            PlaywrightInstagramProvider(browser_factory=lambda:browser).fetch_recent('one',2)
        self.assertEqual(caught.exception.reason,'timeout');self.assertFalse(caught.exception.stop_run)
        self.assertNotIn('SECRET',str(caught.exception))

    def test_private_and_missing_sources_do_not_signal_global_block(self):
        page=FakePage();page.messages=['This account is private']
        self.assertEqual(page_failure(page).reason,'private_account');self.assertFalse(page_failure(page).stop_run)
        page.messages=["Sorry, this page isn't available."]
        self.assertEqual(page_failure(page).reason,'unavailable')

    def test_instaloader_known_post_skips_metadata_access(self):
        provider=InstaloaderProvider();loader=Mock();provider.loader=loader
        profile=Mock(is_private=False);profile.get_posts.return_value=[SimpleNamespace(shortcode='known')]
        with patch('ingestion.providers.instagram.instaloader.Profile.from_username',return_value=profile):
            self.assertEqual(provider.fetch_recent('one',2,known_ids={'known'}),[])
        provider.close()

    def test_instaloader_authentication_challenge_is_classified_and_cached(self):
        loader = Mock()
        loader.login.side_effect = RuntimeError('checkpoint_required SECRET')
        with patch.dict('os.environ', {'INSTAGRAM_USERNAME': 'test', 'INSTAGRAM_PASSWORD': 'SECRET', 'INSTAGRAM_SESSION_FILE': ''}), patch(
            'ingestion.providers.instagram.instaloader.Instaloader', return_value=loader,
        ):
            provider = InstaloaderProvider()
            for _ in range(2):
                with self.assertRaises(InstagramCollectionError) as caught:
                    provider.fetch_recent('one', 1)
                self.assertEqual(caught.exception.reason, 'challenge')
                self.assertNotIn('SECRET', str(caught.exception))
            loader.login.assert_called_once()


class OfflineCommandTests(SimpleTestCase):
    """Exercise real command/state flow without a database or network service."""

    def setUp(self):
        isolate_instagram_schedule(self)
        self.sources = [SimpleNamespace(pk=i, username=f'source{i}', last_checked_at=None) for i in range(1, 22)]
        query = MagicMock()
        query.filter.return_value = query
        query.__iter__.side_effect = lambda: iter(self.sources)
        selector = patch('ingestion.management.commands.collect_instagram.SourceAccount.objects.filter', return_value=query)
        selector.start()
        self.addCleanup(selector.stop)
        self.collector = Mock()
        self.collector.fetch_items.return_value = [HardenedCommandTests.item('A')]
        factory = patch('ingestion.management.commands.collect_instagram.InstagramCollector', return_value=self.collector)
        self.factory = factory.start()
        self.addCleanup(factory.stop)
        store = patch('ingestion.management.commands.collect_instagram.Command._store', return_value=(1, 0))
        self.store = store.start()
        self.addCleanup(store.stop)
        sleeper = patch('ingestion.management.commands.collect_instagram.time.sleep')
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)

    def run_command(self, **kwargs):
        output = StringIO()
        arguments = ['--limit-posts', str(kwargs.pop('limit_posts'))] if 'limit_posts' in kwargs else []
        call_command('collect_instagram', *arguments, stdout=output, stderr=StringIO(), **kwargs)
        return output.getvalue()

    def test_cli_alias_and_plan_do_not_start_browser(self):
        self.assertIn('post limit: 1', self.run_command(limit_posts=1, limit_sources=1, plan=True))
        self.factory.assert_not_called()
        self.store.assert_not_called()

    def test_default_limit_and_durable_rotation_of_due_sources(self):
        self.assertIn('selected: 2; deferred: 19', self.run_command())
        self.factory.assert_called_once_with(limit=2)
        self.assertEqual([call.args[0].pk for call in self.collector.fetch_items.call_args_list], [1, 2])
        self.sleep.assert_called_once_with(60)
        with patch('django.utils.timezone.now', return_value=timezone.now() + timedelta(minutes=2)):
            self.run_command()
        self.assertEqual([call.args[0].pk for call in self.collector.fetch_items.call_args_list], [1, 2, 3, 4])

    def test_challenge_stops_immediately_and_preserves_partial_storage_path(self):
        error = failure('challenge')
        error.partial_items = (HardenedCommandTests.item('B'),)
        self.collector.fetch_items.side_effect = [[HardenedCommandTests.item('A')], error]
        output = StringIO()
        with self.assertRaises(CommandError):
            call_command('collect_instagram', limit_sources=3, stdout=output, stderr=StringIO())
        self.assertEqual(self.collector.fetch_items.call_count, 2)
        self.assertEqual([call.kwargs['completed'] for call in self.store.call_args_list], [True, False])
        self.assertIn('Raw items created: 2', output.getvalue())
        with self.assertRaises(CommandError):
            self.run_command()
        self.factory.assert_called_once()

    def test_interruption_preserves_reservation_and_closes_collector(self):
        self.sources = self.sources[:1]
        self.collector.fetch_items.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.run_command()
        self.collector.close.assert_called_once()
        self.assertIn('Instagram status: no_due_sources', self.run_command())
        self.factory.assert_called_once()
        self.store.assert_not_called()

    def test_orchestration_runs_telegram_once_after_instagram_startup_failure(self):
        output = StringIO()
        with patch('ingestion.management.commands.collect_sources.call_command', side_effect=[CommandError('SECRET'), None]) as dispatch:
            with self.assertRaises(CommandError) as caught:
                call_command('collect_sources', stdout=output, stderr=StringIO())
        self.assertEqual([call.args[0] for call in dispatch.call_args_list], ['collect_instagram', 'collect_telegram'])
        self.assertTrue(all('limit' not in call.kwargs for call in dispatch.call_args_list))
        self.assertIn('instagram=FAILED', str(caught.exception))
        self.assertIn('telegram=OK', str(caught.exception))
        self.assertNotIn('SECRET', output.getvalue() + str(caught.exception))


class HardenedCommandTests(TestCase):
    def setUp(self):
        self.directory=isolate_instagram_schedule(self)
        self.source=SourceAccount.objects.create(username='first')
        self.collector=Mock();self.collector.fetch_items.return_value=[self.item('A')]
        factory=patch('ingestion.management.commands.collect_instagram.InstagramCollector',return_value=self.collector)
        self.factory=factory.start();self.addCleanup(factory.stop)
        sleeper=patch('ingestion.management.commands.collect_instagram.time.sleep');self.sleep=sleeper.start();self.addCleanup(sleeper.stop)
        network=patch('requests.sessions.Session.request',side_effect=AssertionError('No live requests in tests'));network.start();self.addCleanup(network.stop)

    @staticmethod
    def item(external_id):
        return CollectedSourceItem(external_id=external_id,source_url=f'https://www.instagram.com/p/{external_id}/',raw_text='Private caption')

    def run_command(self,**options):
        arguments = ['--limit-posts', str(options.pop('limit_posts'))] if 'limit_posts' in options else []
        out=StringIO();call_command('collect_instagram',*arguments,stdout=out,stderr=StringIO(),**options);return out.getvalue()

    def test_default_attempts_only_two_of_twenty_one_and_two_posts(self):
        for i in range(20):SourceAccount.objects.create(username=f'source{i}')
        out=self.run_command()
        self.assertEqual(self.collector.fetch_items.call_count,2)
        self.factory.assert_called_once_with(limit=2)
        self.sleep.assert_called_once_with(60)
        self.assertIn('selected: 2; deferred: 19',out)
        self.assertEqual(RawSourceItem.objects.count(),2)

    def test_block_stops_remaining_accounts_and_preserves_both_source_and_post_progress(self):
        second=SourceAccount.objects.create(username='second');SourceAccount.objects.create(username='third')
        error=failure('challenge');error.partial_items=(self.item('B'),)
        self.collector.fetch_items.side_effect=[[self.item('A')],error]
        out=StringIO()
        with self.assertRaises(CommandError):call_command('collect_instagram',limit_sources=3,stdout=out,stderr=StringIO())
        self.assertEqual(self.collector.fetch_items.call_count,2)
        self.assertEqual(set(RawSourceItem.objects.values_list('external_id',flat=True)),{'A','B'})
        self.assertIsNone(SourceAccount.objects.get(pk=second.pk).last_checked_at)
        self.assertIn('Raw items created: 2',out.getvalue());self.assertIn('remaining selected sources not attempted',out.getvalue())
        with self.assertRaises(CommandError):self.run_command()
        self.assertEqual(self.factory.call_count,1)  # Cooldown refuses browser construction across runs.

    def test_no_new_uses_longer_schedule_and_recent_sources_never_fetch(self):
        self.collector.fetch_items.return_value=[]
        self.run_command();checked=SourceAccount.objects.get(pk=self.source.pk).last_checked_at
        self.assertIsNotNone(checked)
        state=json.loads((Path(self.directory)/'schedule.json').read_text())
        self.assertGreaterEqual(state['sources'][str(self.source.pk)]['next_check']-checked.timestamp(),43200)
        self.assertIn('Sources attempted: 0',self.run_command())
        self.collector.fetch_items.assert_called_once()

    def test_duplicate_after_later_due_run_preserves_first_snapshot(self):
        self.run_command();raw=RawSourceItem.objects.get();raw.extraction_status='ignored';raw.save()
        future=timezone.now()+timedelta(days=2)
        with patch('django.utils.timezone.now',return_value=future):self.run_command()
        self.assertEqual(RawSourceItem.objects.count(),1)
        raw.refresh_from_db();self.assertEqual(raw.extraction_status,'ignored')
        self.assertEqual(self.collector.fetch_items.call_count,2)

    def test_dry_run_has_no_db_writes_but_respects_persistent_pacing(self):
        self.run_command(dry_run=True)
        self.assertFalse(RawSourceItem.objects.exists());self.source.refresh_from_db();self.assertIsNone(self.source.last_checked_at)
        self.assertTrue((Path(self.directory)/'schedule.json').exists())
        self.run_command(dry_run=True);self.assertEqual(self.collector.fetch_items.call_count,1)

    def test_plan_and_source_selector_are_bounded_and_have_no_network_or_state_write(self):
        other=SourceAccount.objects.create(username='other')
        out=self.run_command(source_id=other.pk,limit_sources=1,limit_posts=1,plan=True)
        self.assertIn(f'Due source #{other.pk}',out)
        self.factory.assert_not_called();self.assertFalse((Path(self.directory)/'schedule.json').exists())
        self.run_command(source_id=other.pk,limit_sources=1,limit_posts=1)
        self.assertEqual(self.collector.fetch_items.call_args.args[0],other)
        self.factory.assert_called_once_with(limit=1)

    def test_invalid_limits_and_unknown_source_do_not_create_collector(self):
        for args in [{'limit_posts':4},{'limit_sources':0},{'source_id':99999}]:
            with self.assertRaises(CommandError):self.run_command(**args)
        self.factory.assert_not_called()

    def test_recent_database_checkpoint_is_honored_without_json_history(self):
        self.source.last_checked_at=timezone.now();self.source.save()
        self.run_command();self.factory.assert_not_called()

    def test_pacing_applies_to_other_sources_across_immediate_runs(self):
        self.run_command()
        SourceAccount.objects.create(username='new')
        out=self.run_command();self.assertIn('status: pacing',out)
        self.assertEqual(self.factory.call_count,1)

    def test_telegram_commits_and_status_survive_instagram_failure(self):
        SourceAccount.objects.create(username='second')
        telegram=SourceAccount.objects.create(username='channel',platform='telegram')
        self.collector.fetch_items.side_effect=[[self.item('A')],failure('rate_limit')]
        tg=Mock();tg.fetch_items.return_value=[CollectedSourceItem('2','https://t.me/channel/2',raw_text='Telegram text')]
        out=StringIO()
        with patch('ingestion.management.commands.collect_telegram.TelegramCollector',return_value=tg) as factory:
            with self.assertRaises(CommandError) as caught:call_command('collect_sources',stdout=out,stderr=StringIO())
        self.assertIn('instagram=FAILED',str(caught.exception));self.assertIn('telegram=OK',str(caught.exception))
        factory.assert_called_once_with(limit=5)
        self.assertEqual(RawSourceItem.objects.count(),2)
        self.assertTrue(RawSourceItem.objects.filter(source_account=telegram).exists())
        self.assertFalse('Private caption' in out.getvalue())
