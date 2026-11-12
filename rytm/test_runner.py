"""Recognize only databases actually installed by Django's test runner."""
from copy import deepcopy
from django.test.runner import DiscoverRunner


def database_identity(connection):
    return tuple(deepcopy(connection.settings_dict.get(key)) for key in
                 ('ENGINE', 'HOST', 'PORT', 'NAME', 'USER', 'PASSWORD', 'OPTIONS'))


def is_runner_test_database(connection):
    identity = getattr(connection, '_rytm_test_database_identity', None)
    return identity is not None and identity == database_identity(connection)


class RytmTestRunner(DiscoverRunner):
    def setup_databases(self, **kwargs):
        old_config = super().setup_databases(**kwargs)
        for connection, original_name, destroy in old_config:
            # Mirrors aren't independent test DBs; never bless an unchanged original DB.
            if destroy and connection.settings_dict['NAME'] != original_name:
                connection._rytm_test_database_identity = database_identity(connection)
        return old_config

    def teardown_databases(self, old_config, **kwargs):
        try:
            return super().teardown_databases(old_config, **kwargs)
        finally:
            for connection, _, _ in old_config:
                connection.__dict__.pop('_rytm_test_database_identity', None)
