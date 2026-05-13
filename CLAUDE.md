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

**Opakující se problém — git HEAD.lock / index.lock:**
Sandbox (VirtioFS mount) nemůže mazat lock soubory vytvořené předchozím git procesem — `rm` vrací `Operation not permitted`. Před každým `git commit` je třeba zkontrolovat a případně smazat locky z Terminálu na Macu:
```bash
sudo rm -f /Users/ludekspilauer/Projekty/razzor-system/.git/HEAD.lock \
           /Users/ludekspilauer/Projekty/razzor-system/.git/index.lock
```
Toto je nutné opakovat před každým commitem z Cowork session — locky vznikají automaticky po každém git operaci v sandboxu.

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
- `kusovniky` — BOM: kolik jakého materiálu jde do každého typu casu; sloupec `prorez_procento` (REAL) pro individuální prořez na položku
- `zakazky` — výrobní zakázky (nové sloupce: `odeslano_do_vyroby`, `destinace`, `poznamka_cnc_operator`)
- `sklad` + `pohyby_skladu` — stavy skladu a pohyby
- `nabidky` — cenové nabídky zákazníkům
- `faktury` — vystavené faktury
- `dochazka` / `dochazka_zaznamy` — docházka pracovníků
- `fifo_davky` — FIFO evidence nákupních cen
- `_migrations` — tracking tabulka pro jednorázové datové migrace (pattern: `SELECT 1 FROM _migrations WHERE name='...'`)
- `typy_casu_dxf` — výsledky parsování DXF souborů pro typ casu (viz sekce DXF níže)
- `barvy_materialu` — barevné profily typů materiálů: `typ TEXT PRIMARY KEY, barva TEXT` (viz sekce Barevné profily níže)

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

Zobrazuje **všechny zakázky** ve stavech `Čeká`, `CNC hotovo`, `Výroba` — bez ohledu na `odeslano_do_vyroby`. Sloupec `odeslano_do_vyroby` je součástí SELECT a používá se pro badge a filtr.

**Filtr nahoře:**
- **Čeká na řezání** — stav `Čeká`
- **CNC hotovo** — stav `CNC hotovo` nebo `Výroba`
- **Všechny casy** — stav `Čeká`, `CNC hotovo`, nebo `Výroba`
- **✅ Může se řezat** — toggle tlačítko (`_cncMuzeSeRezat`); když aktivní, zobrazí jen zakázky s `odeslano_do_vyroby = 1`; filtruje na frontendu z načtených dat

**Badge „Může se řezat"** — zelený badge (`background:#dcfce7;color:#15803d`) pod názvem casu (na novém řádku přes `<br>`, `white-space:nowrap`), zobrazí se pouze když `odeslano_do_vyroby == 1`.

Sloupce: HN+badge | Název/Zákazník + badge „Může se řezat" | Poznámka z kanceláře (`poznamka_cnc`, read-only) | Termín | Materiály | Poznámka operátora (`poznamka_cnc_operator`, editovatelná) | Akce

Barevné kódování materiálových chipů (funkce `_cncMatStyle`): prémiové=červená, natural=žluto-oranžová, plast=žlutá, fenol=hnědá, pěna=šedá, ostatní=modrá.

Tlačítko „⚙ CNC hotovo" je aktivní pouze pokud jsou všechny materiálové chipy ve stavu 2 (hotovo) — nebo pokud zakázka nemá žádné BOM položky pro CNC.

**Tri-state checklist materiálů** — každý materiál (deska, pěna) má tři stavy:
- `0` = neřezáno (šedá tečka)
- `1` = rozpracováno (oranžová tečka)
- `2` = hotovo (zelená fajfka, přeškrtnutý text)

Kliknutím na chip se stav cyklicky přepíná 0→1→2→0. Stav se ukládá do tabulky `cnc_rezani` (sloupec `rezano` jako INTEGER 0/1/2). Funkce `_cncChipHtml(mat, zakId)` generuje HTML chipu, `cncToggleMat(zakId, kod, stavNyni)` provede optimistický update v DOM a uloží na server.

**Podvozky — syntetický chip `_PODVOZKY_`:** Kolečka (podvozky) nejsou řezána CNC, ale signalizují potřebu řezat podvozkovou desku. Místo zobrazení jednotlivých koleček jako chipů se zobrazí jediný chip „Podvozky" s kódem `_PODVOZKY_`. Chip se nezobrazí, pokud desky_mats již obsahují 12mm desku (podvozková deska je pak pokryta chipem desky).

**Tlačítko 🗂 Karta** — otevře výrobní kartu zakázky (funkce `zakazkaDetail(zakId)`), stejnou jako v Dílně.

**Tlačítko ✂ Pěny** — otevře popup pro výpočet polstrování (funkce `cncPolstrovaniPopup(zakId)`). Zobrazuje se **pouze pro typy korpusu**: `Hlava / kombo`, `Accessory case`, `Pedalboard`, `Mixpult`. Filtruje se pomocí konstanty:
```javascript
const _CNC_POLSTROVANI_TYPY = new Set(['Hlava / kombo', 'Accessory case', 'Pedalboard', 'Mixpult']);
```
Podmínka: `_CNC_POLSTROVANI_TYPY.has(z.typ_korpusu||'')`. Pole `typ_korpusu` je nově součástí response `/api/cnc` (JOIN s `typy_casu`).

### Výpočet polstrování — popup a DXF export

Popup `cncPolstrovaniPopup(zakId)` zobrazí výpočet 5 kusů polstrovacích pěn pro case. Vstupní data: rozměry z BOM (`vnitrni_sirka`, `vnitrni_vyska`, `vnitrni_hloubka`), tloušťka pěny z hlavního pěnového materiálu, dělící rovina (`delici_rovina`) z `typy_casu`.

**Pole `delici_rovina`** (INTEGER) — výška spodní části case v mm. Přidáno do `typy_casu` přes `auto_migrate()`. Editovatelné přímo v popupu (uloží se přes `PUT /api/typy-casu/<id>` s `delici_rovina`). Endpoint `api_typ_casu_update()` má `delici_rovina` na whitelistu.

**Výpočet pěn** — klíč `typCase + orientace` (např. `KOMBOMV`):
```python
TOP = {'KOMBOMV': v-dr,   'KOMBOVV': v-dr+2, 'TRUHLAVV': dr-7,
       'TRUHLAMV': dr-9,  'KOMBOF':  v-dr+2, 'TRUHLAF':  dr-11}
BOT = {'KOMBOMV': dr-7,   'KOMBOVV': dr-9,   'TRUHLAVV': v-dr,
       'TRUHLAMV': v-dr+2,'KOMBOF':  dr-11,  'TRUHLAF':  v-dr+2}
```
5 kusů × 2 ks každý: Strop+dno (`s-2t` × `h-2t`), Vrchní přední+zadní (`s` × `top_h`), Vrchní boky (`h-2t` × `top_h`), Spodní přední+zadní (`s` × `bot_h`), Spodní boky (`h-2t` × `bot_h`).

**Endpoint `POST /api/typy-casu/<typ_id>/polstrovani/dxf`** — generuje DXF soubor se všemi obdélníky polstrování. Body: `{dr, sirka, vyska, hloubka, tloustka, orientace, typ_case, hn, nazev}`. Vrátí DXF jako attachment ke stažení.

Implementace DXF (ezdxf knihovna):
- Vrstva pojmenována `P {round(t)}mm pěna` — kompatibilní s DXF parserem v BOM editoru
- Obdélníky jako `LWPOLYLINE` (close=True)
- Mezi kusy mezera `GAP = 20 mm`, mezi skupinami dvojnásobná mezera
- **Bez textu** — pouze obdélníky, žádné popisky
- `io.StringIO()` → encode → `io.BytesIO()` (ezdxf `doc.write()` vyžaduje text stream, ne bytes)
- Závislost `ezdxf>=1.0` je v `requirements.txt`

### Karta Příprava výroby (priprava-vyroby sekce)

Zobrazuje zakázky před odesláním do výroby. Sloupce: ★ | HN/Typ | Název | Zákazník/Sklad (dropdown `destinace`) | Poznámky (dva textarea: pro CNC + pro Dílnu) | BOM | Přidáno | Termín (editovatelný date input) | 📷 | Stav | Pracovník | Do výroby | Akce

Kliknutí na řádek **neotevírá detail** — detail se otvírá jen tlačítkem „Detail". Inline edity (termín, destinace, poznámky) ukládají přes `pripravaSetTermin()`, `pripravaSetDestinace()`, `pripravaSetPoznamka()`.

### Modul Příprava zakázek (kancelar sekce)

Modul pro kancelář — správa zakázek před výrobou. URL klíč: `kancelar`, funkce `kancelar()`.

**Zákazník/kontakt je v seznamu read-only** — pole `zakaznik`, `tel`, `mail` se v řádku tabulky zobrazují jako prostý text. Editace je možná výhradně přes tlačítko „Detail" (`kanDetail(id)`). Důvod: předejít náhodným přepisům při procházení seznamu.

Inline editovatelná pole v seznamu (přes `_kanInlineSel` / `_kanBlur`): Řeší (resitel_id), Priorita, Štítky, Stav, Aktivní.

Zákazník (zakaznik, tel, mail) se ukládá přes `_kanSaveField(id, field, value)` volaný z detailu.

Uživatelské role (konstanta `ROLE_LIST` v app.html): `Admin`, `Dílna`, `CNC`, `Kancelář`, `Projektant`. Barvy rolí definuje `ROLE_BARVY`. Role se ukládá ve sloupci `role TEXT NOT NULL DEFAULT 'Dílna'` v tabulce `uzivatele`.

### Karta Dílna (dilna sekce)

Zobrazuje zakázky s `odeslano_do_vyroby = 1`. Sloupce: ★ | HN/Typ | Název | Poznámka z kanceláře (`poznamka_dilna`, read-only) | Zákazník/Sklad (badge) | CNC (checklist chipů) | Termín | Stav | Pracovník | Akce

**Barevné kódování řádků:**
- Zkontrolováno → sytá zelená (`#86efac`, CSS třída `.dilna-zkontrolovano-row !important` — přebíjí vše)
- Hotovo → světlá zelená (`#ecfccb`, CSS třída `.dilna-hotovo-row !important` — přebíjí i `.prioritni-row`)
- Prioritní zakázka (hvězdička) → světle modrý podklad (`#eff6ff`, CSS třída `.prioritni-row !important`)
- Výroba / CNC hotovo → světle modrý podklad (stejná barva `#eff6ff`, inline `style`)
- Ostatní stavy → bílý podklad

**Svislá barevná čára vlevo** (na první `<td>` — hvězdičce):
- Výroba / CNC hotovo → tmavě modrá (`border-left: 4px solid #1d4ed8`)
- Hotovo → olivově zelená (`border-left: 4px solid #65a30d`)
- Zkontrolováno → tmavě zelená (`border-left: 4px solid #15803d`)
- Ostatní → průhledná

**Řazení řádků** (funkce `_dilnaFilter`): 1. Zkontrolováno, 2. Hotovo, 3. prioritní+Výroba, 4. prioritní, 5. Výroba, 6. ostatní. Implementováno přes `.sort()` s rank skóre. **Důležité:** `dilna()` renderuje `<tbody>` prázdný a ihned volá `_dilnaFilter()` — sort se tak aplikuje i při prvním načtení stránky.

**Workflow stavů v Dílně** — dropdown `<select>` obsahuje pouze: Čeká, Výroba, Hotovo. Stavy Zkontrolováno a Expedováno se nastavují výhradně tlačítky:
- Stav `Hotovo` → zobrazí se zelené tlačítko **Kontrola** (skryjí se Detail a 🖨). Po kliknutí vyskočí modal s potvrzením a informací kam case odnést (📷 Focení nebo 📦 Přejímku dle pole `foceni`). Potvrzení nastaví stav na `Zkontrolováno` (funkce `_dilnaKontrolaPotvrzeni`).
- Stav `Zkontrolováno` → zobrazí se fialové tlačítko **Odneseno** + inline badge „Odneste na focení" nebo „Odneste na přejímku". Kliknutí nastaví stav na `Expedováno` a case zmizí ze seznamu (funkce `_dilnaOdneseno`).

**Detail modal v Dílně** (funkce `zakazkaDetail`):
- Stav zakázky je zobrazen jako badge přímo v záhlaví (tmavý pruh) vedle HN čísla
- Tlačítko „Tisk výrobního listu" (PDF) přesunuto do záhlaví jako malý text-link „PDF"
- Tlačítka „Změnit stav" a „Zrušit zakázku" jsou odstraněna
- Sekce **DESKY** i **PĚNY** jsou sbalené (`<details>/<summary>`) — kliknutím se rozbalí; při tisku se zobrazí standardně
- Poznámka pro Dílnu (`poznamka_dilna`) se zobrazuje bez emoji kladívka
- **Profily – formátování**: duplicitní řádky se stejným `rozmer_mm` jsou sloučeny (sečtou se ks) přes funkci `_dedupProfily()` ve frontendu
- **Zarážky děrovačky** jsou zobrazeny jako prostý text (ne editovatelná pole). Pokud backend nemá hodnotu (NULL), frontend ji dopočítá z délky profilu funkcemi `_calcZarazka(mm)` a `_calcZarazka2(mm)` (rozteč 128 mm, druhý průchod od 7+ otvorů). Profily kratší než 128 mm zarážky nemají záměrně.
- **DXF vizualizace v sekci Desky a Pěny**: pokud má typ casu nahraný DXF, zobrazí se pod BOM tabulkou SVG náhled příslušných tvarů s legendou a kótovacím popupem. Implementováno přes funkci `_buildDxfBlock(typ, nadpis)` (viz níže).

**Sub-sekce v DESKY a PĚNY:** Každá sekce má čtyři rozbalovací pod-sekce (`<details open>`), rozbalené při otevření nadřazené sekce (kromě 3D):
- **Materiál** — BOM tabulka desek/pěn
- **DXF** — SVG náhled vrstev z DXF výkresu
- **Soubory** — přílohy a odkazy kategorie `vykres_sestavy` / `vykres_polstrovani`
- **3D sestava** / **3D polstrování** — Three.js viewer (lazy init přes `ontoggle`, bez atributu `open`, protože `clientWidth=0` při zavřeném `<details>` způsobuje nulový canvas)

Sub-sekce mají levý barevný pruh `border-left: 3px solid #6b7280` a tmavě šedé záhlaví. Label záhlaví je nenápadný (`font-size:.7rem; font-weight:400; opacity:.5`, CSS třída `det-lbl`).

**Duální 3D viewer v detailu zakázky** — DESKY a PĚNY mají každý svůj Three.js viewer. Aby nedocházelo ke kolizi DOM ID, používá se suffix:
- DESKY viewer: suffix `'-d'` (IDs: `3d-viewer-wrap-d`, `3d-viewer-canvas-d`, `3d-layer-legend-d`)
- PĚNY viewer: suffix `''` (původní IDs bez suffixu)

Funkce `_bu3dInitViewer` a `_bu3dRebuildLegend` mají volitelný čtvrtý parametr `sfx=''`:
```javascript
async function _bu3dInitViewer(typId, vid, vrstvy, sfx='')
function _bu3dRebuildLegend(typId, vid, vrstvy, sfx='')
```

**PĚNY 3D viewer — výchozí opacity:** Po inicializaci se automaticky nastaví:
- Deska, HW, Profily, Jiné → opacity 5 % (`_3dSetGroupOpacity(t, 0.05)`)
- Nýty → skryté (`m.visible = false`, `_3dMasterSetActive('nyty', false)`)

Implementováno v `_vlInit3DPena` volaném přes `ontoggle` na `<details>` sekci PĚNY 3D.

### Docházka — modul (dochazka sekce)

Záložky: **Live** (`_dochLiveLoad`) | **Plán** (`_dochPlanLoad`) | **Měsíc** (`_dochMesicLoad`). Aktivní záložka uložena v `let _dochTab = 'live'`.

**Plán docházky — kopírování po uživatelích:**
Každý editovatelný řádek (dnešek a budoucnost) má u každého uživatele tlačítko 📋 (copy) a 📌 (paste, skryté do zkopírování). Workflow:
1. Klik 📋 u uživatele X v den A → zkopíruje jeho `cas_od`/`cas_do` do `_dochCopyBuffer = { uid, cas_od, cas_do }`
2. Ve všech ostatních řádcích téhož uživatele se zobrazí 📌
3. Klik 📌 v den B → vloží časy, aktualizuje oba selecty v DOM a uloží přes `POST /api/dochazka`

Selecty mají atributy `data-uid`, `data-datum`, `data-field` (od/do) — slouží k DOM lookupům při paste. Feedback přes `_planToast(msg, err)` — malý toast vpravo dole (nekonflikuje s `_dochToast`, který je velký overlay pro live check-in).

**Dashboard — widget docházky rozdělen na dvě sekce:**
- 🏭 **Dílna** — uživatelé s rolí `Dílna` nebo `CNC` (oranžový levý proužek)
- 🏢 **Kancelář** — uživatelé s rolí `Kancelář`, `Admin` nebo `Projektant` (modrý levý proužek)

Filtrování probíhá na frontendu podle pole `role` v datech z `/api/dochazka/tyden`. Endpoint vrací `role` jako součást každého záznamu (JOIN s tabulkou `uzivatele`).

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

### DXF parser — záložka v BOM editoru

Záložka **DXF** (tab 4) v BOM editoru (`bomDetailUnified`) umožňuje nahrát DXF výkres, analyzovat vrstvy a získat počty kusů + plochy materiálů.

**Tabulka `typy_casu_dxf`** (database.py):
```
id, typ_casu_id, nazev_souboru, vrstvy_json, varovani_json, nahrano, overrides_json, polygony_json
```

**Endpointy** (app.py):
- `GET  /api/typy-casu/<id>/dxf` — načte uložené výsledky + overrides + polygony pro SVG
- `POST /api/typy-casu/<id>/dxf` — nahraje DXF soubor (multipart), spustí parser, uloží výsledky
- `PATCH /api/typy-casu/<id>/dxf` — uloží manuální přiřazení vrstev (`overrides_json`)

**Parser** (inline funkce v `api_dxf_post()`):
- Podporuje **LWPOLYLINE** (moderní AC1015+) i starší **POLYLINE + VERTEX** (AC1009, AutoCAD R12)
- Shoelace formula pro plochu polygonů
- Nesting algoritmus per-vrstva: polygony na sudé hloubce = kusy, liché = díry
- Klíčová oprava nestingu: polygon může obsahovat jen VĚTŠÍ polygon (`oa > area`) — malý otvor na šroub nemůže „obsahovat" velký kus
- `_interior_point()`: area-weighted centroid + horizontal ray fallback pro konkávní tvary
- `_chain()`: spojuje otevřené segmenty do uzavřených smyček (starší výkresy)
- Detekce typu vrstvy z názvu: `D XYmm` → deska, `P XYmm` → pěna, ostatní → jiné
- Tloušťka z názvu vrstvy regex: `^[DP]\s+(\d+(?:[.,]\d+)?)mm`

**Frontend** (app.html):
- `_dxfOverrides` — globální objekt `{layerName: 'deska:9' | 'pena:50' | 'ignore' | 'auto'}`; resetuje se jen při nahrání nového souboru nebo otevření jiného typu casu
- `_dxfEffective(vrstva)` — vrátí efektivní `{typ, tloustka_mm}` s ohledem na override
- `_dxfBuildTable(dxfData, typId)` — sestaví HTML tabulky vrstev + summary bar + SVG + spodní tlačítko Uložit
- `_dxfRenderSvg(polygony, vrstvy, overrides)` — SVG náhled: deska=světle hnědá (`#c8986a`), pěna=zelená (`#86efac`), jiné=šedá; fill-rule=evenodd pro správné díry
- `_dxfOverrideChange(sel, layerName)` — handler dropdownu; aktualizuje badge, summary, překreslí SVG
- `_dxfRecalcSummary()` — přepočítá m² desek/pěn, zobrazí obě tlačítka Uložit (nahoře i dole)
- `_dxfSaveOverrides(typId)` — PATCH na server, schová tlačítka po uložení

**Důležité gotcha — HTML atributy s JSON.stringify:**
V `onchange` atributu selectu MUSÍ být použity jednoduché uvozovky jako delimiter atributu, protože `JSON.stringify` produkuje dvojité uvozovky které by atribut předčasně uzavřely:
```html
<!-- SPRÁVNĚ: -->
onchange='_dxfOverrideChange(this, ${JSON.stringify(v.nazev)})'
<!-- ŠPATNĚ (SyntaxError: Unexpected token '}' v console): -->
onchange="_dxfOverrideChange(this, ${JSON.stringify(v.nazev)})"
```

**Barvy v UI:**
- Badge v tabulce (`_DXF_TYP_COLOR`): deska = `background:#e8c9a0;color:#7c4a1e`, pěna = `background:#d1fae5;color:#065f46`
- SVG náhled (`FILL` v `_dxfRenderSvg`): deska = `#c8986a`, pěna = `#86efac`, jiné = `#e5e7eb`

**DXF barevný systém (podrobný/zjednodušený mód):**

Globální přepínač `_dxfDetailMode` (bool) řídí, zda se použijí barvy dle tloušťky nebo uniformní. **Výchozí hodnota je `true`** (podrobný mód dle tloušťky) — tlačítko v BOM editoru zobrazuje „🎨 Podrobně (dle tloušťky)" při otevření.

`_dxfLayerColors(typ, mm, nazev)` — vrátí `[fill, stroke]`:
- **Zjednodušený mód** (`_dxfDetailMode = false`): deska=`#9ca3af`/`#4b5563`, pěna=`#1f2937`/`#111827`
- **Podrobný mód** (`_dxfDetailMode = true`): barva dle tloušťky z map:

```javascript
_DXF_PENA_COLORS = {
  5:   ['#fec93f','#b38200'],  // P 5mm
  10:  ['#b34015','#7c2d12'],  // P 10mm
  20:  ['#66b06c','#2d6b33'],  // P 20mm
  30:  ['#668fb1','#2e547a'],  // P 30mm
  40:  ['#1896f8','#0f5ca3'],  // P 40mm
  50:  ['#ca46f7','#7e22ce'],  // P 50mm
  999: ['#454f5f','#1e2d3d'],  // Baldachýn
};
_DXF_PENA_DEFAULT = ['#374151','#111827'];  // Pěna ostatní (černá)

_DXF_DESKA_COLORS = {
  6.5: ['#fd351c','#b91c0d'],
  6.8: ['#d94e1c','#a33510'],  // Premium
  6.9: ['#caf94c','#6b9117'],  // Plast
  9:   ['#13f648','#0a9e2e'],
  9.4: ['#86efac','#15803d'],  // Premium
  12:  ['#1ffbfe','#0a8f91'],
  18:  ['#fe4ef8','#a21caf'],
};
_DXF_DESKA_DEFAULT = ['#d1d5db','#6b7280'];  // Deska ostatní (šedá)
```

**Kritické detaily implementace:**
- Tolerance pro matching tloušťky desek: `Math.abs(k - mm) < 0.05` — nutné, jinak 6.5 vs 6.8 kolide (rozdíl 0.3 < 0.3 způsoboval záměnu barev)
- Sentinel hodnota `mm = -1` pro override `"deska:0"` / `"pena:0"` (Ostatní) — zabraňuje falešnému vyčtení tloušťky z názvu vrstvy přes `_dxfMmFromName()`
- `effMap` v `_dxfRenderSvg`: pro každou vrstvu uchovává `{typ, mm}` s ohledem na override string `"deska:6.8"` — bez effMap se tloušťka z overridu ztrácela
- `_dxfBadgeStyle(typ, mm, nazev)` — mode-aware badge: v zjednodušeném módu uniformní, v podrobném dle barvy tloušťky + luminance check pro barvu textu

**DXF vizualizace v detailu zakázky (Dílna) — `_buildDxfBlock`:**

Funkce `_buildDxfBlock(typ, nadpis)` je definována uvnitř `zakazkaDetail` (async funkce). Volá se dvakrát:
```javascript
dxfDeskyHtml = _buildDxfBlock('deska', 'DXF – plochy desek');
dxfPenyHtml  = _buildDxfBlock('pena',  'DXF – plochy pěn');
```

Data čte z `dxfData` (odpověď `/api/typy-casu/<id>/dxf`, klíč `dxf`) a `ovr` (overrides). Zobrazí jen polygony příslušného typu, vždy v podrobném barevném módu (dočasně nastaví `_dxfDetailMode = true`).

Struktura SVG: vizuální `<path>` elementy per-vrstva (s `fill-rule="evenodd"` pro správné díry, `pointer-events="none"`) + průhledné overlay `<path>` elementy per-polygon (jeden polygon = jeden klikatelný prvek) s `data-coords`, `data-label`, `data-fill`, `data-strk` atributy.

Výsledné HTML se vkládá: v sekci Desky pod `</table>` před `</details>` (screen) a za `</table>` v print-only divu, totéž pro sekci Pěny.

**DXF kótovací popup — `window._dxfDimPopup`:**

Globální funkce definovaná (přepsána) při každém otevření `zakazkaDetail`. Spustí se kliknutím na overlay path v SVG bloku.

Vstup: `el` (kliknutý path), `ev` (MouseEvent). Data z `el.dataset`:
- `coords` — JSON pole `[[x,y],...]` v reálných mm (zaokrouhleno na 1 desetinné místo)
- `label` — text záhlaví (např. „Pěna 20 mm — 318×60 mm")
- `fill`, `strk` — barvy vrstvy

Výstup: bílý plovoucí popup s mini SVG technickým výkresem:
- Tvar nakreslen ve správné barvě (`fill-opacity: 0.65`)
- Pro každou unikátní délku hrany (≥ 5 mm) nakreslena kótovací čára odsazená `OFFS=20 px` od hrany s tikmarky, spojovacími čárami k rohům a textem `realLen mm` (font 9 px, bílý halo via `paint-order="stroke"`)
- Normála hrany určena z těžiště polygonu (outward = pryč od těžiště)
- Popup se adaptivně zvětšuje dle poměru stran (AR):
  - AR > 8×: MAXW\_C=300, MAXH\_C=220
  - AR > 4×: 240/190
  - AR > 2×: 190/160
  - Normální: 160/130
- `BORDER=30 px` je vždy fixní v pixelech (nezávislý na scale) → kóty se nikdy neoříznou
- SVG má `overflow="visible"` pro krajní případy
- Popup se zavře kliknutím mimo; ostatní tvary v SVG se ztmaví na `opacity: 0.1`

### Soubory — záložka v BOM editoru

Záložka **Soubory** (tab 6) v BOM editoru (`bomDetailUnified`) umožňuje připojit k typu casu soubory (PDF, DXF, obrázky…) a URL odkazy.

**Tabulky (database.py):**
- `typy_casu_prilohy` — nahrané soubory: `id, typ_casu_id, filename, filepath, mime_type, velikost, created_at, typ_json`
- `typy_casu_links` — URL odkazy (existující tabulka) + přidán sloupec `typ_json TEXT NOT NULL DEFAULT '["ostatni"]'`

**Endpointy (app.py):**
- `GET/POST /api/typy-casu/<id>/prilohy` — seznam / nahrání souboru
- `PATCH /api/typy-casu/<id>/prilohy/<fid>` — uloží `typ_json`
- `GET /api/typy-casu/<id>/prilohy/<fid>/view` — inline zobrazení (`as_attachment=False`)
- `GET /api/typy-casu/<id>/prilohy/<fid>/download` — stažení
- `DELETE /api/typy-casu/<id>/prilohy/<fid>` — smazání

**Kategorie souborů (chips)** — multi-select, vždy viditelné toggle pilulky:
- Klíče: `obrazek`, `vykres_sestavy`, `vykres_polstrovani`, `ostatni`
- Aktivní = modrá (`#dbeafe`/`#1d4ed8`), neaktivní = šedá
- Stejný systém pro nahrané soubory i URL odkazy
- Funkce: `_tcChipsHtml(selected, onclickFn)`, `_tcTypToggle(chip)`, `_buLinkTypToggle(chip)`

**Inline prohlížeč** — `_tcPrilohaView(typId, fid, filename)`:
- Obrázky (`png/jpg/jpeg/gif/svg/webp`) → `<img>`
- Ostatní → `<iframe>` (PDF, DXF…)
- Overlay přes celou stránku, zavře se kliknutím mimo nebo ✕

### 3D viewer — záložka v BOM editoru

Záložka **3D** (tab 5) v BOM editoru (`bomDetailUnified`) umožňuje nahrát ZIP se STL soubory (jeden STL = jedna vrstva z AutoCADu) a zobrazit 3D náhled case s přepínáním viditelnosti vrstev.

**Pořadí záložek v BOM editoru:**
`TAB_NAMES = ['Specifikace', 'Materiály', 'Profily', 'DXF', '3D', 'Soubory', 'Čas výroby']`
(tab 1–7; save button: `[1,7].includes(_buTab)`; auto-init 3D: `_buTab===5`)

**Workflow AutoCAD → viewer:**
1. Michal otevře výkres v AutoCADu, načte LISP: `APPLOAD` → `export_layers_3d.lsp`
2. Spustí příkaz `ExportLayers3D` — skript projde viditelné vrstvy, zamrazí ostatní, exportuje každou do `.stl`
3. Vybere STL soubory ve Finderu → Pravý klik → Komprimovat → vznikne ZIP
4. Nahraje ZIP do Razzor → záložka 3D → klikne „Vybrat ZIP" (upload spustí se výběrem souboru, bez extra tlačítka)

**LISP skript** (`export_layers_3d.lsp`, aktuálně v22):
- Přeskakuje skryté/zmrazené vrstvy (uživatel je záměrně vypnul = nechce exportovat)
- Po exportu obnovuje původní stav viditelnosti vrstev
- `_rz-safename`: převádí názvy vrstev na bezpečné názvy souborů — mezery/lomítka → `_`, tečky → `,`, česká diakritika stripována (`překlizka` → `preklizka`)
- `(gc)` po každém exportu — předchází pádu AutoCADu při větším počtu vrstev
- **Referenční box 1×1×1 mm na WCS (0,0,0)** přidán do každého STL před exportem, smazán přes `entdel` po exportu. Server z jeho polohy zjistí UCS offset a automaticky opraví vrcholy. Syntaxe: `(command "._BOX" "0,0,0" "1,1,1")`.
- **Bez UNDO control** (v21+) — UNDO control způsoboval pád LISPu na Macu kvůli odlišnému promptu mezi verzemi AutoCADu. S `_Freeze *` je undo buffer malý, pád nehrozí.
- **v22 — oprava záporných souřadnic**: Před exportem se model posune do kladného WCS prostoru (rezerva +10 mm od originu). Po exportu se vrátí. Důvod: AutoCAD STLOUT ořezává/normalizuje záporné souřadnice. Funkce: `(setq rz-extmin (getvar "EXTMIN"))`, shift přes `._MOVE` na celý `(ssget "_X")`.

**Tabulka `typy_casu_3d`** (database.py):
```
id, typ_casu_id, nazev_souboru, vrstvy_json, nahrano, typ_sestavy
```
`vrstvy_json` = seznam `{nazev, filename, typ, tloustka_mm}` kde `typ` ∈ `deska|pena|hw|profily|nyty|jine|ignorovat`.
`typ_sestavy` (TEXT) — comma-separated multi-value: `'sestava'`, `'polstrovani'`, `'sestava,polstrovani'`. Jeden typ casu může mít více verzí 3D (více řádků v tabulce).

**STL soubory** se ukládají do `data/3d_modely/<typ_id>/<vid>/` (kde `vid` = DB id řádku v typy_casu_3d). Legacy cesta bez `vid` (`data/3d_modely/<typ_id>/`) je podporována jako fallback.

**Endpointy** (app.py):
- `POST  /api/typy-casu/<id>/3d` — nahraje ZIP, vytvoří nový záznam s novou verzí, STL do `data/3d_modely/<id>/<vid>/`
- `GET   /api/typy-casu/<id>/3d` — vrátí seznam všech verzí: `[{id, nazev_souboru, typ_sestavy, nahrano, vrstvy_json}, ...]`
- `PATCH /api/typy-casu/<id>/3d/<vid>` — uloží přiřazení typů vrstev nebo `typ_sestavy`
- `GET   /api/typy-casu/<id>/3d/<vid>/stl/<filename>` — vrátí binární STL soubor

**Auto-detekce typu vrstvy při uploadu** (v `api_3d_post()`, pořadí priorit):
1. Název vrstvy — regex `^D[_\s]+(\d+)mm` → `deska`, `^P[_\s]+(\d+)mm` → `pena`
2. Název "Nyty" / "Nýty" (normalizováno) → `nyty`
3. Kód nalezen v tabulce `materialy` → dle `typ_materialu`: začíná `HW` → `hw`, obsahuje `PROFIL` → `profily`, `PÉNA`/`PENA` → `pena`, `DESKA` → `deska`
4. Fallback → `jine`

**Auto-korekce Y-offsetu** (v `api_3d_post()` v app.py):
- Primární: ref box 1×1×1 mm u (0,0,0) — server najde box, spočítá UCS offset, přesune všechny vrcholy, box odstraní z STL souboru
- Fallback (LISP bez ref. boxu): Y-median — spočítá Y-střed každé vrstvy, filtruje outliery ±50 mm, opraví vrstvy s delta 0.5–10 mm
- Funkce: `_stl_read_triangles`, `_tri_verts`, `_stl_write_triangles`, `_stl_shift_xyz`, `_stl_find_ref_box`
- Korekce se provádí in-place (přepíše STL soubory na disku)

**Frontend** (app.html):

Layout záložky 3D: nahoře karty verzí (tabs), pod nimi dvousloupcový grid — vlevo (280px) seznam vrstev, vpravo (1fr) Three.js canvas vždy viditelný inline. Canvas se **neničí** při přepínání záložek ani při přepínání verzí.

Globální proměnné a konstanty (všechny na module level, ne uvnitř funkcí):
- `_3dCurrentVid = null` — aktuálně zobrazená verze 3D modelu (vid = DB id řádku v typy_casu_3d)
- `_3dOpacityMap = {}` — `{typ → opacity}` (0.05–1.0) pro průhlednost skupin vrstev
- `_3dMatMap = {}` — `{kod_lowercase: {kod, nazev, ...}}` — naplní se z `_cache.materialy` při přepnutí na záložku 3D
- `_3D_COLORS` — barvy fallback dle typu: `deska=0xc8986a`, `pena=0x86efac`, `hw=0x94a3b8`, `profily=0xfbbf24`, `nyty=0xd4d4d8`, `jine=0xe2e8f0`
- `_3D_DESKA_COLORS` / `_3D_PENA_COLORS` — barvy dle tloušťky (odpovídají AutoCAD paletě)
- `_3D_NAMED_COLORS` — barvy pro specifické názvy vrstev (Logo, Nyty, Pomocna 1…)
- `layerColor(v)` — **globální funkce** (pozor: dříve byla lokální uvnitř `_bu3dInitViewer`, což způsobovalo ReferenceError). Priority: 0=kód materiálu→bílá, 1=přesný název, 2=tloušťka, 3=typ fallback
- `_3dNormName(s)` — odstraní diakritiku, lowercase
- `_3dColorByThickness(nazev)` — regex `^([dp])[_\s]+(\d+)mm`, lookup v color mapě
- `_bu3dModelInfoHtml(typId, vid, verData, vrstvy)` — tabulka vrstev s dropdown přiřazení typu (vid = verze)
- `_bu3dTypOpts(cur)` — options pro dropdown: deska/pena/hw/profily/nyty/jiné/ignorovat
- `_3dSetLayerType(typId, vid, filename, newTyp)` — PATCH `/3d/<vid>` + aktualizace meshe + přestavění legendy
- `_bu3dSwitchVersion(typId, vid)` — přepne viewer na jinou verzi (načte STL soubory dané verze)
- `_bu3dSetTipSestavy(typId, vid, value)` — toggle Sestava/Polstrování (comma-separated set, obě najednou možné)
- `_3dToggleLayer(filename)` — přepne viditelnost jedné vrstvy
- `_3dToggleGroup(typ)` — přepne viditelnost celé skupiny
- `_3dSetGroupOpacity(typ, opacity)` — nastaví průhlednost skupiny; `depthWrite = opacity >= 1.0` (jinak by průhledné vrstvy blokovaly objekty za nimi)
- `_3dPillSetActive(pill, active)` — vizuální stav individuálního pillu
- `_3dMasterSetActive(typ, active)` — vizuální stav master checkbox-tlačítka skupiny
- `_bu3dRebuildLegend(typId, vid, vrstvy)` — přestaví legendu; volá se po změně typu vrstvy
- `_bu3dVersionTabsHtml(typId, versions, activeVid)` — karty verzí s pills Sestava/Polstrování + tlačítko „+"

**Legenda vrstev** (renderována v `_bu3dInitViewer`, přestavována v `_bu3dRebuildLegend`):
- Jeden řádek na skupinu: `[master checkbox-btn] [pill1] [pill2] …` + posuvník průhlednosti
- Master btn: bílé pozadí, barevný rámeček; kliknutím přepne celou skupinu
- Individuální piluly: barevné pozadí (barva+`22` alpha); zblednou při skrytí
- Posuvník opacity (`<input type="range" min="0.05" max="1" step="0.05">`) — živý náhled v %
- `GROUP_DEF`: deska=`#c8986a`, pěna=`#86efac`, hw=`#94a3b8`, profily=`#fbbf24`, nýty=`#d4d4d8`, jiné=`#e2e8f0`
- Zobrazují se jen skupiny přítomné v modelu (`presentTypes`)

Funkce `_bu3dInitViewer(typId, vid, vrstvy, sfx='')`:
- Volitelný parametr `sfx` — suffix pro DOM ID elementů (canvas, wrap, legend); prázdný řetězec = původní chování
- Three.js r128, STLLoader, OrbitControls (CDN: cdn.jsdelivr.net/npm/three@0.128.0)
- STL URL: `/api/typy-casu/${typId}/3d/${vid}/stl/${filename}`
- **Rotace**: `geometry.applyMatrix4(makeRotationX(-Math.PI/2))` — AutoCAD Z-up → Three.js Y-up
- **Orbit centra**: 5–95 percentil vrcholů (každý 50.) — ignoruje outlier body
- **Emissive** pro tenké objekty (minDim < 3 mm): 45 % base barvy
- **renderOrder + polygonOffset**: jine=2, pena=1, deska=0 — předchází Z-fightingu
- **depthWrite**: `false` pro průhledné skupiny (opacity < 1.0) — nutné aby vrstvy za průhlednou vrstvou byly viditelné
- `_3dMeshMap` — globální `{filename → mesh}`

**Tip pro re-detekci typů:** Po přidání nových materiálů do katalogu nebo opravě kódů stačí ZIP nahrát znovu — server provede detekci znovu s aktuálními daty z DB.

### Barevné profily materiálů

Každý typ materiálu (`materialy.typ`) může mít přiřazenou barvu uloženou v tabulce `barvy_materialu`. Barvy se zobrazují jako tenký barevný pruh na levém okraji řádku v seznamu Materiálů (3px) a v BOM editoru (5px).

**Tabulka `barvy_materialu`** (database.py):
```
typ TEXT PRIMARY KEY, barva TEXT NOT NULL DEFAULT '#e5e7eb'
```

**Migrace `barvy_materialu_seed_v1`** — při prvním spuštění po nasazení v92 automaticky naplní výchozí barvy:
- DESKA=`#f0b429`, PĚNA=`#d1d5db`, PODVOZEK=`#e57373`, PROFIL AL=`#f59e0b`, LEPIDLO=`#f59e0b`
- HW GUMOVÁ NOŽIČKA=`#4b5563`, HW KOULE=`#5b8fd9`, HW L ROH=`#4d8b8b`, HW OSTATNÍ=`#111827`
- HW PANT=`#7c1d1d`, HW RACK=`#dc2626`, HW RUKOJEŤ=`#4a1942`, HW ZÁMEK=`#2e7d32`

**Endpointy** (app.py):
- `GET  /api/barvy-materialu` — vrátí `{barvy: {typ: barva, ...}}`
- `POST /api/barvy-materialu` — uloží `{barvy: {typ: barva, ...}}`; prázdná/null hodnota barvy = smazání záznamu

**Frontend** (app.html):
- `let _barvyMat = {}` — globální cache `{typ: barva}`; naplní se při prvním načtení sekce Materiály (spolu s API `/api/barvy-materialu`)
- `function _barvaMat(typ)` — vrátí barvu nebo `null`
- Pruh v řádku: `border-left: 3px solid ${barva}` na první `<td>` v `_matRow()` (Materiály) a `border-left: 5px solid ${barva}` v BOM řádcích
- Editace barev: Nastavení → záložka **🎨 Barevné profily** (`_renderBarvyMaterialu()`) — color pickery pro každý typ; `_barvySaveAll()` POSTuje na API a aktualizuje `_barvyMat` v paměti

### Verze

Při nasazení `_NASTROJE\Nasadit na cloud.bat` se volá `python update_version.py "popis" "autor"`, který zapíše `version.json`. Endpoint `/api/verze` tato data zobrazuje ve spodním pravém rohu aplikace.

### UI — barevné konvence

**Chybové / urgentní upozornění** (červená):
```
background: #fef2f2
border:      1px solid #fca5a5
nadpis:      color #991b1b, font-weight 700
popis/text:  color #991b1b
badge/počet: background #dc2626, color #fff
tlačítko:    background #dc2626, color #fff
řádek karty: border 1px solid #fecaca, background white
```

**Varování** (žlutá — starší styl, preferuj červenou pro akční upozornění):
```
background: #fef3c7
border:      1px solid #fbbf24
```

## Spolupráce více vývojářů

Kód sdílíme přes **GitHub** (jeden repozitář). Na Fly.io může nasazovat každý, kdo má přístup do Fly.io organizace.

Workflow:
1. Udělej změny lokálně
2. `Nasadit na cloud.bat` — commitne, pushne, nasadí
3. Druhý vývojář spustí `Stahni novou verzi.bat` (git pull) + restartuje server

Data (databáze) jsou sdílená na Fly.io. Lokální DB a cloudová DB se nesynchronizují automaticky — je třeba ručně použít `Nahrat data na cloud.bat` / `Stahni data z cloudu.bat`.
