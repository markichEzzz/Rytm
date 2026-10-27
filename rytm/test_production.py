import logging
import os
import runpy
import tempfile
from pathlib import Path
from unittest.mock import patch
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.http import JsonResponse
from django.test import SimpleTestCase, override_settings
from django.urls import path
from rytm.config import boolean, hosts, origins, database_url
from rytm.logging import SafeFormatter
from scripts.production_smoke_test import check, origin


def broken(request):
    raise RuntimeError('PRIVATE_ERROR_MARKER')

urlpatterns = [path('broken/', broken)]


class EnvironmentParserTests(SimpleTestCase):
    def test_hosts_trim_deduplicate(self):
        self.assertEqual(hosts(' api.example.org,api.example.org, admin.example.org ', True), ['api.example.org','admin.example.org'])
    def test_hosts_reject_url_port_wildcards_and_path(self):
        for bad in ['*','https://api.example.org','api.example.org:443','api.example.org/x','api.*.org','api..org','']:
            with self.subTest(bad=bad),self.assertRaises(ImproperlyConfigured):hosts(bad,True)
    def test_local_wildcard_supported(self):self.assertEqual(hosts('*'),['*'])
    def test_origins_strict(self):
        self.assertEqual(origins(' https://admin.example.org:8443 ',True),['https://admin.example.org:8443'])
        for bad in ['http://api.example.org','https://x.org/','https://user:pw@x.org','https://@x.org','https://x.org?q=x','https://*.org','https://x.org:bad','https://x.org#frag']:
            with self.subTest(bad=bad),self.assertRaises(ImproperlyConfigured):origins(bad,True)
    def test_local_http_origin_supported(self):self.assertEqual(origins('http://localhost:8000'),['http://localhost:8000'])
    def test_invalid_boolean_rejected(self):
        with self.assertRaises(ImproperlyConfigured):boolean('truthy','TEST_BOOL')
    def test_postgis_url_parses_credentials_port(self):
        db=database_url('postgresql://test:unused%40value@db.example.org:5433/rytm',production=True)
        self.assertEqual(db['ENGINE'],'django.contrib.gis.db.backends.postgis')
        self.assertEqual(db['PASSWORD'],'unused@value');self.assertEqual(db['PORT'],5433)
        self.assertEqual(db['OPTIONS']['sslmode'],'require');self.assertTrue(db['CONN_HEALTH_CHECKS']);self.assertEqual(db['CONN_MAX_AGE'],60)
    def test_local_database_url_does_not_force_ssl(self):
        db=database_url('postgres://test:unused@localhost/rytm',production=False)
        self.assertNotIn('sslmode',db['OPTIONS']);self.assertEqual(db['CONN_MAX_AGE'],0)
    def test_verify_full_not_downgraded(self):
        db=database_url('postgres://test:unused@db/rytm?sslmode=verify-full&sslrootcert=/run/secrets/root.crt',production=True)
        self.assertEqual(db['OPTIONS']['sslmode'],'verify-full');self.assertEqual(db['OPTIONS']['sslrootcert'],'/run/secrets/root.crt')
    def test_ssl_conflict_rejected(self):
        with self.assertRaises(ImproperlyConfigured):database_url('postgres://test:unused@db/rytm?sslmode=disable',production=True)
    def test_explicit_private_network_ssl_override(self):
        self.assertNotIn('sslmode',database_url('postgres://test:unused@db/rytm',production=True,ssl_required=False)['OPTIONS'])
    def test_invalid_url_never_echoes_secret(self):
        for url in ['mysql://test:PRIVATE_SECRET@db/rytm','not-a-url','postgres://test:PRIVATE_SECRET@db']:
            with self.assertRaises(ImproperlyConfigured) as error:database_url(url,production=True)
            self.assertNotIn('PRIVATE_SECRET',str(error.exception))
    def test_connection_lifetime_bounded(self):
        with self.assertRaises(ImproperlyConfigured):database_url('postgres://test:unused@db/rytm',production=True,conn_max_age='999999')


@override_settings(DEBUG=False,SECURE_SSL_REDIRECT=True,ALLOWED_HOSTS=['testserver'])
class ProductionHttpTests(SimpleTestCase):
    def test_health_https_minimal(self):self.assertEqual(self.client.get('/api/health/',secure=True).json(),{'status':'ok'})
    def test_http_redirects(self):self.assertEqual(self.client.get('/api/health/').status_code,301)
    @override_settings(SECURE_PROXY_SSL_HEADER=('HTTP_X_FORWARDED_PROTO','https'))
    def test_trusted_proxy_avoids_redirect_loop(self):self.assertEqual(self.client.get('/api/health/',HTTP_X_FORWARDED_PROTO='https').status_code,200)
    def test_legal_pages_https_no_auth(self):
        for page in ['privacy','terms','account-deletion']:
            self.assertEqual(self.client.get('/'+page+'/',secure=True).status_code,200)
    @override_settings(RYTM_SUPPORT_EMAIL='support@example.org',RYTM_LEGAL_ENTITY='Example <Operator>')
    def test_legal_operator_configuration_is_escaped(self):
        response=self.client.get('/privacy/',secure=True)
        self.assertContains(response,'support@example.org');self.assertContains(response,'Example &lt;Operator&gt;')
        self.assertNotContains(response,'[CONTACT_EMAIL]');self.assertNotContains(response,'<Operator>')
    def test_unknown_route_safe(self):
        response=self.client.get('/api/unknown/',secure=True)
        self.assertEqual(response.status_code,404);self.assertNotIn(b'Traceback',response.content)
    @override_settings(ROOT_URLCONF=__name__)
    def test_server_error_hidden_from_response_visible_in_logs(self):
        self.client.raise_request_exception=False
        with self.assertLogs('django.request',level='ERROR') as logs:
            response=self.client.get('/broken/',secure=True)
        self.assertEqual(response.status_code,500)
        self.assertNotIn(b'PRIVATE_ERROR_MARKER',response.content);self.assertNotIn(b'Traceback',response.content)
        self.assertTrue(logs.records[0].exc_info)
    def test_auth_error_json_safe(self):
        response=self.client.delete('/api/me/account/',secure=True)
        self.assertEqual(response.status_code,401);self.assertNotIn('Traceback',str(response.json()))
    def test_headers(self):
        response=self.client.get('/api/health/',secure=True)
        self.assertEqual(response.headers['X-Frame-Options'],'DENY');self.assertEqual(response.headers['X-Content-Type-Options'],'nosniff')
    def test_collectstatic_manifest_and_admin_assets(self):
        with tempfile.TemporaryDirectory() as directory,override_settings(STATIC_ROOT=directory,WHITENOISE_AUTOREFRESH=False,
                STORAGES={**settings.STORAGES,'staticfiles':{'BACKEND':'whitenoise.storage.CompressedManifestStaticFilesStorage'}}):
            call_command('collectstatic',interactive=False,verbosity=0)
            self.assertTrue((Path(directory)/'admin/css/base.css').exists())
            self.assertTrue((Path(directory)/'staticfiles.json').exists())
            # Fresh middleware indexes collected assets, just as a fresh worker does.
            from whitenoise.middleware import WhiteNoiseMiddleware
            from django.test import RequestFactory
            middleware=WhiteNoiseMiddleware(lambda request:JsonResponse({'fallback':True}))
            response=middleware(RequestFactory().get('/static/admin/css/base.css',secure=True))
            self.assertEqual(response.status_code,200);self.assertIn('text/css',response['Content-Type']);response.close()


class ProductionLoggingTests(SimpleTestCase):
    def test_secret_redaction_and_traceback_preserved(self):
        formatter=SafeFormatter('%(levelname)s %(message)s')
        try:raise RuntimeError('password=PRIVATE_PASSWORD api_key=PRIVATE_APIKEY')
        except RuntimeError:
            import sys
            record=logging.LogRecord('test',logging.ERROR,__file__,1,'Authorization: Bearer PRIVATE_BEARER',(),sys.exc_info())
        text=formatter.format(record)
        self.assertIn('Traceback',text);self.assertIn('RuntimeError',text)
        for value in ['PRIVATE_PASSWORD','PRIVATE_APIKEY','PRIVATE_BEARER']:self.assertNotIn(value,text)
    def test_json_and_database_credentials_redacted(self):
        from rytm.logging import redact
        text=redact('"id_token": "PRIVATE_ID", postgres://user:PRIVATE_DB@db/rytm')
        self.assertNotIn('PRIVATE_ID',text);self.assertNotIn('PRIVATE_DB',text)
    def test_environment_secret_redacted(self):
        from rytm.logging import redact
        with patch.dict(os.environ,{'OPENAI_API_KEY':'PRIVATE_CONFIG_VALUE'}):self.assertNotIn('PRIVATE_CONFIG_VALUE',redact('failed PRIVATE_CONFIG_VALUE'))
    def test_start_command_has_no_migrate(self):
        text=(settings.BASE_DIR/'start.sh').read_text()
        self.assertNotIn('manage.py migrate',text);self.assertIn('collectstatic',text);self.assertIn('exec gunicorn',text)
    def test_gunicorn_redacts_access_query_headers(self):
        config=runpy.run_path(str(settings.BASE_DIR/'gunicorn.conf.py'))
        self.assertNotIn('%(r)',config['access_log_format']);self.assertNotIn('%(q)',config['access_log_format'])
        self.assertNotIn('%(a)',config['access_log_format'])


class FakeResponse:
    def __init__(self,body,status=200,content_type='application/json'):
        self.body=body;self.status_code=status;self.headers={'Content-Type':content_type}
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def iter_content(self,size):yield self.body

class SmokeScriptTests(SimpleTestCase):
    def good(self,url,**kwargs):
        self.assertFalse(kwargs['allow_redirects']);self.assertTrue(kwargs['verify']);self.assertNotIn('Authorization',kwargs['headers'])
        if url.endswith('/api/health/'):return FakeResponse(b'{"status":"ok"}')
        if '/api/' in url:return FakeResponse(b'[]')
        return FakeResponse(b'<html>Rytm legal page</html>',content_type='text/html')
    @patch('scripts.production_smoke_test.requests.Session')
    def test_no_ambient_credentials_proxies_or_cookie_reuse(self,factory):
        session=factory.return_value.__enter__.return_value
        session.get.side_effect=self.good
        self.assertTrue(all(row[1] for row in check('https://api.example.org')))
        self.assertFalse(session.trust_env);self.assertIsNone(session.auth)
        self.assertEqual(session.cookies.clear.call_count,6)
    def test_six_get_checks_only(self):
        results=check('https://api.example.org',get=self.good)
        self.assertEqual(len(results),6);self.assertTrue(all(item[1] for item in results))
    def test_redirect_not_followed(self):self.assertFalse(any(row[1] for row in check('https://api.example.org',get=lambda *a,**kw:FakeResponse(b'',302))))
    def test_health_internals_rejected(self):
        self.assertFalse(check('https://api.example.org',get=lambda *a,**kw:FakeResponse(b'{"status":"ok","secret":"private"}'))[0][1])
    def test_unresolved_legal_placeholders_fail(self):
        rows=check('https://api.example.org',get=lambda *a,**kw:FakeResponse(b'<html>Rytm [CONTACT_EMAIL]</html>',content_type='text/html'))
        self.assertFalse(rows[-1][1])
    def test_unsafe_origins_rejected_before_network(self):
        for bad in ['http://api.example.org','https://user:secret@api.example.org','https://@api.example.org','https://api.example.org/path']:
            with self.assertRaises(ValueError):origin(bad)


class ProductionLauncherTests(SimpleTestCase):
    def launch(self, mode=None):
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'python').write_text('#!/bin/sh\n[ "$*" = "manage.py collectstatic --noinput" ] || exit 99\n')
            (root/'gunicorn').write_text('#!/bin/sh\n[ "$*" = "--config gunicorn.conf.py rytm.wsgi:application" ] || exit 98\nprintf "%s" "$ENVIRONMENT"\n')
            for file in root.iterdir():file.chmod(0o755)
            env={'PATH':directory}
            if mode is not None:env['ENVIRONMENT']=mode
            result=subprocess.run(['/bin/sh',str(settings.BASE_DIR/'start.sh')],env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            return result.stdout
    def test_production_launcher_defaults_fail_closed(self):self.assertEqual(self.launch(),'production')
    def test_explicit_developer_mode_remains_available(self):self.assertEqual(self.launch('development'),'development')
