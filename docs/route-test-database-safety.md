# Route test database safety — 2026-11-12

## Failure reproduced

The old seed command checked HOST only. A Docker service name such as `db` is non-local
under that check even when Django has created and switched to a dedicated test database.
Using the original command from HEAD and Django's standard runner reproduced exactly
**46 tests, 6 DemoRouteTests errors**, all from the non-local database guard.
For this reproduction libpq HOST was `db` but hostaddr was pinned to 127.0.0.1; no remote
server was contacted. New safety tests were excluded from the original baseline count.

## Fix

`rytm.test_runner.RytmTestRunner` extends DiscoverRunner without changing DB creation or
connection settings. Only after successful Django database setup, it records the identity
of each created test database whose name differs from its original database. It removes
this proof during teardown, including exceptional teardown. Mirrors/unchanged originals
are not marked. The identity includes engine, host, port, database, user, password and
connection OPTIONS, so changing the target cannot reuse the proof.

The seed allows an existing local host, the matching runner-created test connection, or
an explicit --allow-nonlocal-database flag. Neither test_* naming nor TEST.NAME alone
bypasses the guard. Custom Django test names work without relying on `test_postgres`.
An ordinary remote/Supabase/Render command still requires explicit approval. The runner
setting affects management test execution only; production connection configuration and
route behavior are unchanged. No new migration.

## Connection investigation

No route test or seed path creates threads, executors, extra database wrappers or raw
clients. Seeding uses Django's current connection and transaction.atomic context.
Instrumentation retained all physical PostgreSQL handles so garbage collection could not
hide their state. In both the original failing baseline and corrected Docker-host-context
run, every recorded Django handle was closed by teardown; both test databases were dropped.
Therefore the reported single lingering session's origin was **not reproduced or proven**.
It would be inaccurate to attribute it to a particular worker or claim a speculative fix.

A regression test now repeats successful seeds and a failing/rolled-back seed, verifies
the caller's physical connection is reused, clears PostgreSQL's stats snapshot and checks
no additional client sessions remain. PID values are discovered dynamically, not hardcoded.
No sleeps, retries, test-runner session killing or blanket connection cleanup were added.
No manual stale-session termination/drop was needed. Read-only post-run checks confirmed
zero test DB sessions and zero remaining test databases.

## Verification

Original baseline: 46 tests, 6 errors (expected reproduction).
Corrected non-local HOST with loopback hostaddr: 52 tests, 0 failures/errors, exit 0,
clean destruction. Final normal loopback run: 52 tests in 4.877 s, OK, clean destruction,
exit 0. The original 46 tests plus five safety tests and one connection regression pass.

Commands were equivalent to these management commands, using the existing virtualenv and
an explicit forced-loopback DATABASE_URL constructed from local docker-compose credentials:

```sh
python manage.py test tour_routes --noinput
python manage.py check
python manage.py makemigrations --check --dry-run
```

A temporary /tmp wrapper ran manage.py with these arguments after setting the loopback URL,
ENVIRONMENT=development and DATABASE_SSL_REQUIRE=false. The default .env DATABASE_URL was
not used because it can point at production. Check: no issues. Migration consistency:
No changes detected. Post-run pg_stat_activity test sessions: 0; pg_database test DBs: 0.

Files for this fix: rytm/settings.py, new rytm/test_runner.py,
tour_routes/management/commands/seed_demo_route.py, tour_routes/test_demo_route.py,
new tour_routes/test_seed_database_safety.py, and this report.
Existing unstaged route-source/docs changes were preserved. Android was not modified.
Nothing staged, committed, pushed or deployed; production was not accessed.
