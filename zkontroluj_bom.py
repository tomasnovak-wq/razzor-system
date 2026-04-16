"""
Kontrola úplnosti BOM dat v databázi
Spusť: python zkontroluj_bom.py
Výsledek se uloží do bom_kontrola.txt
"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'system.db')
log = []

def w(msg=''):
    print(msg)
    log.append(msg)

conn = sqlite3.connect(DB_PATH, timeout=30)
conn.execute("PRAGMA journal_mode = DELETE")
c = conn.cursor()

w("=== KONTROLA BOM ===")
w()

c.execute('SELECT COUNT(*) FROM typy_casu')
celkem = c.fetchone()[0]
w(f"Typy casů celkem v DB: {celkem}")

c.execute('SELECT COUNT(DISTINCT typ_casu_id) FROM kusovniky')
s_bom = c.fetchone()[0]
w(f"Typy casů S BOM položkami: {s_bom}")

c.execute('''SELECT COUNT(*) FROM typy_casu t
             WHERE NOT EXISTS (SELECT 1 FROM kusovniky k WHERE k.typ_casu_id=t.id)''')
bez_bom = c.fetchone()[0]
w(f"Typy casů BEZ BOM položek: {bez_bom}")

w()
w("--- Příklady HN čísel BEZ BOM ---")
c.execute('''SELECT t.hn_cislo, t.nazev FROM typy_casu t
             WHERE NOT EXISTS (SELECT 1 FROM kusovniky k WHERE k.typ_casu_id=t.id)
             ORDER BY t.hn_cislo LIMIT 30''')
for r in c.fetchall():
    w(f"  {r[0]:15s}  {r[1]}")

w()
w("--- Typy casů s nejméně BOM položkami (min 1) ---")
c.execute('''SELECT t.hn_cislo, t.nazev, COUNT(k.id) as pocet
             FROM typy_casu t
             JOIN kusovniky k ON k.typ_casu_id=t.id
             GROUP BY t.id
             ORDER BY pocet ASC LIMIT 10''')
for r in c.fetchall():
    w(f"  {r[0]:15s}  {r[2]:3d} pol.  {r[1]}")

w()
w("--- Statistika BOM ---")
c.execute('''SELECT MIN(cnt), MAX(cnt), ROUND(AVG(cnt),1) FROM
             (SELECT COUNT(*) as cnt FROM kusovniky GROUP BY typ_casu_id)''')
r = c.fetchone()
w(f"  BOM položky na typ: min={r[0]}, max={r[1]}, průměr={r[2]}")

c.execute('SELECT COUNT(*) FROM kusovniky')
w(f"  Celkem BOM záznamů: {c.fetchone()[0]}")

conn.close()

# Ulož výsledek
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bom_kontrola.txt')
with open(out, 'w', encoding='utf-8') as f:
    f.write('\n'.join(log))

w()
w(f"Výsledek uložen do: bom_kontrola.txt")
input("\nStiskni Enter pro zavření...")
