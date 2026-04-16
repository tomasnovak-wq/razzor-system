"""
Import profilů a metadat casů z VHW_import2 – List 2 (nový formát).

Formát: 1 řádek = 1 case (transponováno oproti starému VHW).
  Řádek 1: záhlaví sloupců (Počty L profilu, Rozměry L profilu, ...)
  Řádek 2: sub-indexy (1, 2, 3, ...)
  Řádek 3+: data

Spusť po migrace.py. Server může běžet (čte DB, pak zapisuje transakčně).
CSV soubor = export List 2 z Google Sheets: Soubor → Stáhnout → CSV.
Ulož jako:  data/VHW2.csv

Co se importuje:
  - Počty + rozměry L profilů (10 slotů)
  - Počty + rozměry + zakončení H profilů (15 slotů)
  - Metadata typy_casu: typ_korpusu, vnitrni_sirka/vyska/hloubka,
    orientace_lid, pena_poznamka, pena_odkaz
  - Příslušenství 1–4
"""

import sqlite3, os, re, sys

BASE     = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE, 'data', 'system.db')
CSV_PATH = os.path.join(BASE, 'data', 'VHW2.csv')

# ── Sloupcové indexy (0-based) ─────────────────────────────────────────────────
COL_HN          = 0    # A – HN číslo
COL_NAZEV       = 1    # B – Název case
COL_TYP         = 2    # C – Typ korpusu
COL_SIRKA       = 3    # D – Vnitřní rozměr Š (width)
COL_VYSKA       = 4    # E – Vnitřní rozměr V (height)
COL_HLOUBKA     = 5    # F – Vnitřní rozměr H (lenght)
COL_POZNAMKA    = 6    # G – Poznámka
COL_POLSTROVANI = 7    # H – Polstrování
COL_ORIENTACE   = 8    # I – Orientace LID profilu
COL_POMER       = 9    # J – Poměr výšek / Spodek
COL_ODCHYLKA    = 10   # K – Odchylka%
COL_PRISL_1     = 11   # L – Příslušenství 1
COL_PRISL_2     = 12   # M – Příslušenství 2
COL_PRISL_3     = 13   # N – Příslušenství 3
COL_PRISL_4     = 14   # O – Příslušenství 4
COL_PREPAZKA    = 15   # P – Přepážka / Počet přepážek
COL_SUPLIK      = 16   # Q – Šuplík s plnovýsuvem

# L profily – 10 slotů
COL_L_KS_START     = 17   # R–AA  (R=17, AA=26)
COL_L_ROZMER_START = 27   # AB–AK (AB=27, AK=36)
L_SLOTS = 10

# H profily – 15 slotů
COL_H_KS_START      = 37   # AL–AZ (AL=37, AZ=51)
COL_H_ROZMER_START  = 52   # BA–BO (BA=52, BO=66)
COL_H_ZAK_START     = 67   # BP–CD (BP=67, CD=81)
H_SLOTS = 15

COL_PENA_ODKAZ  = 82   # CE – Výkres polstrování (URL)
COL_DOKUMENT    = 83   # CF – Dokument


# ── Pomocné funkce ─────────────────────────────────────────────────────────────

def clean(val: str) -> str:
    return val.strip().strip('"').strip()

def to_int(val: str):
    v = clean(val).replace('\xa0', '').replace(' ', '')
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None

def to_float(val: str):
    v = clean(val).replace('\xa0', '').replace(' ', '').replace(',', '.')
    v = re.sub(r'[^\d.\-]', '', v)
    try:
        f = float(v)
        return f if f != 0.0 else None
    except (ValueError, TypeError):
        return None

def col(row: list, idx: int) -> str:
    return row[idx] if idx < len(row) else ''


# ── Načti CSV ─────────────────────────────────────────────────────────────────
if not os.path.exists(CSV_PATH):
    print(f"❌ Soubor {CSV_PATH} nenalezen.")
    print("   Stáhni List 2 jako CSV z Google Sheets a ulož jako data/VHW2.csv")
    input("Stiskni Enter pro ukončení...")
    sys.exit(1)

print(f"Čtu {CSV_PATH}...")
import csv
rows = []
with open(CSV_PATH, encoding='utf-8-sig', newline='') as f:
    reader = csv.reader(f)
    for r in reader:
        rows.append(r)

# Řádky 0 a 1 = záhlaví (přeskočíme)
data_rows = rows[2:]
print(f"Nalezeno {len(data_rows)} datových řádků (bez záhlaví).")


# ── DB ─────────────────────────────────────────────────────────────────────────
conn = sqlite3.connect(DB_PATH, timeout=30)
conn.execute("PRAGMA journal_mode = DELETE")
conn.execute("PRAGMA foreign_keys = ON")
conn.row_factory = sqlite3.Row
c = conn.cursor()

c.execute("SELECT id, hn_cislo FROM typy_casu")
db_typy: dict[str, int] = {row['hn_cislo']: row['id'] for row in c.fetchall()}
print(f"Typy casů v DB: {len(db_typy)}")

updated = 0
skipped = 0
no_data = 0
profiles_inserted = 0

for row in data_rows:
    if len(row) < 3:
        no_data += 1
        continue

    hn = clean(col(row, COL_HN))
    if not hn:
        no_data += 1
        continue

    typ_id = db_typy.get(hn)
    if not typ_id:
        skipped += 1
        # Pokud typ neexistuje, vytvoř ho (volitelné – odkomentuj pokud chceš)
        # nazev = clean(col(row, COL_NAZEV))
        # c.execute("INSERT OR IGNORE INTO typy_casu (hn_cislo, nazev) VALUES (?,?)", (hn, nazev))
        # typ_id = c.lastrowid or db_typy.get(hn)
        continue

    # ── Metadata typy_casu ──────────────────────────────────────────────────
    typ_korpusu   = clean(col(row, COL_TYP)) or None
    sirka         = to_float(col(row, COL_SIRKA))
    vyska         = to_float(col(row, COL_VYSKA))
    hloubka       = to_float(col(row, COL_HLOUBKA))
    orientace     = clean(col(row, COL_ORIENTACE)) or None
    pena_text     = clean(col(row, COL_POLSTROVANI)) or None
    pena_url_raw  = clean(col(row, COL_PENA_ODKAZ))
    pena_odkaz    = pena_url_raw if pena_url_raw.startswith('http') else None
    prisl = [clean(col(row, COL_PRISL_1 + i)) or None for i in range(4)]

    c.execute("""
        UPDATE typy_casu SET
            typ_korpusu     = COALESCE(?, typ_korpusu),
            vnitrni_sirka   = COALESCE(?, vnitrni_sirka),
            vnitrni_vyska   = COALESCE(?, vnitrni_vyska),
            vnitrni_hloubka = COALESCE(?, vnitrni_hloubka),
            orientace_lid   = COALESCE(?, orientace_lid),
            pena_poznamka   = COALESCE(?, pena_poznamka),
            pena_odkaz      = COALESCE(?, pena_odkaz),
            prisl_1         = COALESCE(?, prisl_1),
            prisl_2         = COALESCE(?, prisl_2),
            prisl_3         = COALESCE(?, prisl_3),
            prisl_4         = COALESCE(?, prisl_4)
        WHERE id = ?
    """, (typ_korpusu, sirka, vyska, hloubka, orientace, pena_text, pena_odkaz,
          prisl[0], prisl[1], prisl[2], prisl[3], typ_id))

    # ── Smaž staré profily pro tento typ (reimport) ─────────────────────────
    c.execute("DELETE FROM profily_plan WHERE typ_casu_id=?", (typ_id,))

    # ── L profily ────────────────────────────────────────────────────────────
    for i in range(L_SLOTS):
        ks  = to_int(col(row, COL_L_KS_START + i))
        mm  = to_float(col(row, COL_L_ROZMER_START + i))
        if not ks and not mm:
            continue
        c.execute("""
            INSERT INTO profily_plan (typ_casu_id, typ_profilu, poradi, ks, rozmer_mm)
            VALUES (?, 'L', ?, ?, ?)
        """, (typ_id, i + 1, ks or 0, mm))
        profiles_inserted += 1

    # ── H profily ────────────────────────────────────────────────────────────
    for i in range(H_SLOTS):
        ks  = to_int(col(row, COL_H_KS_START + i))
        mm  = to_float(col(row, COL_H_ROZMER_START + i))
        zak = clean(col(row, COL_H_ZAK_START + i)) or None
        if not ks and not mm:
            continue
        c.execute("""
            INSERT INTO profily_plan (typ_casu_id, typ_profilu, poradi, ks, rozmer_mm, zakonceni)
            VALUES (?, 'H', ?, ?, ?, ?)
        """, (typ_id, i + 1, ks or 0, mm, zak))
        profiles_inserted += 1

    updated += 1

conn.commit()
conn.close()

print()
print(f"✓ Zpracováno HN čísel:      {updated}")
print(f"  Přeskočeno (není v DB):   {skipped}")
print(f"  Prázdné řádky:            {no_data}")
print(f"✓ Profily vloženo:          {profiles_inserted}")
print()
print("Hotovo! Restartuj server.")
input("Stiskni Enter pro zavření...")
