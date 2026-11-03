"""Test-only schedule isolation; never use the operator's collection state."""
from tempfile import TemporaryDirectory
from django.test import override_settings


def isolate_instagram_schedule(test):
    directory = TemporaryDirectory()
    test.addCleanup(directory.cleanup)
    settings = override_settings(INSTAGRAM_COLLECTION_STATE_DIR=directory.name)
    settings.enable()
    test.addCleanup(settings.disable)
    return directory.name
