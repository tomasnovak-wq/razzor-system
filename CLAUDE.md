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

### Workflow při spolupráci s Claude (Cowork)

Claude (v Cowork módu) může autonomně editovat soubory, commitovat a volat `update_version.py`. **Nemůže** pushovat na GitHub ani nasazovat na Fly.io — jeho sandbox nemá síťový přístup ven.

Workflow:
1. Claude udělá změny + spustí `update_version.py "popis" "Ludek"` + commitne
2. Ludek spustí v terminálu: `git push && fly deploy`

GitHub token je uložen v `.git/config` (remote URL) — `git push` na Macu proto nevyžaduje heslo.

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

**Důležité:** `auto_migrate()` a `init_db()` se volají na úrovni modulu v `app.py` (hned po vytvoření `app`), **ne** jen uvnitř `if __name__ == '__main__':`. Díky tomu migrace proběhne i pod gunicorn na Fly.io. Pokud bys viděl chybu `no such column` po uploadu DB na cloud, stačí restartovat Fly.io machine příkazem `fly machine restart -a razzor-system` — `auto_migrate()` přidá chybějící sloupce při dalším startu.

Klíčové tabulky:
- `materialy` — katalog materiálů (desky, profily, HW)
- `typy_casu` — typy casů (HN221250 apod.)
- `kusovniky` — BOM: kolik jakého materiálu jde do každého typu casu
- `zakazky` — výrobní zakázky (nové sloupce: `odeslano_do_vyroby`, `destinace`, `poznamka_cnc_operator`)
- `sklad` + `pohyby_skladu` — stavy skladu a pohyby
- `nabidky` — cenové nabídky zákazníkům
- `faktury` — vystavené faktury
- `dochazka` / `dochazka_zaznamy` — docházka pracovníků
- `fifo_davky` — FIFO evidence nákupních cen
- `_migrations` — tracking tabulka pro jednorázové datové migrace (pattern: `SELECT 1 FROM _migrations WHERE name='...'`)

### Výrobní zakázky — workflow stavů

Stavy zakázky (v tomto pořadí): **Čeká → CNC hotovo → Výroba → Hotovo → Zkontrolováno → Expedováno**

Stav „Zrušeno" neexistuje — zrušená zakázka se fyzicky smaže (`DELETE /api/zakazky/<id>`).

Stav lze měnit přímo v seznamu zakázek přes inline `<select>` v řádku — bez otevírání dialogu. Změna se projeví okamžitě (cache se aktualizuje v JS bez reloadu).

Bannery pod řádkem zakázky (viditelné v hlavním seznamu zakázek v kanceláři):
- `Zkontrolováno` + `foceni=1` → černý: „ODNESTE NA FOCENÍ"
- `Zkontrolováno` + `fakturovano=1` → zelený: „ODNESTE NEPRODLENĚ NA PŘEJÍMKU"
- `Zkontrolováno` + `fakturovano=0` → červený: „VYČKEJTE NA VYSTAVENÍ FAKTURY"

Sloupec `foceni` (INTEGER DEFAULT 0) v tabulce `zakazky` — zaškrtávátko přímo v řádku seznamu.

### Zakázky — pole relevantní pro výrobní karty

- `odeslano_do_vyroby` (INTEGER DEFAULT 0) — 1 = zakázka je viditelná v CNC a Dílně. Nastavuje se v kartě Příprava výroby tlačítkem „Do výroby". Existující zakázky před zavedením tohoto pole mají hodnotu 1 díky jednorázové migraci `odeslano_init_v1`.
- `destinace` (TEXT DEFAULT 'Zákazník') — kam case míří: `'Zákazník'` nebo `'Sklad'`. Nastavuje se v Přípravě výroby, zobrazuje se v CNC i Dílně (read-only).
- `poznamka_cnc` (TEXT) — poznámka z kanceláře pro CNC operátora. Píše se v Přípravě výroby, zobrazuje se v CNC jako needitovatelný text.
- `poznamka_dilna` (TEXT) — poznámka z kanceláře pro dílnu. Píše se v Přípravě výroby, zobrazuje se v Dílně jako needitovatelný text.
- `poznamka_cnc_operator` (TEXT) — soukromá editovatelná poznámka operátora CNC (co ještě chybí nařezat). Viditelná a editovatelná pouze v kartě CNC.

### Fakturace — blokace odchylkami

Zakázku s otevřenou odchylkou (stav = `Nová` v tabulce `odchylky_karty`) nelze vyfakturovat. Blokace je na dvou úrovních: frontend (zakázka je zašedlá s badge ⚠ Odchylka, checkbox disabled) i backend (endpoint `POST /api/faktury` vrátí 400 pokud zakázka má otevřenou odchylku).

### PDF faktura — layout (pdf_faktura.py)

A4 stránka má 210 mm šířky; okraje jsou `ML = MR_offset = 18 mm`, tedy **dostupná šířka obsahu = 174 mm**. Při úpravách šířek sloupců v ReportLab tabulkách je třeba dodržet:

- **Tabulka položek** (KÓD+NÁZEV | KS | CENA ZA MJ | SAZBA | ZÁKLAD | CELKEM S DPH): součet šířek musí být **174 mm** (aktuální: `70 + 10 + 24 + 16 + 26 + 28`).
- **Rekapitulace DPH** (Sazba DPH | Základ bez DPH | DPH | Celkem s DPH): šířka **96 mm**, umístěná napravo přes `rek_x = MR - 96 * mm`. Aktuální sloupce: `22 + 26 + 24 + 24`. Pozor: v těchto hodnotách se musí vejít jak bold text v hlavičce, tak max. hodnoty typu „XX XXX,XX Kč" (cca 15 mm textu + 8 mm paddingu = ~23 mm potřeba pro peněžní sloupce).
- **Pruh „CELKEM K ÚHRADĚ"** musí mít stejnou šířku jako rekapitulace (96 mm) a pravá `drawRightString` pozice je `rek_x + 93 mm` (= 3 mm od pravého okraje pruhu).

Pokud budou v budoucnu přibývat sloupce nebo se prodlužovat hlavičky, vždy přepočítat součet a ověřit, že všechno padne do dostupné šířky stránky. Při testu lze vygenerovat PDF na `/tmp/test.pdf` a převést na PNG přes `pdftoppm -png -r 250 /tmp/test.pdf /tmp/test`.

### Stav skladu — důležité

Správný výpočet disponibilního množství je vždy `COALESCE(s.naskladneno - s.pouzito, 0)`. Sloupec `skutecny_stav` obsahuje fyzicky napočítaný stav z inventury — může být 0 i když materiál na skladě je. Nikdy nepoužívat `skutecny_stav` jako hlavní ukazatel dostupnosti.

### Karta CNC (cnc sekce v app.html)

Zobrazuje zakázky s `odeslano_do_vyroby = 1`. Filtr nahoře:
- **Čeká na řezání** — stav `Čeká`
- **CNC hotovo** — stav `CNC hotovo` nebo `Výroba`
- **Všechny casy** — stav `Čeká`, `CNC hotovo`, nebo `Výroba`

Sloupce: HN+badge | Název/Zákazník | Poznámka z kanceláře (`poznamka_cnc`, read-only) | Termín | Materiály | Checklist | Poznámka operátora (`poznamka_cnc_operator`, editovatelná) | Akce

Barevné kódování materiálových chipů (funkce `_cncMatStyle`): prémiové=červená, natural=žluto-oranžová, plast=žlutá, fenol=hnědá, pěna=šedá, ostatní=modrá.

Tlačítko „⚙ CNC hotovo" je aktivní pouze pokud jsou zaškrtnuty všechny položky checklistu (desky, podvozky, pěny) — nebo pokud zakázka nemá žádné BOM položky pro CNC.

### Karta Příprava výroby (priprava-vyroby sekce)

Zobrazuje zakázky před odesláním do výroby. Sloupce: ★ | HN/Typ | Název | Zákazník/Sklad (dropdown `destinace`) | Poznámky (dva textarea: pro CNC + pro Dílnu) | BOM | Přidáno | Termín (editovatelný date input) | 📷 | Stav | Pracovník | Do výroby | Akce

Kliknutí na řádek **neotevírá detail** — detail se otvírá jen tlačítkem „Detail". Inline edity (termín, destinace, poznámky) ukládají přes `pripravaSetTermin()`, `pripravaSetDestinace()`, `pripravaSetPoznamka()`.

### Karta Dílna (dilna sekce)

Zobrazuje zakázky s `odeslano_do_vyroby = 1`. Sloupce: ★ | HN/Typ | Název | Poznámka z kanceláře (`poznamka_dilna`, read-only) | Zákazník/Sklad (badge) | CNC (checklist chipů) | Termín | Stav | Pracovník | Akce

**Barevné kódování řádků:**
- Prioritní zakázka (hvězdička) → světle modrý podklad (`#eff6ff`, CSS třída `.prioritni-row !important`)
- Výroba / CNC hotovo → světle modrý podklad (stejná barva, inline `style`)
- Hotovo → světle zelený podklad (`#ecfccb`, CSS třída `.dilna-hotovo-row !important` — přebíjí i `.prioritni-row`)
- Ostatní stavy → bílý podklad

**Svislá barevná čára vlevo** (na první `<td>` — hvězdičce):
- Výroba / CNC hotovo → tmavě modrá (`border-left: 4px solid #1d4ed8`)
- Hotovo → olivově zelená (`border-left: 4px solid #65a30d`)
- Ostatní → průhledná

**Řazení řádků** (funkce `_dilnaFilter`): 1. prioritní+Výroba, 2. prioritní, 3. Výroba, 4. ostatní. Implementováno přes `.sort()` s rank skóre `(prioritni ? 2 : 0) + (isVyroba ? 1 : 0)`.

**Workflow stavů v Dílně** — dropdown `<select>` obsahuje pouze: Čeká, Výroba, Hotovo. Stavy Zkontrolováno a Expedováno se nastavují výhradně tlačítky:
- Stav `Hotovo` → zobrazí se zelené tlačítko **Kontrola** (skryjí se Detail a 🖨). Po kliknutí vyskočí modal s potvrzením a informací kam case odnést (📷 Focení nebo 📦 Přejímku dle pole `foceni`). Potvrzení nastaví stav na `Zkontrolováno` (funkce `_dilnaKontrolaPotvrzeni`).
- Stav `Zkontrolováno` → zobrazí se fialové tlačítko **Odneseno** + inline badge „Odneste na focení" nebo „Odneste na přejímku". Kliknutí nastaví stav na `Expedováno` a case zmizí ze seznamu (funkce `_dilnaOdneseno`).

**Detail modal v Dílně** (funkce `zakazkaDetail`):
- Stav zakázky je zobrazen jako badge přímo v záhlaví (tmavý pruh) vedle HN čísla
- Tlačítka „Změnit stav" a „Zrušit zakázku" jsou odstraněna — zobrazuje se pouze „Tisk výrobního listu"
- Sekce **Desky a pěny** je sbalená (`<details>/<summary>`) — kliknutím se rozbalí; při tisku se zobrazí standardně
- Poznámka pro Dílnu (`poznamka_dilna`) se zobrazuje bez emoji kladívka
- **Profily – formátování**: duplicitní řádky se stejným `rozmer_mm` jsou sloučeny (sečtou se ks) přes funkci `_dedupProfily()` ve frontendu
- **Zarážky děrovačky** jsou zobrazeny jako prostý text (ne editovatelná pole). Pokud backend nemá hodnotu (NULL), frontend ji dopočítá z délky profilu funkcemi `_calcZarazka(mm)` a `_calcZarazka2(mm)` (rozteč 128 mm, druhý průchod od 7+ otvorů). Profily kratší než 128 mm zarážky nemají záměrně.

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
