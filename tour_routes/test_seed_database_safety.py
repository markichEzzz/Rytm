from types import SimpleNamespace
from unittest.mock import patch

from django.core.management.base import CommandError
from django.test import SimpleTestCase
from django.test.runner import DiscoverRunner

from rytm.test_runner import RytmTestRunner, database_identity, is_runner_test_database
from tour_routes.management.commands.seed_demo_route import Command


def connection(host='db', name='custom_ci_database'):
    return SimpleNamespace(settings_dict={'ENGINE': 'postgis', 'HOST': host, 'PORT': '5432', 'NAME': name, 'USER': 'test'})


class SeedDatabaseSafetyTests(SimpleTestCase):
    def allowed_before_queries(self, conn, allow=False):
        # Stop at transaction entry, proving the guard made its decision with no DB/network.
        with patch('tour_routes.management.commands.seed_demo_route.connections', {'default': conn}), \
             patch('tour_routes.management.commands.seed_demo_route.transaction.atomic', side_effect=RuntimeError('transaction reached')):
            with self.assertRaisesRegex(RuntimeError, 'transaction reached'):
                Command().handle(allow_nonlocal_database=allow)

    def test_local_development_is_allowed(self):
        for host in ('', 'localhost', '127.0.0.1', '::1', '/var/run/postgresql'):
            with self.subTest(host=host): self.allowed_before_queries(connection(host, 'development'))

    def test_test_prefix_or_test_settings_alone_never_bypass_remote_guard(self):
        for host in ('db', 'db.example.invalid', 'example.supabase.co', 'example.render.com'):
            conn = connection(host, 'test_postgres')
            conn.settings_dict['TEST'] = {'NAME': 'test_postgres'}
            with self.subTest(host=host), patch('tour_routes.management.commands.seed_demo_route.connections', {'default': conn}):
                with self.assertRaises(CommandError): Command().handle(allow_nonlocal_database=False)
            self.allowed_before_queries(conn, allow=True)

    def test_runner_marks_custom_generated_name_and_removes_proof_on_teardown(self):
        conn = connection()
        config = [(conn, 'original_database', True)]
        runner = RytmTestRunner()
        with patch.object(DiscoverRunner, 'setup_databases', return_value=config):
            self.assertEqual(runner.setup_databases(), config)
        self.assertTrue(is_runner_test_database(conn))
        self.allowed_before_queries(conn)
        with patch.object(DiscoverRunner, 'teardown_databases'):
            runner.teardown_databases(config)
        self.assertFalse(is_runner_test_database(conn))

    def test_changed_target_cannot_reuse_test_database_proof(self):
        conn = connection()
        conn._rytm_test_database_identity = database_identity(conn)
        for key, value in [('HOST', 'example.supabase.co'), ('NAME', 'production'), ('USER', 'another_user'), ('OPTIONS', {'hostaddr': '192.0.2.1'})]:
            with self.subTest(key=key), patch.dict(conn.settings_dict, {key: value}):
                self.assertFalse(is_runner_test_database(conn))
                with patch('tour_routes.management.commands.seed_demo_route.connections', {'default': conn}):
                    with self.assertRaises(CommandError): Command().handle(allow_nonlocal_database=False)

    def test_unchanged_database_is_not_marked_and_cleanup_survives_teardown_error(self):
        conn = connection()
        runner = RytmTestRunner()
        config = [(conn, conn.settings_dict['NAME'], True)]
        with patch.object(DiscoverRunner, 'setup_databases', return_value=config): runner.setup_databases()
        self.assertFalse(is_runner_test_database(conn))
        conn._rytm_test_database_identity = database_identity(conn)
        with patch.object(DiscoverRunner, 'teardown_databases', side_effect=RuntimeError('teardown')):
            with self.assertRaises(RuntimeError): runner.teardown_databases(config)
        self.assertFalse(is_runner_test_database(conn))
