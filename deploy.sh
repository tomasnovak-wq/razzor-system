#!/bin/bash
# deploy.sh — automatické nasazení nové verze razzor-system (Mac/Linux)
#
# Mac ekvivalent _NASTROJE/Nasadit na cloud.bat pro Windows.
# Volá stejný update_version.py jako ten .bat, aby byl formát version.json jednotný.
#
# Použití:
#   ./deploy.sh "krátký popis změny"
#   ./deploy.sh                        (interaktivně si vyžádá popis)
#
# Volitelně přepis autora:  AUTOR=Tomas ./deploy.sh "..."
#
# Co skript dělá:
#   1. Zkontroluje, že je v kořeni projektu.
#   2. Získá popis změny (z argumentu nebo interaktivně).
#   3. Ukáže git status a vyžádá si potvrzení.
#   4. Stáhne nejnovější kód z GitHubu (git pull --rebase --autostash).
#   5. Zavolá update_version.py "POPIS" "AUTOR" (zapíše version.json ve správném
#      formátu: verze, popis, autor, datum, cas, git_commit).
#   6. git add -A, git commit, git push.
#   7. fly deploy -a razzor-system.

set -e

# --- Konfigurace ------------------------------------------------------------

AUTOR="${AUTOR:-Ludek}"          # dá se přepsat: AUTOR=Tomas ./deploy.sh ...
FLY_APP="razzor-system"
# ---------------------------------------------------------------------------

# Přejít do složky, kde leží tento skript (= kořen projektu)
cd "$(dirname "$0")"

# Kontrola, že jsme opravdu v kořeni projektu
if [ ! -f "version.json" ] || [ ! -f "app.py" ] || [ ! -f "update_version.py" ]; then
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

# Připomínka CLAUDE.md
echo ""
echo "-----------------------------------------------"
echo " Chceš aktualizovat CLAUDE.md?"
echo " (doporučeno pokud jsi přidal novou funkci,"
echo "  změnil architekturu nebo workflow)"
echo "-----------------------------------------------"
printf "Aktualizovat CLAUDE.md? [y/N] "
read -r CLAUDE_UPDATE
if [ "$CLAUDE_UPDATE" = "y" ] || [ "$CLAUDE_UPDATE" = "Y" ]; then
    echo ""
    echo " >>> Otevři Cowork a řekni Claudovi:"
    echo "     'Aktualizuj CLAUDE.md podle aktuálních změn v projektu'"
    echo ""
    printf " Stiskni Enter až budeš mít CLAUDE.md hotový..."
    read -r
fi

# Ukázat, co se bude commitovat
echo ""
echo "==> Lokální změny, které se zacommitují:"
git status --short
if [ -z "$(git status --short)" ]; then
    echo "   (žádné změny v kódu — zveřejní se jen nová verze v version.json)"
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

# Zapsat novou verzi přes oficiální update_version.py (stejně jako Windows .bat)
echo ""
echo "==> python3 update_version.py \"$POPIS\" \"$AUTOR\""
python3 update_version.py "$POPIS" "$AUTOR"

# Přečíst zapsanou verzi pro commit message
NEW_VERSION=$(python3 -c "import json; print(json.load(open('version.json'))['verze'])")

# Commit
echo ""
echo "==> git add + git commit (v$NEW_VERSION: $POPIS)"
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
