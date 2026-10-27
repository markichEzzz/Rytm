from datetime import timedelta
from io import StringIO
import os
from unittest.mock import Mock, patch

import instaloader
from django.core.management import call_command, CommandError
from django.test import TestCase, SimpleTestCase
from django.utils import timezone

from .collectors import CollectedSourceItem
from .models import SourceAccount, RawSourceItem, EventCandidate
from .providers.instagram import InstagramCollector, InstaloaderProvider, InstagramCollectionError


class CollectionCommandTests(TestCase):
    def setUp(self):
        self.source = SourceAccount.objects.create(username='festrepublic')
        # Guard all tests against accidental HTTP, even if a provider mock is removed later.
        network = patch('requests.sessions.Session.request', side_effect=AssertionError('Tests must not access the network'))
        network.start()
        self.addCleanup(network.stop)
        self.collector = Mock()
        self.collector.fetch_items.return_value = [self.item()]
        factory = patch('ingestion.management.commands.collect_instagram.InstagramCollector', return_value=self.collector)
        self.factory = factory.start()
        self.addCleanup(factory.stop)
        delay = patch('ingestion.management.commands.collect_instagram.time.sleep')
        self.sleep = delay.start()
        self.addCleanup(delay.stop)

    def item(self, external_id='post-1', **overrides):
        fields = dict(external_id=external_id, source_url=f'https://www.instagram.com/p/{external_id}/',
                      raw_text='Original private caption', published_at=timezone.now() - timedelta(days=1),
                      raw_image_url='https://example.com/internal.jpg', raw_metadata={'provider': 'test'})
        fields.update(overrides)
        return CollectedSourceItem(**fields)

    def run_collection(self, **options):
        stdout, stderr = StringIO(), StringIO()
        call_command('collect_instagram', stdout=stdout, stderr=stderr, **options)
        return stdout.getvalue(), stderr.getvalue()

    def test_new_post_persisted_as_new_raw_only(self):
        output, _ = self.run_collection()
        raw = RawSourceItem.objects.get()
        self.assertEqual(raw.source_account, self.source)
        self.assertEqual(raw.extraction_status, 'new')
        self.assertEqual(raw.raw_text, 'Original private caption')
        self.assertEqual(raw.raw_image_url, 'https://example.com/internal.jpg')
        self.assertIn('New RawSourceItems: 1', output)
        self.assertIn('Existing items skipped: 0', output)
        self.assertIn('Failures: 0', output)
        self.assertIn('Sources attempted: 1', output)
        self.assertFalse(EventCandidate.objects.exists())
        from events.models import Event
        self.assertFalse(Event.objects.exists())
        self.collector.close.assert_called_once()

    def test_repeated_run_idempotent_preserves_snapshot_and_status(self):
        self.run_collection()
        raw = RawSourceItem.objects.get()
        raw.extraction_status = 'ignored'
        raw.save()
        self.collector.fetch_items.return_value = [self.item(raw_text='Changed caption', raw_metadata={'changed': True})]
        output, _ = self.run_collection()
        self.assertEqual(RawSourceItem.objects.count(), 1)
        raw.refresh_from_db()
        self.assertEqual(raw.raw_text, 'Original private caption')
        self.assertEqual(raw.raw_metadata, {'provider': 'test'})
        self.assertEqual(raw.extraction_status, 'ignored')
        self.assertIn('Existing items skipped: 1', output)

    def test_disabled_and_non_instagram_sources_skipped(self):
        self.source.enabled = False
        self.source.save()
        SourceAccount.objects.create(username='other', platform='telegram')
        output, _ = self.run_collection()
        self.assertIn('Processed sources: 0', output)
        self.collector.fetch_items.assert_not_called()
        self.assertFalse(RawSourceItem.objects.exists())

    def test_username_requires_enabled_configured_source(self):
        for username in ('arbitrary', '@other'):
            with self.assertRaises(CommandError):
                self.run_collection(username=username)
        self.source.enabled = False
        self.source.save()
        with self.assertRaises(CommandError):
            self.run_collection(username='festrepublic')
        self.collector.fetch_items.assert_not_called()

    def test_username_filters_and_limit_forwarded(self):
        SourceAccount.objects.create(username='other')
        self.run_collection(username='@FESTREPUBLIC', limit=3)
        self.factory.assert_called_once_with(limit=3)
        self.collector.fetch_items.assert_called_once_with(self.source)
        self.sleep.assert_not_called()

    def test_invalid_limits_fail_before_collection(self):
        for limit in (-1, 0, 21):
            with self.assertRaises(CommandError):
                self.run_collection(limit=limit)
        self.factory.assert_not_called()

    def test_failure_continues_other_sources_and_sanitizes_logs(self):
        other = SourceAccount.objects.create(username='other')
        previous = timezone.now() - timedelta(days=1)
        self.source.last_checked_at = previous
        self.source.save()
        self.collector.fetch_items.side_effect = [RuntimeError('password=PRIVATESECRET cookie=PRIVATECOOKIE'), [self.item()]]
        stdout, stderr = StringIO(), StringIO()
        with self.assertLogs('ingestion.management.commands.collect_instagram', level='WARNING') as logs:
            with self.assertRaises(CommandError):
                call_command('collect_instagram', stdout=stdout, stderr=stderr)
        self.assertNotIn('PRIVATE', stdout.getvalue() + stderr.getvalue() + str(logs.output))
        self.assertIn('Processed sources: 2', stdout.getvalue())
        self.assertIn('Failures: 1', stdout.getvalue())
        self.assertEqual(RawSourceItem.objects.get().source_account, other)
        self.source.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(self.source.last_checked_at, previous)
        self.assertIsNotNone(other.last_checked_at)
        self.assertTrue(self.source.enabled)
        self.sleep.assert_called_once_with(2)

    def test_dry_run_no_database_changes(self):
        previous = timezone.now() - timedelta(days=1)
        self.source.last_checked_at = previous
        self.source.save()
        output, _ = self.run_collection(dry_run=True)
        self.assertIn('dry-run', output)
        self.assertIn('Fetched items: 1', output)
        self.assertIn('New RawSourceItems: 0', output)
        self.assertFalse(RawSourceItem.objects.exists())
        self.source.refresh_from_db()
        self.assertEqual(self.source.last_checked_at, previous)

    def test_empty_success_updates_last_checked(self):
        self.collector.fetch_items.return_value = []
        self.run_collection()
        self.source.refresh_from_db()
        self.assertIsNotNone(self.source.last_checked_at)
        self.assertFalse(RawSourceItem.objects.exists())

    def test_source_transaction_rolls_back_invalid_item(self):
        self.collector.fetch_items.return_value = [self.item(), self.item(external_id='bad', source_url='invalid')]
        with self.assertLogs('ingestion.management.commands.collect_instagram', level='WARNING'):
            with self.assertRaises(CommandError):
                self.run_collection()
        self.assertFalse(RawSourceItem.objects.exists())
        self.source.refresh_from_db()
        self.assertIsNone(self.source.last_checked_at)

    def test_disable_during_network_fetch_prevents_persistence(self):
        def fetch(source):
            SourceAccount.objects.filter(pk=source.pk).update(enabled=False)
            return [self.item()]
        self.collector.fetch_items.side_effect = fetch
        with self.assertLogs('ingestion.management.commands.collect_instagram', level='WARNING'):
            with self.assertRaises(CommandError):
                self.run_collection()
        self.assertFalse(RawSourceItem.objects.exists())

    def test_oversized_provider_response_rejected(self):
        self.collector.fetch_items.return_value = [self.item('one'), self.item('two')]
        with self.assertLogs('ingestion.management.commands.collect_instagram', level='WARNING'):
            with self.assertRaises(CommandError):
                self.run_collection(limit=1)
        self.assertFalse(RawSourceItem.objects.exists())

    def test_collector_enforces_whitelist_even_when_called_directly(self):
        provider = Mock()
        collector = InstagramCollector(limit=3, provider=provider)
        provider.fetch_recent.return_value = [self.item()]
        self.assertEqual(len(collector.fetch_items(self.source)), 1)
        provider.fetch_recent.assert_called_once_with('festrepublic', 3)
        self.source.enabled = False
        self.source.save()
        for source in (self.source, SourceAccount(username='not-configured')):
            with self.assertRaises(InstagramCollectionError):
                collector.fetch_items(source)
        self.assertEqual(provider.fetch_recent.call_count, 1)


class InstagramProviderTests(SimpleTestCase):
    def setUp(self):
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        network = patch('requests.sessions.Session.request', side_effect=AssertionError('Tests must not access the network'))
        network.start()
        self.addCleanup(network.stop)
        self.loader = Mock()
        factory = patch('ingestion.providers.instagram.instaloader.Instaloader', return_value=self.loader)
        self.factory = factory.start()
        self.addCleanup(factory.stop)
        self.profile = Mock(is_private=False)
        profiles = patch('ingestion.providers.instagram.instaloader.Profile.from_username', return_value=self.profile)
        self.profiles = profiles.start()
        self.addCleanup(profiles.stop)
        self.provider = InstaloaderProvider()
        self.addCleanup(self.provider.close)

    def post(self, shortcode='abc', age_days=1):
        return Mock(shortcode=shortcode, date_utc=(timezone.now() - timedelta(days=age_days)).replace(tzinfo=None),
                    caption='Caption', url='https://example.com/source.jpg', mediaid=123, typename='GraphImage', is_video=False)

    def test_maps_metadata_and_stops_at_scan_limit(self):
        consumed = []
        def posts():
            for post in (self.post('pinned', 90), self.post('recent'), self.post('never-consumed')):
                consumed.append(post.shortcode)
                yield post
        self.profile.get_posts.return_value = posts()
        items = self.provider.fetch_recent('festrepublic', 2)
        self.assertEqual(consumed, ['pinned', 'recent'])
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.external_id, 'recent')
        self.assertEqual(item.source_url, 'https://www.instagram.com/p/recent/')
        self.assertTrue(timezone.is_aware(item.published_at))
        self.assertEqual(item.raw_text, 'Caption')
        self.assertEqual(item.raw_image_url, 'https://example.com/source.jpg')
        self.assertEqual(item.raw_metadata['media_id'], '123')
        config = self.factory.call_args.kwargs
        self.assertEqual(config['max_connection_attempts'], 1)
        self.assertEqual(config['request_timeout'], 20)
        self.assertFalse(config['download_pictures'])
        self.assertIn(429, config['fatal_status_codes'])
        self.loader.download_post.assert_not_called()
        self.loader.login.assert_not_called()

    def test_rejects_private_account(self):
        self.profile.is_private = True
        with self.assertRaisesMessage(InstagramCollectionError, 'Private accounts'):
            self.provider.fetch_recent('festrepublic', 3)
        self.profile.get_posts.assert_not_called()

    def test_rate_block_no_retry_or_further_network_for_run(self):
        self.profiles.side_effect = instaloader.AbortDownloadException('"429 Too Many Requests" secret-response')
        for username in ('one', 'two'):
            with self.assertRaisesMessage(InstagramCollectionError, 'blocked access') as caught:
                self.provider.fetch_recent(username, 3)
            self.assertNotIn('secret-response', str(caught.exception))
        self.profiles.assert_called_once()

    def test_missing_account_does_not_block_next_source(self):
        self.profiles.side_effect = [instaloader.AbortDownloadException('"404 Not Found"'), self.profile]
        self.profile.get_posts.return_value = [self.post()]
        with self.assertRaisesMessage(InstagramCollectionError, 'not found'):
            self.provider.fetch_recent('missing', 3)
        self.assertEqual(len(self.provider.fetch_recent('other', 3)), 1)

    def test_exception_text_never_exposed(self):
        self.profiles.side_effect = RuntimeError('password=SECRET cookies=SECRET')
        with self.assertRaises(InstagramCollectionError) as caught:
            self.provider.fetch_recent('festrepublic', 3)
        self.assertNotIn('SECRET', str(caught.exception))

    def test_session_from_explicit_environment_without_password_login(self):
        os.environ.update(INSTAGRAM_USERNAME='login-user', INSTAGRAM_SESSION_FILE='/tmp/trusted-session')
        self.profile.get_posts.return_value = []
        self.provider.fetch_recent('festrepublic', 3)
        self.loader.load_session_from_file.assert_called_once_with('login-user', '/tmp/trusted-session')
        self.loader.login.assert_not_called()
        self.loader.save_session_to_file.assert_not_called()

    def test_password_from_environment_login_only_once(self):
        os.environ.update(INSTAGRAM_USERNAME='login-user', INSTAGRAM_PASSWORD='SECRET')
        self.profile.get_posts.return_value = []
        self.provider.fetch_recent('one', 3)
        self.provider.fetch_recent('two', 3)
        self.loader.login.assert_called_once_with('login-user', 'SECRET')

    def test_authentication_failure_sanitized_and_cached(self):
        os.environ.update(INSTAGRAM_USERNAME='login-user', INSTAGRAM_PASSWORD='SECRET')
        self.loader.login.side_effect = RuntimeError('SECRET cookie-body')
        for username in ('one', 'two'):
            with self.assertRaisesMessage(InstagramCollectionError, 'authentication/session failed') as caught:
                self.provider.fetch_recent(username, 3)
            self.assertNotIn('SECRET', str(caught.exception))
        self.loader.login.assert_called_once()
        self.profiles.assert_not_called()
