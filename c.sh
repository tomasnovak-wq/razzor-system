#!/bin/bash
# c.sh — rychlý commit + push (bez deploy)
#
# Používá se po změnách v Cowork session:
#   ./c.sh          → push bez deploy (jen git)
#   ./c.sh deploy   → push + fly deploy
#
# Odstraní git lock soubory které zanechává Cowork sandbox (není třeba sudo).

set -e
cd "$(dirname "$0")"

# Smaž lock soubory zanechané sandboxem (vlastní soubory — sudo není potřeba)
rm -f .git/HEAD.lock .git/index.lock 2>/dev/null || true

# Zjisti zprávu z version.json (Claude už ji zapsal přes update_version.py)
MSG=$(python3 -c "
import json
v = json.load(open('version.json'))
print(f\"v{v['verze']}: {v.get('popis','aktualizace')}\")
" 2>/dev/null || echo "aktualizace")

echo "==> git add -A"
git add -A

echo "==> git commit: $MSG"
git commit -m "$MSG"

echo "==> git push"
git push

if [ "$1" = "deploy" ]; then
    echo "==> fly deploy"
    fly deploy -a razzor-system
    echo ""
    echo "Hotovo. https://razzor-system.fly.dev"
else
    echo ""
    echo "Hotovo. Pro nasazení na cloud spusť: fly deploy -a razzor-system"
fi
