#!/usr/bin/env python3
"""List placeholder locations without printing file contents or secret values."""
import argparse
from pathlib import Path

MARKERS = ('[CONTACT_EMAIL]', '[LEGAL_ENTITY_OR_DEVELOPER_NAME]')
EXCLUDED = {'.git','.aws','.ssh','.codex','.agents','.idea','.gradle','.kotlin','build','venv','venv_old','.venv','env','__pycache__','node_modules'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    default=Path(__file__).resolve().parents[2]
    parser.add_argument('--workspace-root',type=Path,default=default)
    args=parser.parse_args();root=args.workspace_root.resolve()
    if not (root/'rytm-backend').is_dir() or not (root/'rytm-android').is_dir():
        parser.error('Select the RYTM workspace containing both project directories.')
    print('# Rytm support/legal placeholder inventory — 2026-10-27\n')
    print('Source placeholders remain intentional defaults. Configure backend RYTM_SUPPORT_EMAIL / RYTM_LEGAL_ENTITY and Android RYTM_SUPPORT_EMAIL (or local.properties SUPPORT_EMAIL), then verify rendered pages/About. No values are invented. Test/reference occurrences are not operator content.\n')
    print('| File and line | Marker | Kind |\n|---|---|---|')
    count=0
    for file in sorted(root.rglob('*')):
        if any(part in EXCLUDED for part in file.relative_to(root).parts) or not file.is_file():continue
        if file.name=='PRODUCTION_PLACEHOLDERS.md' or file.suffix not in {'.md','.txt','.py','.kt','.kts','.xml'}:continue
        if file.stat().st_size>1000000:continue
        try:lines=file.read_text().splitlines()
        except (UnicodeError,OSError):continue
        rel=str(file.relative_to(root))
        if '/docs/legal/' in rel:kind='Production legal template: runtime operator configuration required'
        elif file.name=='AboutRytmDialog.kt':kind='Android intentional support fallback: configure support'
        elif file.name=='production_smoke_test.py':kind='Read-only smoke validation guard (intentional)'
        elif 'test' in rel.lower():kind='Test fixture/reference (intentional)'
        elif file.name=='public_views.py':kind='Escaped renderer substitution logic (intentional)'
        elif file.name==Path(__file__).name:kind='Inventory generator reference (intentional)'
        else:kind='Documentation reference'
        for number,line in enumerate(lines,1):
            for marker in MARKERS:
                if marker in line:
                    print(f'| [{rel}:{number}]({file}:{number}) | `{marker}` | {kind} |');count+=1
    print(f'\nTotal literal locations: {count}. This inventory does not claim deployment configuration or legal approval.')

if __name__=='__main__':main()
