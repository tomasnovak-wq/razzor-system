import json
import sys
import subprocess
from datetime import datetime

popis = sys.argv[1] if len(sys.argv) > 1 else 'aktualizace'
autor = sys.argv[2] if len(sys.argv) > 2 else 'Tomas'

ver_file = 'version.json'
try:
    with open(ver_file, encoding='utf-8') as f:
        data = json.load(f)
    ver = data.get('verze', 0) + 1
except:
    ver = 1

try:
    commit = subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD'], text=True).strip()
except:
    commit = ''

now = datetime.now()

data = {
    'verze': ver,
    'popis': popis,
    'autor': autor,
    'datum': now.strftime('%d.%m.%Y'),
    'cas': now.strftime('%H:%M'),
    'git_commit': commit
}

with open(ver_file, 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

print(f'Verze {ver} zapsana ({autor}, {now.strftime("%d.%m.%Y %H:%M")})')
