"""
Importuje dodavatele z MATERIAL.csv do tabulky dodavatele
a normalizuje pole dodavatel v tabulce materialy.

Spusť jednou (server může běžet, ale bezpečnější ho zastavit).
"""

import sqlite3, csv, io, os

BASE = os.path.dirname(__file__)
DB_PATH  = os.path.join(BASE, 'data', 'system.db')
CSV_PATH = os.path.join(BASE, 'data', 'MATERIAL.csv')

# ── Sloučení různě psaných názvů stejného dodavatele ─────────────────────────
# klíč = lowercase název z CSV → kanonická podoba
MERGE = {
    'hainz':      'HAINZ',
    'penn elcom': 'PENN ELCOM',
    'sinfo':      'SINFO',
}

def canonical(name: str) -> str:
    return MERGE.get(name.strip().lower(), name.strip())

# ── Načti CSV (první 2 řádky jsou šum) ───────────────────────────────────────
with open(CSV_PATH, encoding='utf-8-sig') as f:
    lines = f.readlines()

csv_content = ''.join(lines[2:])
reader = csv.DictReader(io.StringIO(csv_content))

suppliers: dict[str, int | None] = {}   # canonical_name → dodaci_lhuta
mat_updates: list[tuple[str, str]] = [] # (canonical_name, kod_materialu)

for row in reader:
    kod = (row.get('Č. produktu') or '').strip()
    dod = (row.get('Dodavatel')   or '').strip()
    lhuta_raw = (row.get('Dodací lhůta') or '').strip()
    if not kod or not dod:
        continue
    canon = canonical(dod)
    lhuta = int(lhuta_raw) if lhuta_raw.isdigit() else None
    if canon not in suppliers:
        suppliers[canon] = lhuta
    elif suppliers[canon] is None and lhuta is not None:
        suppliers[canon] = lhuta
    mat_updates.append((canon, kod))

print(f"Nalezeno kanonických dodavatelů: {len(suppliers)}")
print(f"Materiálů ke spojení: {len(mat_updates)}")

# ── Zápis do DB ───────────────────────────────────────────────────────────────
conn = sqlite3.connect(DB_PATH, timeout=30)
conn.execute("PRAGMA journal_mode = DELETE")
c = conn.cursor()

c.execute("SELECT nazev FROM dodavatele")
existing = {row[0] for row in c.fetchall()}

inserted = 0
skipped  = 0
updated_lhuta = 0

for name, lhuta in sorted(suppliers.items()):
    if name not in existing:
        c.execute("""
            INSERT INTO dodavatele (nazev, zkratka, dodaci_lhuta_dni, mena, aktivni)
            VALUES (?, ?, ?, 'CZK', 1)
        """, (name, name[:6].upper(), lhuta or 14))
        print(f"  + {name}  (lhůta={lhuta or 14} dní)")
        inserted += 1
    else:
        if lhuta:
            cur = c.execute("SELECT dodaci_lhuta_dni FROM dodavatele WHERE nazev=?", (name,)).fetchone()
            if cur and cur[0] == 14 and lhuta != 14:
                c.execute("UPDATE dodavatele SET dodaci_lhuta_dni=? WHERE nazev=?", (lhuta, name))
                updated_lhuta += 1
        print(f"  = {name}  (již existuje)")
        skipped += 1

# Normalizace pole dodavatel v tabulce materialy
updated_mats = 0
for canon, kod in mat_updates:
    c.execute("UPDATE materialy SET dodavatel=? WHERE kod=?", (canon, kod))
    updated_mats += c.rowcount

conn.commit()
conn.close()

print()
print(f"✓ Dodavatelé vloženi:  {inserted}")
print(f"  Dodavatelé přeskočeni (existují): {skipped}")
print(f"  Dodací lhůty aktualizovány: {updated_lhuta}")
print(f"✓ Materiály normalizovány: {updated_mats} řádků")
print()
print("Hotovo! Restartuj server a zkontroluj modul Dodavatelé.")
input("Stiskni Enter pro zavření...")
