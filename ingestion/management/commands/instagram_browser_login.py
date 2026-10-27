from django.core.management.base import BaseCommand, CommandError

from ingestion.providers.instagram import InstagramCollectionError
from ingestion.providers.instagram_browser import InstagramBrowser, SESSION_REQUIRED, session_authenticated


class Command(BaseCommand):
    help = 'Open the dedicated persistent Chromium profile for manual Instagram login/verification.'

    def handle(self, *args, **options):
        browser = None
        try:
            browser = InstagramBrowser(headed_login=True).start()
            browser.page.goto('https://www.instagram.com/', wait_until='domcontentloaded')
            self.stdout.write(
                'Log into Instagram manually in the opened browser.\n'
                'Complete any verification if requested.\n'
                'When the Instagram home page is usable, return here and press Enter.'
            )
            input()
            if not session_authenticated(browser.context, browser.page):
                raise CommandError(SESSION_REQUIRED)
            self.stdout.write(self.style.SUCCESS('Instagram session appears authenticated. The local browser profile is retained.'))
        except CommandError:
            raise
        except InstagramCollectionError as error:
            raise CommandError(str(error)) from None
        except (EOFError, KeyboardInterrupt):
            raise CommandError('Manual login was not confirmed; the browser was closed.') from None
        except Exception:
            raise CommandError('Could not complete manual browser setup. Check Chromium installation, display, profile access and authentication.') from None
        finally:
            if browser:
                browser.close()
