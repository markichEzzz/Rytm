from pathlib import Path
from django.conf import settings
from django.http import Http404, HttpResponse, JsonResponse
from django.utils.html import escape

PAGES = {'privacy': 'Політика конфіденційності Rytm', 'terms': 'Умови користування Rytm',
         'account-deletion': 'Видалення акаунта Rytm'}

def legal_page(request, page):
    if page not in PAGES:
        raise Http404
    text = (Path(settings.BASE_DIR) / 'docs' / 'legal' / (page + '.txt')).read_text(encoding='utf-8')
    text = text.replace('[CONTACT_EMAIL]', settings.RYTM_SUPPORT_EMAIL or '[CONTACT_EMAIL]')
    text = text.replace('[LEGAL_ENTITY_OR_DEVELOPER_NAME]', settings.RYTM_LEGAL_ENTITY or '[LEGAL_ENTITY_OR_DEVELOPER_NAME]')
    # Plain text is the maintainable source; escape every byte before HTML.
    return HttpResponse('<!doctype html><html lang="uk"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>' + PAGES[page] + '</title><style>body{max-width:48rem;margin:2rem auto;'
        'padding:0 1rem;font:1.1rem/1.65 system-ui;color:#241b21;background:#faf7f8}'
        'pre{white-space:pre-wrap;font:inherit;overflow-wrap:anywhere}a{color:#742449}</style>'
        '<nav><a href="/privacy/">Конфіденційність</a> · <a href="/terms/">Умови</a> · '
        '<a href="/account-deletion/">Видалення акаунта</a></nav><main><h1>' + PAGES[page] +
        '</h1><pre>' + escape(text) + '</pre></main></html>')

def health(request):
    return JsonResponse({'status': 'ok'})
