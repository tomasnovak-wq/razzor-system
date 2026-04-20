# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Co je tento systém

Výrobní systém pro firmu **Razzor Cases** — výrobce flight cases (přepravní kufry pro hudební vybavení). Systém řídí celý výrobní proces: typy casů a jejich kusovníky (BOM), sklad materiálů, výrobní zakázky, docházku, nabídky zákazníkům a faktury.

Stack: **Flask + SQLite** (SPA — jedna HTML stránka, veškerá logika v JavaScriptu).

## Spuštění lokálně

**Windows:**
```
SPUSTIT.bat          # spustí python app.py na http://localhost:5001
python app.py        # přímé spuštění
```

**Mac / Linux:**
```
python3 app.py       # přímé spuštění na http://localhost:5001
```

Server musí být restartován po každé změně Python souboru. Změny v `templates/app.html` se projeví okamžitě (bez restartu) — stačí refresh v prohlížeči.

Port je pevně 5001 (ne 5000) — na macOS kolidoval port 5000 s AirPlay Receiverem, proto je 5001 zvolený jednotně pro všechny platformy.

## Deployment na cloud

**Windows (Tomáš):**
```
_NASTROJE\Nasadit na cloud.bat        # git commit + push + fly deploy (Fly.io)
_NASTROJE\Stahni novou verzi.bat      # git pull (stáhnutí cizích změn)
_NASTROJE\Nahrat data na cloud.bat    # upload lokální DB na server
_NASTROJE\Stahni data z cloudu.bat    # download DB ze serveru
```

**Mac (Ludek):**
```
./deploy.sh "popis zmeny"             # ekvivalent Nasadit na cloud.bat
                                      # (volá stejný update_version.py → shodný formát version.json)
git pull --rebase --autostash         # stáhnutí cizích změn

# Stáhnout produkční databázi (pomocí flyctl):
fly ssh sftp get /data/system.db -a razzor-system && mv system.db data/system.db
```

Mac užívá `./deploy.sh` místo .bat skriptů. Oba skripty volají stejný `update_version.py "POPIS" "AUTOR"`, takže formát `version.json` je identický a štítek verze v aplikaci je konzistentní.

### Autentizace na GitHub (Mac)

GitHub od roku 2021 nepřijímá git push s heslem. Nejjednodušší řešení:
```
brew install gh
gh auth login                        # přihlášení přes prohlížeč, jednou provždy
```
Od té chvíle `git push` funguje bez dotazu na heslo.

Cloud URL: **https://razzor-system.fly.dev** (Fly.io, Frankfurt)

## Architektura

```
app.py              — Flask server, všechny API endpointy (~8000 řádků)
database.py         — schéma, init_db(), auto_migrate(), pomocné funkce
templates/app.html  — celá SPA frontend (~8000 řádků, vanilla JS + Tailwind)
pdf_faktura.py      — generování PDF faktur (ReportLab)
static/             — tailwind.min.css, logo.svg
data/system.db      — SQLite databáze (lokálně)
/data/system.db     — SQLite databáze (na Fly.io, persistent volume)
```

### Databáze

`database.py` určuje cestu k DB:
- Pokud existuje adresář `/data` → `/data/system.db` (Fly.io)
- Jinak → `data/system.db` (lokální vývoj)

Schéma se nikdy neupravuje ručně. Nové sloupce a tabulky se přidávají výhradně přes funkci `auto_migrate()` v `database.py` — ta se volá automaticky při každém startu serveru. **Nikdy nemazat existující tabulky ani sloupce.**

Klíčové tabulky:
- `materialy` — katalog materiálů (desky, profily, HW)
- `typy_casu` — typy casů (HN221250 apod.)
- `kusovniky` — BOM: kolik jakého materiálu jde do každého typu casu
- `zakazky` — výrobní zakázky
- `sklad` + `pohyby_skladu` — stavy skladu a pohyby
- `nabidky` — cenové nabídky zákazníkům
- `faktury` — vystavené faktury
- `dochazka` / `dochazka_zaznamy` — docházka pracovníků
- `fifo_davky` — FIFO evidence nákupních cen

### Výrobní zakázky — workflow stavů

Stavy zakázky (v tomto pořadí): **Čeká → CNC hotovo → Výroba → Hotovo → Zkontrolováno → Expedováno**

Stav „Zrušeno" neexistuje — zrušená zakázka se fyzicky smaže (`DELETE /api/zakazky/<id>`).

Stav lze měnit přímo v seznamu zakázek přes inline `<select>` v řádku — bez otevírání dialogu. Změna se projeví okamžitě (cache se aktualizuje v JS bez reloadu).

Bannery pod řádkem zakázky (viditelné v hlavním seznamu):
- `Hotovo` → žlutý: „VYČKEJTE NA KONTROLU CASE"
- `Zkontrolováno` + `foceni=1` → černý: „ODNESTE NA FOCENÍ"
- `Zkontrolováno` + `fakturovano=1` → zelený: „ODNESTE NEPRODLENĚ NA PŘEJÍMKU"
- `Zkontrolováno` + `fakturovano=0` → červený: „VYČKEJTE NA VYSTAVENÍ FAKTURY"

Sloupec `foceni` (INTEGER DEFAULT 0) v tabulce `zakazky` — zaškrtávátko přímo v řádku seznamu.

### Fakturace — blokace odchylkami

Zakázku s otevřenou odchylkou (stav = `Nová` v tabulce `odchylky_karty`) nelze vyfakturovat. Blokace je na dvou úrovních: frontend (zakázka je zašedlá s badge ⚠ Odchylka, checkbox disabled) i backend (endpoint `POST /api/faktury` vrátí 400 pokud zakázka má otevřenou odchylku).

### Stav skladu — důležité

Správný výpočet disponibilního množství je vždy `COALESCE(s.naskladneno - s.pouzito, 0)`. Sloupec `skutecny_stav` obsahuje fyzicky napočítaný stav z inventury — může být 0 i když materiál na skladě je. Nikdy nepoužívat `skutecny_stav` jako hlavní ukazatel dostupnosti.

### Frontend (app.html)

Čistý JavaScript bez frameworku. Navigace přes funkci `navigate('sekce')`. API volání přes helper `api(url, method, body)`. Modály se otevírají/zavírají ručně přes `show()`/`hide()`.

Detekce prostředí probíhá v JS podle `window.location.hostname`:
- `localhost` / `127.0.0.1` / `192.168.x.x` → modrá sidebar + badge "LOCAL"
- Cokoliv jiného → červená sidebar + badge "CLOUD"

### API konvence

Všechny endpointy vrací JSON. Chyby vracejí `{"error": "..."}` s HTTP 4xx/5xx. Server má globální error handler — nikdy nevrací HTML chybové stránky.

Příklady endpointů:
- `GET /api/materialy?q=...&typ=...` — seznam materiálů
- `GET /api/typy-casu` — seznam typů casů
- `GET /api/typy-casu/<id>/bom` — kusovník typu
- `GET /api/zakazky` — výrobní zakázky
- `GET /api/verze` — aktuální verze systému (z version.json)
- `POST /admin/upload-db` + `GET /admin/download-db` — synchronizace DB (secret: `razzor-upload-2026`)

### Verze

Při nasazení `_NASTROJE\Nasadit na cloud.bat` se volá `python update_version.py "popis" "autor"`, který zapíše `version.json`. Endpoint `/api/verze` tato data zobrazuje ve spodním pravém rohu aplikace.

## Spolupráce více vývojářů

Kód sdílíme přes **GitHub** (jeden repozitář). Na Fly.io může nasazovat každý, kdo má přístup do Fly.io organizace.

Workflow:
1. Udělej změny lokálně
2. `Nasadit na cloud.bat` — commitne, pushne, nasadí
3. Druhý vývojář spustí `Stahni novou verzi.bat` (git pull) + restartuje server

Data (databáze) jsou sdílená na Fly.io. Lokální DB a cloudová DB se nesynchronizují automaticky — je třeba ručně použít `Nahrat data na cloud.bat` / `Stahni data z cloudu.bat`.
