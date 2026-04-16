"""
Diagnostický skript - výsledky zapíše do test_vysledek.txt
Spusť: python test_db.py
"""
import sys
import os

log = []

def w(msg):
    print(msg)
    log.append(msg)

w("=== DIAGNOSTIKA FLIGHT CASE SYSTEM ===")
w(f"Python: {sys.version}")
w(f"Pracovni adresar: {os.getcwd()}")
w(f"Soubor skriptu: {os.path.abspath(__file__)}")
w("")

# Test Flask
try:
    import flask
    w(f"Flask: OK (verze {flask.__version__})")
except ImportError as e:
    w(f"Flask: CHYBA - {e}")
    w("  -> Spust: pip install flask")

# Test databaze
db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'system.db')
w(f"\nDatabaze cesta: {db_path}")
w(f"Databaze existuje: {os.path.exists(db_path)}")
if os.path.exists(db_path):
    w(f"Databaze velikost: {os.path.getsize(db_path):,} bytes")

# Test SQLite spojeni
try:
    import sqlite3
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode = DELETE")
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM materialy")
    count = cursor.fetchone()[0]
    w(f"SQLite: OK - materialu v DB: {count}")
    cursor.execute("SELECT COUNT(*) FROM typy_casu")
    w(f"SQLite: typy casu: {cursor.fetchone()[0]}")
    cursor.execute("SELECT COUNT(*) FROM kusovniky")
    w(f"SQLite: kusovnik polozky: {cursor.fetchone()[0]}")
    conn.close()
except Exception as e:
    w(f"SQLite CHYBA: {e}")

# Test templates
tmpl = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates', 'app.html')
w(f"\napp.html existuje: {os.path.exists(tmpl)}")
if os.path.exists(tmpl):
    w(f"app.html velikost: {os.path.getsize(tmpl):,} bytes")

# Zapis do souboru
result_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'test_vysledek.txt')
with open(result_path, 'w', encoding='utf-8') as f:
    f.write('\n'.join(log))

w(f"\nVysledky ulozeny do: {result_path}")
input("\nStiskni Enter pro zavreni...")
