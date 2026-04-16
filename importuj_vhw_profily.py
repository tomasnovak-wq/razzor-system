"""
Importuje řezný plán profilů a metadata typů casů z VHW.csv do DB.
Spusť po migrace.py, server může běžet (jen čte a pak zapíše transakčně).

Co se importuje pro každé HN číslo:
  - Počty + rozměry L profilů  (až 10 řádků)
  - Počty + rozměry + zakončení H profilů (až 15 řádků)
  - Orientace LID profilu
  - Text polstrování / pěny
  - Odkaz na výkres polstrování (pokud URL)
  - Příslušenství 1–4
"""

import sqlite3, os, re

BASE     = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE, 'data', 'system.db')
CSV_PATH = os.path.join(BASE, 'data', 'VHW.csv')

# ── Row indices (0-based) v CSV ────────────────────────────────────────────────
IDX_PENA_TEXT   = 790   # Polstrování – text
IDX_ORIENTACE   = 791   # Orientace LID profilu
IDX_PRISL       = list(range(794, 798))   # Příslušenství 1–4
IDX_L_KS        = list(range(800, 810))   # Počty L profilu 1–10
IDX_L_ROZMER    = list(range(815, 825))   # Rozměry L profilu 1–10
IDX_H_KS        = list(range(825, 840))   # Počty H profilu 1–15
IDX_H_ROZMER    = list(range(840, 855))   # Rozměry H profilu 1–15
IDX_H_ZAKONCENI = list(range(855, 870))   # Zakončení H profilu 1–15
IDX_PENA_ODKAZ  = 875   # Výkres polstrování – odkaz

def clean(val: str) -> str:
    """Odstraní přebytečné uvozovky a whitespace z CSV buňky."""
    return val.strip().strip('"').strip()

def to_int(val: str) -> int | None:
    v = clean(val).replace('\xa0', '').replace(' ', '')
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None

def to_float(val: str) -> float | None:
    v = clean(val).replace('\xa0', '').replace(' ', '').replace(',', '.')
    # Odstraň přípony jako 'Kč', '%'
    v = re.sub(r'[^\d.\-]', '', v)
    try:
        return float(v)
    except (ValueError, TypeError):
        return None

# ── Načti CSV ─────────────────────────────────────────────────────────────────
print(f"Čtu {CSV_PATH}...")
with open(CSV_PATH, encoding='utf-8-sig') as f:
    lines = f.readlines()

# Řádek 0 = HN čísla (sloupce)
header_cols = lines[0].rstrip('\n').split(',')

def cell(line_idx: int, col_idx: int) -> str:
    if line_idx >= len(lines):
        return ''
    cols = lines[line_idx].split(',')
    return cols[col_idx] if col_idx < len(cols) else ''

# Najdi všechna HN čísla a jejich indexy sloupců
hn_columns: dict[str, int] = {}
for col_i, val in enumerate(header_cols):
    v = clean(val)
    if v.startswith('HN') and len(v) >= 8:
        hn_columns[v] = col_i
    elif v and re.match(r'^\d{6}X$', v):   # 221846X apod.
        hn_columns[v] = col_i

print(f"Nalezeno {len(hn_columns)} HN čísel v CSV.")

# ── DB ─────────────────────────────────────────────────────────────────────────
conn = sqlite3.connect(DB_PATH, timeout=30)
conn.execute("PRAGMA journal_mode = DELETE")
conn.execute("PRAGMA foreign_keys = ON")
conn.row_factory = sqlite3.Row
c = conn.cursor()

# Načti existující typy casů z DB (hn_cislo → id)
c.execute("SELECT id, hn_cislo FROM typy_casu")
db_typy: dict[str, int] = {row['hn_cislo']: row['id'] for row in c.fetchall()}
print(f"Typy casů v DB: {len(db_typy)}")

# ── Import ─────────────────────────────────────────────────────────────────────
updated   = 0
skipped   = 0
profiles_inserted = 0

for hn, col_i in hn_columns.items():
    typ_id = db_typy.get(hn)
    if not typ_id:
        skipped += 1
        continue

    # --- Metadata ---
    pena_text  = clean(cell(IDX_PENA_TEXT,  col_i))
    orientace  = clean(cell(IDX_ORIENTACE,  col_i))
    pena_url_raw = clean(cell(IDX_PENA_ODKAZ, col_i))
    pena_odkaz = pena_url_raw if pena_url_raw.startswith('http') else None
    prisl = [clean(cell(idx, col_i)) or None for idx in IDX_PRISL]

    c.execute("""
        UPDATE typy_casu SET
            orientace_lid = COALESCE(orientace_lid, ?),
            pena_poznamka = COALESCE(pena_poznamka, ?),
            pena_odkaz    = COALESCE(pena_odkaz, ?),
            prisl_1       = COALESCE(prisl_1, ?),
            prisl_2       = COALESCE(prisl_2, ?),
            prisl_3       = COALESCE(prisl_3, ?),
            prisl_4       = COALESCE(prisl_4, ?)
        WHERE id = ?
    """, (orientace or None, pena_text or None, pena_odkaz,
          prisl[0], prisl[1], prisl[2], prisl[3], typ_id))

    # --- L profily ---
    for i in range(10):
        ks  = to_int(cell(IDX_L_KS[i],     col_i))
        mm  = to_float(cell(IDX_L_ROZMER[i], col_i))
        if not ks and not mm:
            continue
        c.execute("""
            INSERT INTO profily_plan (typ_casu_id, typ_profilu, poradi, ks, rozmer_mm)
            VALUES (?, 'L', ?, ?, ?)
            ON CONFLICT(typ_casu_id, typ_profilu, poradi) DO UPDATE SET
                ks=excluded.ks, rozmer_mm=excluded.rozmer_mm
        """, (typ_id, i + 1, ks or 0, mm))
        profiles_inserted += 1

    # --- H profily ---
    for i in range(15):
        ks  = to_int(cell(IDX_H_KS[i],       col_i))
        mm  = to_float(cell(IDX_H_ROZMER[i],   col_i))
        zak = clean(cell(IDX_H_ZAKONCENI[i],   col_i)) or None
        if not ks and not mm:
            continue
        c.execute("""
            INSERT INTO profily_plan (typ_casu_id, typ_profilu, poradi, ks, rozmer_mm, zakonceni)
            VALUES (?, 'H', ?, ?, ?, ?)
            ON CONFLICT(typ_casu_id, typ_profilu, poradi) DO UPDATE SET
                ks=excluded.ks, rozmer_mm=excluded.rozmer_mm, zakonceni=excluded.zakonceni
        """, (typ_id, i + 1, ks or 0, mm, zak))
        profiles_inserted += 1

    updated += 1

conn.commit()
conn.close()

print(f"\n✓ Zpracováno HN čísel:      {updated}")
print(f"  Přeskočeno (není v DB):   {skipped}")
print(f"✓ Profily vloženo/update:   {profiles_inserted}")
print()
print("Hotovo! Restartuj server.")
input("Stiskni Enter pro zavření...")
