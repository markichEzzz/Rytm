"""Durable, locked Instagram-only schedule. No cookies, captions or credentials."""
import fcntl
import json
import math
import os
from pathlib import Path
import tempfile

from django.utils import timezone
from .providers.instagram import InstagramCollectionError


class InstagramSchedule:
    def __init__(self, policy):
        self.policy = policy
        self.directory = policy.state_dir
        self.path = self.directory / 'schedule.json'
        self.lock = None
        self.data = {'version': 1, 'sources': {}, 'global_until': 0, 'global_reason': '', 'pacing_until': 0}

    def __enter__(self):
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.lock = os.open(self.directory / 'collection.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.path.exists():
                fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd) as stream:
                    if os.fstat(stream.fileno()).st_size > 1024 * 1024:
                        raise ValueError
                    data = json.load(stream)
                if data.get('version') != 1 or not isinstance(data.get('sources'), dict):
                    raise ValueError
                for key in ('global_until', 'pacing_until'):
                    self._timestamp(data.get(key))
                if not isinstance(data.get('global_reason'), str):
                    raise ValueError
                for source_id, record in data['sources'].items():
                    if not source_id.isdigit() or not isinstance(record, dict):
                        raise ValueError
                    self._timestamp(record.get('next_check'))
                    if type(record.get('failures')) is not int or not 0 <= record['failures'] <= 20:
                        raise ValueError
                self.data = data
            return self
        except BlockingIOError:
            self.__exit__(None, None, None)
            raise InstagramCollectionError('Another Instagram run holds the schedule lock; no requests made.', reason='locked') from None
        except Exception:
            self.__exit__(None, None, None)
            raise InstagramCollectionError('Instagram schedule is unavailable or invalid; no requests made. Check state directory permissions/format.', reason='schedule') from None

    @staticmethod
    def _timestamp(value):
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 32503680000:
            raise ValueError

    def __exit__(self, *_):
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None

    def save(self):
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(dir=self.directory, prefix='.schedule-')
            with os.fdopen(fd, 'w') as stream:
                json.dump(self.data, stream, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception:
            raise InstagramCollectionError('Could not persist Instagram schedule; stopping without further requests.', reason='schedule', stop_run=True) from None
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def due_at(self, source):
        database_due = source.last_checked_at.timestamp() + self.policy.success_interval if source.last_checked_at else 0
        return max(database_due, self.data['sources'].get(str(source.pk), {}).get('next_check', 0))

    def reserve(self, source):
        # Persist BEFORE network work: crashes/interruptions cannot immediately hammer this source.
        old = self.data['sources'].get(str(source.pk), {})
        self.data['sources'][str(source.pk)] = {'next_check': timezone.now().timestamp() + self.policy.failure_interval,
                                                'failures': old.get('failures', 0)}
        self.data['pacing_until'] = timezone.now().timestamp() + self.policy.source_delay
        self.save()

    def complete(self, source, new_count):
        interval = self.policy.success_interval if new_count else max(self.policy.success_interval, self.policy.empty_interval)
        self.data['sources'][str(source.pk)] = {'next_check': timezone.now().timestamp() + interval, 'failures': 0}
        self.pace()
        self.save()

    def failed(self, source, reason, stop_run):
        failures = min(20, self.data['sources'].get(str(source.pk), {}).get('failures', 0) + 1)
        delay = min(self.policy.failure_interval * 2 ** min(failures - 1, 4), max(self.policy.failure_interval, 21600))
        if reason in ('private_account', 'not_found', 'unavailable'):
            delay = max(delay, self.policy.empty_interval)
        self.data['sources'][str(source.pk)] = {'next_check': timezone.now().timestamp() + delay, 'failures': failures}
        if stop_run:
            self.data['global_until'] = max(self.data['global_until'], timezone.now().timestamp() + self.policy.block_cooldown)
            self.data['global_reason'] = reason
        self.pace()
        self.save()

    def pace(self):
        self.data['pacing_until'] = timezone.now().timestamp() + self.policy.source_delay
