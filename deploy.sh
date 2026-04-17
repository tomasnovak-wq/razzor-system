#!/bin/bash
# deploy.sh — automatické nasazení nové verze razzor-system
#
# Použití:
#   ./deploy.sh "krátký popis změny"
#   ./deploy.sh                        (interaktivně se zeptá na popis)
#
# Co skript dělá:
#   1. Zkontroluje, že je ve správné složce.
#   2. Zeptá se (nebo převezme z argumentu) na popis změny.
#   3. Ukáže git status a vyžádá si potvrzení.
#   4. Stáhne nejnovější verzi z GitHubu (git pull --rebase --autostash).
#   5. V souboru version.json zvýší číslo verze, přepíše autora na "Ludek",
#      popis na zadaný text a datum na aktuální.
#   6. Zacommituje všechny změny a pushne na GitHub.
#   7. Nasadí aplikaci na Fly.io (fly deploy -a razzor-system).

set -e  # kdekoli něco selže, skript skončí

# --- Konfigurace ------------------------------------------------------------

AUTOR="${AUTOR:-Ludek}"          # dá se přepsat: AUTOR=Tom ./deploy.sh ...
FLY_APP="razzor-system"
# ---------------------------------------------------------------------------

# Přejít do složky, kde leží tento skript (= kořen projektu)
cd "$(dirname "$0")"

# Kontrola, že jsme opravdu ve složce projektu
if [ ! -f "version.json" ] || [ ! -f "app.py" ]; then
    echo "Chyba: skript musí ležet v kořenové složce razzor-system."
    echo "Aktuální složka: $(pwd)"
    exit 1
fi

# Popis změny
if [ -n "$1" ]; then
    POPIS="$1"
else
    printf "Popis změny (co jsi upravil): "
    read -r POPIS
fi

if [ -z "$POPIS" ]; then
    echo "Chyba: popis změny nesmí být prázdný."
    exit 1
fi

# Ukázat, co se bude commitovat
echo ""
echo "==> Lokální změny, které se zacommitují:"
git status --short
if [ -z "$(git status --short)" ]; then
    echo "   (žádné změny — bude zveřejněna jen nová verze v version.json)"
fi

echo ""
printf "Pokračovat s commit + push + deploy? [y/N] "
read -r CONFIRM
if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
    echo "Přerušeno uživatelem."
    exit 0
fi

# Stáhnout nejnovější verzi (--autostash dočasně odloží tvé změny a zase je vrátí)
echo ""
echo "==> git pull --rebase --autostash"
git pull --rebase --autostash

# Zvýšit verzi, přepsat autora/popis/datum v version.json
echo ""
echo "==> Aktualizace version.json"
NEW_VERSION=$(POPIS_ENV="$POPIS" AUTOR_ENV="$AUTOR" python3 <<'PYEOF'
import json
import os
from datetime import datetime

popis = os.environ['POPIS_ENV']
autor = os.environ['AUTOR_ENV']

with open('version.json', 'r', encoding='utf-8') as f:
    v = json.load(f)

v['verze'] = int(v.get('verze', 0)) + 1
v['autor'] = autor
v['popis'] = popis
v['datum'] = datetime.now().strftime('%d.%m.%Y %H:%M')

with open('version.json', 'w', encoding='utf-8') as f:
    json.dump(v, f, ensure_ascii=False, indent=2)
    f.write('\n')

print(v['verze'])
PYEOF
)

echo "   Nová verze: v$NEW_VERSION"
echo "   Datum:      $(date +'%d.%m.%Y %H:%M')"
echo "   Autor:      $AUTOR"
echo "   Popis:      $POPIS"

# Commit
echo ""
echo "==> git add + git commit"
git add -A
git commit -m "v$NEW_VERSION: $POPIS"

# Push
echo ""
echo "==> git push"
git push

# Nasazení na Fly.io
echo ""
echo "==> fly deploy -a $FLY_APP"
fly deploy -a "$FLY_APP"

# Hotovo
echo ""
echo "-----------------------------------------------------------------"
echo "Hotovo. Verze v$NEW_VERSION je nasazená."
echo "Otevři: https://${FLY_APP}.fly.dev"
echo "-----------------------------------------------------------------"
