"""
Flight Case výrobní systém — Flask server
Spuštění: python app.py
Přístup: http://localhost:5000  (nebo http://IP_PC:5000 z jiných strojů v síti)
"""
from flask import Flask, render_template, request, jsonify, redirect, url_for, send_file
import sqlite3
import os
import sys
import json
import csv
import io
import subprocess
import threading
import time
from datetime import date, datetime, timedelta
from database import get_db, init_db, auto_migrate, aktualizuj_stav_skladu, zkontroluj_dostupnost_materialu, odepis_material_ze_skladu, vypocti_cenu_dilu
from pdf_faktura import vygeneruj_pdf

app = Flask(__name__)
app.secret_key = 'flightcase-system-2026'

# ─── GLOBÁLNÍ ERROR HANDLER ──────────────────────────────────────────────
# Zajistí, že server vždy vrátí JSON (nikdy HTML stránku s chybou)
import traceback as _tb

@app.errorhandler(Exception)
def handle_any_exception(e):
    _tb.print_exc()
    return jsonify({'error': str(e)}), 500

@app.errorhandler(404)
def handle_404(e):
    return jsonify({'error': 'Endpoint nenalezen'}), 404

# ─── POMOCNÉ ──────────────────────────────────────────────────────────────

def db_rows_to_list(rows):
    return [dict(r) for r in rows]

def get_sklad_stav(conn, material_kod):
    c = conn.cursor()
    c.execute("""
        SELECT naskladneno, pouzito, skutecny_stav, min_skladem,
               (naskladneno - pouzito) as vypocteny_stav
        FROM sklad WHERE material_kod=?
    """, (material_kod,))
    row = c.fetchone()
    if not row:
        return {'naskladneno': 0, 'pouzito': 0, 'skutecny_stav': 0, 'min_skladem': 0, 'vypocteny_stav': 0}
    return dict(row)

# ─── DASHBOARD ────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('app.html')

# ─── MATERIÁLY API ────────────────────────────────────────────────────────

@app.route('/api/materialy')
def api_materialy():
    conn = get_db()
    c = conn.cursor()
    search = request.args.get('q', '')
    typ = request.args.get('typ', '')
    limit = int(request.args.get('limit', 200))
    offset = int(request.args.get('offset', 0))

    dodavatel_id = request.args.get('dodavatel_id', '')

    query = "SELECT m.*, COALESCE(s.naskladneno - s.pouzito, 0) as vypocteny_stav, COALESCE(s.skutecny_stav, 0) as skutecny_stav, COALESCE(s.min_skladem, 0) as min_skladem FROM materialy m LEFT JOIN sklad s ON s.material_kod = m.kod WHERE 1=1"
    params = []
    if search:
        query += " AND (m.kod LIKE ? OR m.nazev LIKE ? OR m.dodavatel LIKE ?)"
        params += [f'%{search}%', f'%{search}%', f'%{search}%']
    if typ:
        query += " AND m.typ = ?"
        params.append(typ)
    if dodavatel_id:
        # Filtruje materiály podle primárního dodavatele (match přes jméno)
        query += " AND m.dodavatel = (SELECT nazev FROM dodavatele WHERE id=?)"
        params.append(dodavatel_id)
    query += " ORDER BY m.oblibeny DESC, m.typ, m.nazev LIMIT ? OFFSET ?"
    params += [limit, offset]

    c.execute(query, params)
    items = db_rows_to_list(c.fetchall())

    c.execute("SELECT DISTINCT typ FROM materialy WHERE typ != '' ORDER BY typ")
    typy = [r['typ'] for r in c.fetchall()]

    conn.close()
    return jsonify({'items': items, 'typy': typy})

@app.route('/api/materialy/<kod>')
def api_material_detail(kod):
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT m.*, s.naskladneno, s.pouzito, s.skutecny_stav, s.min_skladem,
               (s.naskladneno - s.pouzito) as vypocteny_stav
        FROM materialy m LEFT JOIN sklad s ON s.material_kod = m.kod
        WHERE m.kod = ?
    """, (kod,))
    mat = c.fetchone()
    if not mat:
        conn.close()
        return jsonify({'error': 'Materiál nenalezen'}), 404

    # Pohyby skladu
    c.execute("""
        SELECT p.*, z.hn_cislo as zakazka_hn
        FROM pohyby_skladu p
        LEFT JOIN zakazky z ON z.id = p.zakazka_id
        WHERE p.material_kod = ?
        ORDER BY p.created_at DESC LIMIT 50
    """, (kod,))
    pohyby = db_rows_to_list(c.fetchall())

    # Použití v BOM
    c.execute("""
        SELECT t.hn_cislo, t.nazev, k.mnozstvi
        FROM kusovniky k
        JOIN typy_casu t ON t.id = k.typ_casu_id
        WHERE k.material_kod = ? AND t.aktivni = 1
        ORDER BY t.nazev
        LIMIT 50
    """, (kod,))
    pouziti = db_rows_to_list(c.fetchall())

    conn.close()
    return jsonify({'material': dict(mat), 'pohyby': pohyby, 'pouziti': pouziti})

@app.route('/api/materialy', methods=['POST'])
def api_material_create():
    data = request.json
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("""
            INSERT INTO materialy (kod, nazev, typ, druh, umisteni, hmotnost, balenf,
            master_baleni, nakup_baleni, nakup_jednotka, nc_bez_dph, cas_s, dodavatel,
            dodaci_lhuta, sirka_hw, priorita, zobrazovat, poznamka)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (data['kod'], data['nazev'], data.get('typ',''), data.get('druh',''),
              data.get('umisteni',''), data.get('hmotnost',0), data.get('balenf',1),
              data.get('master_baleni',1), data.get('nakup_baleni',0), data.get('nakup_jednotka',0),
              data.get('nc_bez_dph',0), data.get('cas_s',0), data.get('dodavatel',''),
              data.get('dodaci_lhuta',14), data.get('sirka_hw',0),
              data.get('priorita','Střední'), data.get('zobrazovat',1),
              data.get('poznamka','')))
        c.execute("INSERT OR IGNORE INTO sklad (material_kod) VALUES (?)", (data['kod'],))
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'kod': data['kod']})
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 400

@app.route('/api/materialy/<kod>', methods=['PUT'])
def api_material_update(kod):
    data = request.json
    conn = get_db()
    c = conn.cursor()
    fields = ['nazev','typ','druh','umisteni','hmotnost','balenf','master_baleni',
              'nakup_baleni','nakup_jednotka','nc_bez_dph','cas_s','dodavatel',
              'dodaci_lhuta','sirka_hw','priorita','zobrazovat','poznamka']
    updates = ', '.join(f"{f}=?" for f in fields if f in data)
    vals = [data[f] for f in fields if f in data]
    if updates:
        c.execute(f"UPDATE materialy SET {updates}, updated_at=datetime('now') WHERE kod=?", vals + [kod])
        conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/materialy/<kod>/oblibeny', methods=['POST'])
def api_material_toggle_oblibeny(kod):
    """Přepne příznak oblíbenosti materiálu (0 ↔ 1)."""
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        UPDATE materialy
        SET oblibeny = CASE WHEN oblibeny = 1 THEN 0 ELSE 1 END,
            updated_at = datetime('now')
        WHERE kod = ?
    """, (kod,))
    conn.commit()
    c.execute("SELECT oblibeny FROM materialy WHERE kod=?", (kod,))
    row = c.fetchone()
    conn.close()
    return jsonify({'ok': True, 'oblibeny': row['oblibeny'] if row else 0})

# ─── TYPY CASŮ API ────────────────────────────────────────────────────────

@app.route('/api/typy-casu')
def api_typy_casu():
    conn = get_db()
    c = conn.cursor()
    search = request.args.get('q', '')
    typ = request.args.get('typ', '')
    limit = int(request.args.get('limit', 100))
    offset = int(request.args.get('offset', 0))

    query = """
        SELECT t.*,
               COALESCE((
                   SELECT SUM(k.mnozstvi * m.nc_bez_dph)
                   FROM kusovniky k
                   JOIN materialy m ON m.kod = k.material_kod
                   WHERE k.typ_casu_id = t.id
               ), 0) as cena_dilu
        FROM typy_casu t WHERE t.aktivni=1
    """
    params = []
    if search:
        query += " AND (t.hn_cislo LIKE ? OR t.nazev LIKE ?)"
        params += [f'%{search}%', f'%{search}%']
    if typ:
        query += " AND t.typ_korpusu = ?"
        params.append(typ)
    query += " ORDER BY CAST(SUBSTR(t.hn_cislo, 3) AS INTEGER) LIMIT ? OFFSET ?"
    params += [limit, offset]

    c.execute(query, params)
    items = db_rows_to_list(c.fetchall())

    c.execute("SELECT DISTINCT typ_korpusu FROM typy_casu WHERE aktivni=1 AND typ_korpusu!='' ORDER BY typ_korpusu")
    typy = [r['typ_korpusu'] for r in c.fetchall()]

    # Cenové parametry pro výpočet správné MC
    c.execute("SELECT klic, hodnota FROM cas_parametry WHERE sekce='Ceny'")
    ceny_par = {r['klic']: r['hodnota'] for r in c.fetchall()}

    conn.close()
    return jsonify({'items': items, 'typy': typy, 'ceny_par': ceny_par})

@app.route('/api/typy-casu/<int:typ_id>')
def api_typ_casu_detail(typ_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM typy_casu WHERE id=?", (typ_id,))
    typ = c.fetchone()
    if not typ:
        conn.close()
        return jsonify({'error': 'Typ nenalezen'}), 404

    # BOM – přímé položky
    c.execute("""
        SELECT k.material_kod, k.mnozstvi, m.nazev, m.typ, m.druh, m.nc_bez_dph,
               m.hmotnost as hmotnost_j, m.nity, m.oblibeny,
               COALESCE(s.naskladneno - s.pouzito, 0) as stav_skladu,
               (k.mnozstvi * m.nc_bez_dph) as cena_polozky,
               (k.mnozstvi * m.hmotnost)   as hmotnost_polozky
        FROM kusovniky k
        JOIN materialy m ON m.kod = k.material_kod
        LEFT JOIN sklad s ON s.material_kod = k.material_kod
        WHERE k.typ_casu_id = ?
        ORDER BY m.oblibeny DESC, m.typ, m.nazev
    """, (typ_id,))
    bom = db_rows_to_list(c.fetchall())

    # BOM – automatické spojeniky (agregované přes všechny přímé položky)
    spojeniky_agg = {}   # spojovaci_kod → {data + mnozstvi + zdroje}
    for polozka in bom:
        c.execute("""
            SELECT ms.spojovaci_kod, ms.mnozstvi_na_kus,
                   m.nazev, m.nc_bez_dph, m.hmotnost as hmotnost_j,
                   COALESCE(s.naskladneno - s.pouzito, 0) as stav_skladu
            FROM material_spojeniky ms
            JOIN materialy m ON m.kod = ms.spojovaci_kod
            LEFT JOIN sklad s ON s.material_kod = ms.spojovaci_kod
            WHERE ms.material_kod = ?
        """, (polozka['material_kod'],))
        for sp in c.fetchall():
            kod = sp['spojovaci_kod']
            mnozstvi = polozka['mnozstvi'] * sp['mnozstvi_na_kus']
            zdroj = {
                'material_kod': polozka['material_kod'],
                'material_nazev': polozka['nazev'],
                'bom_mnozstvi': polozka['mnozstvi'],
                'mnozstvi_na_kus': sp['mnozstvi_na_kus'],
                'celkem': round(mnozstvi, 3),
            }
            if kod in spojeniky_agg:
                spojeniky_agg[kod]['mnozstvi'] += mnozstvi
                spojeniky_agg[kod]['zdroje'].append(zdroj)
            else:
                spojeniky_agg[kod] = {
                    'material_kod': kod,
                    'nazev': sp['nazev'],
                    'mnozstvi': mnozstvi,
                    'nc_bez_dph': sp['nc_bez_dph'],
                    'hmotnost_j': sp['hmotnost_j'],
                    'stav_skladu': sp['stav_skladu'],
                    'je_automaticky': True,
                    'zdroje': [zdroj],
                }
    for sp in spojeniky_agg.values():
        sp['cena_polozky']    = round(sp['mnozstvi'] * sp['nc_bez_dph'], 2)
        sp['hmotnost_polozky'] = round(sp['mnozstvi'] * (sp['hmotnost_j'] or 0), 4)
        sp['mnozstvi']         = round(sp['mnozstvi'], 2)

    spojeniky = list(spojeniky_agg.values())

    cena_dilu    = sum(b['cena_polozky'] for b in bom) + sum(s['cena_polozky'] for s in spojeniky)
    hmotnost_bom = round(sum(b['hmotnost_polozky'] or 0 for b in bom), 3)

    # Pracovní postupy – odkazy
    _lnk_tbl = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='typy_casu_links'").fetchone()
    links = []
    if _lnk_tbl:
        c.execute("SELECT id, nazev, url, poradi FROM typy_casu_links WHERE typ_casu_id=? ORDER BY poradi, id", (typ_id,))
        links = [dict(r) for r in c.fetchall()]

    conn.close()
    return jsonify({'typ': dict(typ), 'bom': bom, 'spojeniky': spojeniky,
                    'cena_dilu': cena_dilu, 'hmotnost_bom': hmotnost_bom, 'links': links})

@app.route('/api/typy-casu/<int:typ_id>/debug-spojeniky')
def api_bom_debug_spojeniky(typ_id):
    """Diagnostika: pro každý materiál v BOM ukáže jeho spojeniky."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, nazev, hn_cislo FROM typy_casu WHERE id=?", (typ_id,))
    typ = c.fetchone()
    if not typ:
        conn.close()
        return jsonify({'error': 'Typ nenalezen'}), 404

    c.execute("""
        SELECT k.material_kod, k.mnozstvi, m.nazev
        FROM kusovniky k
        JOIN materialy m ON m.kod = k.material_kod
        WHERE k.typ_casu_id = ?
        ORDER BY k.material_kod
    """, (typ_id,))
    bom_polozky = db_rows_to_list(c.fetchall())

    result = []
    for pol in bom_polozky:
        c.execute("""
            SELECT ms.spojovaci_kod, ms.mnozstvi_na_kus, m.nazev as spoj_nazev
            FROM material_spojeniky ms
            JOIN materialy m ON m.kod = ms.spojovaci_kod
            WHERE ms.material_kod = ?
        """, (pol['material_kod'],))
        spoj = db_rows_to_list(c.fetchall())
        result.append({
            'material_kod': pol['material_kod'],
            'material_nazev': pol['nazev'],
            'mnozstvi_v_bom': pol['mnozstvi'],
            'spojeniky': spoj,
        })
    conn.close()
    return jsonify({'typ': dict(typ), 'bom': result})

@app.route('/api/typy-casu', methods=['POST'])
def api_typ_casu_create():
    data = request.json
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("""
            INSERT INTO typy_casu (hn_cislo, nazev, typ_korpusu, vnitrni_sirka,
            vnitrni_vyska, vnitrni_hloubka, cena_vyroby, cas_narocnost,
            hmotnost, prodej_ap_bez_dph, poznamka)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (data['hn_cislo'], data['nazev'], data.get('typ_korpusu',''),
              data.get('vnitrni_sirka',0), data.get('vnitrni_vyska',0),
              data.get('vnitrni_hloubka',0), data.get('cena_vyroby',0),
              data.get('cas_narocnost',0), data.get('hmotnost',0),
              data.get('prodej_ap_bez_dph',0), data.get('poznamka','')))
        new_id = c.lastrowid
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'id': new_id})
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 400

@app.route('/api/typy-casu/<int:typ_id>', methods=['PUT'])
def api_typ_casu_update(typ_id):
    data = request.json
    conn = get_db()
    c = conn.cursor()
    fields = ['nazev','typ_korpusu','vnitrni_sirka','vnitrni_vyska','vnitrni_hloubka',
              'cena_vyroby','cas_narocnost','hmotnost','prodej_ap_bez_dph','spravna_mc','aktivni','poznamka']
    updates = ', '.join(f"{f}=?" for f in fields if f in data)
    vals = [data[f] for f in fields if f in data]
    if updates:
        c.execute(f"UPDATE typy_casu SET {updates}, updated_at=datetime('now') WHERE id=?", vals + [typ_id])
        conn.commit()
    conn.close()
    return jsonify({'ok': True})

# BOM editace
@app.route('/api/typy-casu/<int:typ_id>/bom', methods=['POST'])
def api_bom_add(typ_id):
    data = request.json
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("""
            INSERT OR REPLACE INTO kusovniky (typ_casu_id, material_kod, mnozstvi)
            VALUES (?,?,?)
        """, (typ_id, data['material_kod'], data['mnozstvi']))
        vypocti_cenu_dilu(conn, typ_id)
        conn.commit()
        conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 400

@app.route('/api/typy-casu/<int:typ_id>/bom/<mat_kod>', methods=['DELETE'])
def api_bom_remove(typ_id, mat_kod):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM kusovniky WHERE typ_casu_id=? AND material_kod=?", (typ_id, mat_kod))
    vypocti_cenu_dilu(conn, typ_id)
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/typy-casu/<int:typ_id>/profily-plan', methods=['GET', 'POST'])
def api_profily_plan(typ_id):
    conn = get_db()
    c = conn.cursor()
    if request.method == 'GET':
        c.execute("""
            SELECT typ_profilu, poradi, ks, rozmer_mm, zakonceni
            FROM profily_plan WHERE typ_casu_id=? ORDER BY typ_profilu, poradi
        """, (typ_id,))
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return jsonify({'profily': rows})
    # POST – nahradí všechny profily pro tento typ
    data = request.get_json() or {}
    profily = data.get('profily', [])
    c.execute("DELETE FROM profily_plan WHERE typ_casu_id=?", (typ_id,))
    for p in profily:
        ks = int(p.get('ks') or 0)
        rozmer = p.get('rozmer_mm')
        if not ks and not rozmer:
            continue
        c.execute("""
            INSERT INTO profily_plan (typ_casu_id, typ_profilu, poradi, ks, rozmer_mm, zakonceni)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (typ_id, p['typ_profilu'], p['poradi'], ks, rozmer, p.get('zakonceni') or None))
    # Zneplatni pruvodni_profily pro zakázky tohoto typu (budou reinicializovány)
    c.execute("""
        DELETE FROM pruvodni_profily WHERE zakazka_id IN
        (SELECT id FROM zakazky WHERE typ_casu_id=?)
    """, (typ_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/typy-casu/<int:typ_id>/links', methods=['GET', 'POST'])
def api_typy_casu_links(typ_id):
    conn = get_db()
    c = conn.cursor()
    # Pokud tabulka ještě neexistuje (před restartem serveru), vrátíme prázdné pole
    _tbl = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='typy_casu_links'").fetchone()
    if not _tbl:
        conn.close()
        return jsonify({'links': []})
    if request.method == 'GET':
        c.execute("SELECT id, nazev, url, poradi FROM typy_casu_links WHERE typ_casu_id=? ORDER BY poradi, id", (typ_id,))
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return jsonify({'links': rows})
    # POST – nahradí všechny odkazy pro tento typ
    data = request.get_json() or {}
    links = data.get('links', [])
    c.execute("DELETE FROM typy_casu_links WHERE typ_casu_id=?", (typ_id,))
    for i, lnk in enumerate(links):
        nazev = (lnk.get('nazev') or '').strip()
        url   = (lnk.get('url')   or '').strip()
        if not nazev or not url:
            continue
        c.execute("INSERT INTO typy_casu_links (typ_casu_id, nazev, url, poradi) VALUES (?, ?, ?, ?)",
                  (typ_id, nazev, url, i))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/kusovniky/migrace-spravna-mc', methods=['POST'])
def api_migrace_spravna_mc():
    """Jednorázová migrace: přečte hodnotu TRUE z kusovníků → uloží jako spravna_mc → smaže TRUE ze všech kusovníků."""
    conn = get_db()
    c = conn.cursor()
    # Najdi všechny záznamy kde material_kod = 'TRUE' (case-insensitive)
    c.execute("""
        SELECT k.typ_casu_id, k.mnozstvi
        FROM kusovniky k
        WHERE UPPER(k.material_kod) = 'TRUE'
    """)
    rows = c.fetchall()
    pocet = len(rows)
    for row in rows:
        c.execute("""
            UPDATE typy_casu SET spravna_mc = ?, updated_at = datetime('now')
            WHERE id = ?
        """, (row['mnozstvi'], row['typ_casu_id']))
    # Smaž TRUE ze všech kusovníků
    c.execute("DELETE FROM kusovniky WHERE UPPER(material_kod) = 'TRUE'")
    # Smaž TRUE z tabulky materiálů (byl to nesmyslný záznam)
    c.execute("DELETE FROM sklad WHERE UPPER(material_kod) = 'TRUE'")
    c.execute("DELETE FROM materialy WHERE UPPER(kod) = 'TRUE'")
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'opraveno_bom': pocet})

@app.route('/api/kusovniky/hromadne-odeber', methods=['POST'])
def api_kusovniky_hromadne_odeber():
    """Odebere materiál ze všech kusovníků (nebo vybraného). Body: { material_kod, typ_casu_id (opt.) }"""
    data = request.json
    mat_kod = (data.get('material_kod') or '').strip()
    typ_casu_id = data.get('typ_casu_id')  # None = všechny
    if not mat_kod:
        return jsonify({'error': 'Chybí material_kod'}), 400
    conn = get_db()
    c = conn.cursor()
    if typ_casu_id:
        c.execute("DELETE FROM kusovniky WHERE material_kod=? AND typ_casu_id=?", (mat_kod, typ_casu_id))
        pocet_typu = 1
    else:
        c.execute("SELECT DISTINCT typ_casu_id FROM kusovniky WHERE material_kod=?", (mat_kod,))
        typ_ids = [r['typ_casu_id'] for r in c.fetchall()]
        c.execute("DELETE FROM kusovniky WHERE material_kod=?", (mat_kod,))
        pocet_typu = len(typ_ids)
        for tid in typ_ids:
            vypocti_cenu_dilu(conn, tid)
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'pocet_typu': pocet_typu})

# ─── SKLAD API ────────────────────────────────────────────────────────────

@app.route('/api/sklad')
def api_sklad():
    conn = get_db()
    c = conn.cursor()
    search = request.args.get('q', '')
    filtr = request.args.get('filtr', '')  # 'nedostatek', 'ok'

    query = """
        SELECT m.kod, m.nazev, m.typ, m.druh, m.dodavatel, m.nc_bez_dph, m.priorita,
               m.oblibeny,
               COALESCE(s.naskladneno, 0) as naskladneno,
               COALESCE(s.pouzito, 0) as pouzito,
               COALESCE(s.skutecny_stav, COALESCE(s.naskladneno,0) - COALESCE(s.pouzito,0)) as stav,
               COALESCE(s.min_skladem, 0) as min_skladem,
               s.posledni_inventura
        FROM materialy m
        LEFT JOIN sklad s ON s.material_kod = m.kod
        WHERE (
            m.zobrazovat = 1
            OR EXISTS (SELECT 1 FROM kusovniky k WHERE k.material_kod = m.kod)
            OR EXISTS (SELECT 1 FROM material_spojeniky ms WHERE ms.spojovaci_kod = m.kod)
        )
    """
    params = []
    if search:
        query += " AND (m.kod LIKE ? OR m.nazev LIKE ? OR m.dodavatel LIKE ?)"
        params += [f'%{search}%', f'%{search}%', f'%{search}%']
    if filtr == 'nedostatek':
        query += " AND s.min_skladem > 0 AND COALESCE(s.skutecny_stav, s.naskladneno - s.pouzito) < s.min_skladem"
    query += " ORDER BY m.oblibeny DESC, m.typ, m.nazev"

    c.execute(query, params)
    items = db_rows_to_list(c.fetchall())

    c.execute("""SELECT DISTINCT m.typ FROM materialy m
        WHERE m.typ IS NOT NULL AND m.typ != ''
          AND (
            m.zobrazovat = 1
            OR EXISTS (SELECT 1 FROM kusovniky k WHERE k.material_kod = m.kod)
            OR EXISTS (SELECT 1 FROM material_spojeniky ms WHERE ms.spojovaci_kod = m.kod)
          )
        ORDER BY m.typ""")
    typy = [r['typ'] for r in c.fetchall()]

    conn.close()
    return jsonify({'items': items, 'typy': typy})

@app.route('/api/sklad/pohyb', methods=['POST'])
def api_sklad_pohyb():
    data = request.json
    conn = get_db()
    c = conn.cursor()
    try:
        mnozstvi = float(data['mnozstvi'])
        typ = data['typ']  # 'prijem' nebo 'vydej'
        mat_kod = data['material_kod']

        c.execute("""
            INSERT INTO pohyby_skladu (material_kod, typ, mnozstvi, poznamka, uzivatel)
            VALUES (?,?,?,?,?)
        """, (mat_kod, typ, mnozstvi, data.get('poznamka',''), data.get('uzivatel','')))

        # Aktualizuj stav skladu
        if typ == 'prijem':
            c.execute("UPDATE sklad SET naskladneno = naskladneno + ?, updated_at=datetime('now') WHERE material_kod=?", (mnozstvi, mat_kod))
        elif typ == 'vydej':
            c.execute("UPDATE sklad SET pouzito = pouzito + ?, updated_at=datetime('now') WHERE material_kod=?", (mnozstvi, mat_kod))

        conn.commit()
        conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 400

@app.route('/api/sklad/inventura', methods=['POST'])
def api_sklad_inventura():
    """Zapsat fyzický stav z inventury"""
    data = request.json
    conn = get_db()
    c = conn.cursor()
    try:
        polozky = data.get('polozky', [])
        nazev = data.get('nazev', f'Inventura {date.today()}')

        c.execute("INSERT INTO inventury (datum, nazev, uzivatel) VALUES (?,?,?)",
                  (date.today().isoformat(), nazev, data.get('uzivatel','')))
        inv_id = c.lastrowid

        for p in polozky:
            mat_kod = p['material_kod']
            stav_fyzicky = float(p['stav_fyzicky'])

            # Aktuální vypočtený stav
            c.execute("SELECT naskladneno - pouzito as stav FROM sklad WHERE material_kod=?", (mat_kod,))
            row = c.fetchone()
            stav_pred = row['stav'] if row else 0

            c.execute("""
                INSERT INTO inventura_polozky (inventura_id, material_kod, stav_pred, stav_fyzicky)
                VALUES (?,?,?,?)
            """, (inv_id, mat_kod, stav_pred, stav_fyzicky))

            # Nastav skutečný stav
            c.execute("UPDATE sklad SET skutecny_stav=?, posledni_inventura=?, updated_at=datetime('now') WHERE material_kod=?",
                      (stav_fyzicky, date.today().isoformat(), mat_kod))

        c.execute("UPDATE inventury SET stav='dokončena' WHERE id=?", (inv_id,))
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'inventura_id': inv_id})
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 400

@app.route('/api/materialy/migrace-spojovaci', methods=['POST'])
def api_migrace_spojovaci():
    """Přetypuje nýty, šrouby, lepidlo a příbuzné položky na typ 'SPOJOVACÍ MAT.'"""
    conn = get_db()
    c = conn.cursor()
    # Klíčová slova v názvu nebo aktuální typ – case-insensitive
    kw_nazev = ['nýt', 'nyt', 'rivet', 'šroub', 'sroub', 'screw', 'lepidlo', 'glue', 'klih',
                'podložk', 'matice', 'závlačk', 'hmoždink', 'vrutu', 'vrut']
    kw_typ   = ['nýty', 'nyt', 'šrouby', 'sroub', 'lepidlo', 'spojov']
    conditions_nazev = ' OR '.join(["lower(nazev) LIKE ?" for _ in kw_nazev])
    conditions_typ   = ' OR '.join(["lower(typ) LIKE ?" for _ in kw_typ])
    params_nazev = [f'%{k}%' for k in kw_nazev]
    params_typ   = [f'%{k}%' for k in kw_typ]
    c.execute(f"""
        UPDATE materialy SET typ='SPOJOVACÍ MAT.'
        WHERE ({conditions_nazev}) OR ({conditions_typ})
    """, params_nazev + params_typ)
    updated = c.rowcount
    # Zkontroluj co bylo změněno
    c.execute("""SELECT kod, nazev, typ FROM materialy
        WHERE typ='SPOJOVACÍ MAT.' ORDER BY nazev LIMIT 200""")
    items = [dict(r) for r in c.fetchall()]
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'updated': updated, 'items': items})

@app.route('/api/sklad/min-sklad', methods=['POST'])
def api_sklad_min():
    data = request.json
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE sklad SET min_skladem=? WHERE material_kod=?",
              (data['min_skladem'], data['material_kod']))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ─── ZAKÁZKY API ──────────────────────────────────────────────────────────

@app.route('/api/zakazky')
def api_zakazky():
    conn = get_db()
    c = conn.cursor()
    stav = request.args.get('stav', '')
    search = request.args.get('q', '')
    limit = int(request.args.get('limit', 100))

    query = """
        SELECT z.*, t.nazev as case_nazev, t.typ_korpusu
        FROM zakazky z
        LEFT JOIN typy_casu t ON t.id = z.typ_casu_id
        WHERE 1=1
    """
    params = []
    if stav:
        query += " AND z.stav=?"
        params.append(stav)
    if search:
        query += " AND (z.hn_cislo LIKE ? OR z.nazev LIKE ? OR z.zakaznik LIKE ?)"
        params += [f'%{search}%', f'%{search}%', f'%{search}%']
    query += " ORDER BY z.prioritni DESC, z.created_at DESC LIMIT ?"
    params.append(limit)

    c.execute(query, params)
    items = db_rows_to_list(c.fetchall())
    conn.close()
    return jsonify({'items': items})

@app.route('/api/zakazky', methods=['POST'])
def api_zakazka_create():
    data = request.json
    conn = get_db()
    c = conn.cursor()
    try:
        # Najdi typ casu podle HN čísla
        typ_id = data.get('typ_casu_id')
        hn_cislo = data.get('hn_cislo', '')
        nazev = data.get('nazev', '')

        if not typ_id and hn_cislo:
            c.execute("SELECT id, nazev FROM typy_casu WHERE hn_cislo=?", (hn_cislo,))
            row = c.fetchone()
            if row:
                typ_id = row['id']
                if not nazev:
                    nazev = row['nazev']

        c.execute("""
            INSERT INTO zakazky (typ_casu_id, hn_cislo, nazev, stav, pocet_ks,
            termin, zakaznik, poznamka_dilna, poznamka_cnc, pracovnik, sn_cislo)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (typ_id, hn_cislo, nazev, data.get('stav','Čeká'),
              data.get('pocet_ks',1), data.get('termin',''), data.get('zakaznik',''),
              data.get('poznamka_dilna',''), data.get('poznamka_cnc',''),
              data.get('pracovnik',''), data.get('sn_cislo','')))
        new_id = c.lastrowid
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'id': new_id})
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 400

@app.route('/api/zakazky/<int:zak_id>', methods=['PUT'])
def api_zakazka_update(zak_id):
    data = request.json
    conn = get_db()
    c = conn.cursor()
    fields = ['stav','pocet_ks','termin','zakaznik','poznamka_dilna','poznamka_cnc',
              'pracovnik','sn_cislo','faktura_cislo','faktura_datum','datum_dokonceni','prioritni']
    updates = ', '.join(f"{f}=?" for f in fields if f in data)
    vals = [data[f] for f in fields if f in data]
    if 'stav' in data and data['stav'] == 'Hotovo' and 'datum_dokonceni' not in data:
        updates += ", datum_dokonceni=date('now')"
    if updates:
        c.execute(f"UPDATE zakazky SET {updates}, updated_at=datetime('now') WHERE id=?", vals + [zak_id])
        conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/zakazky/<int:zak_id>/odepis-material', methods=['POST'])
def api_odepis_material(zak_id):
    conn = get_db()
    ok = odepis_material_ze_skladu(conn, zak_id)
    conn.close()
    return jsonify({'ok': ok})

@app.route('/api/zakazky/<int:zak_id>/dostupnost')
def api_dostupnost(zak_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT typ_casu_id, pocet_ks FROM zakazky WHERE id=?", (zak_id,))
    zak = c.fetchone()
    if not zak or not zak['typ_casu_id']:
        conn.close()
        return jsonify({'chybi': []})
    chybi = zkontroluj_dostupnost_materialu(conn, zak['typ_casu_id'], zak['pocet_ks'])
    conn.close()
    return jsonify({'chybi': chybi})

# ─── VÝROBNÍ LIST (tisk pro montéra) ─────────────────────────────────────

@app.route('/api/zakazky/<int:zak_id>/vyrobni-list')
def api_vyrobni_list(zak_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT z.*, t.nazev as case_nazev, t.typ_korpusu,
               t.vnitrni_sirka, t.vnitrni_vyska, t.vnitrni_hloubka,
               t.cas_narocnost, t.poznamka as typ_poznamka,
               t.orientace_lid, t.pena_poznamka, t.pena_odkaz,
               t.prisl_1, t.prisl_2, t.prisl_3, t.prisl_4
        FROM zakazky z
        LEFT JOIN typy_casu t ON t.id = z.typ_casu_id
        WHERE z.id=?
    """, (zak_id,))
    zak = c.fetchone()
    if not zak:
        conn.close()
        return jsonify({'error': 'Zakázka nenalezena'}), 404

    bom = []
    profily_l = []
    profily_h = []

    spojeniky = []
    if zak['typ_casu_id']:
        pocet_ks = zak['pocet_ks']
        # BOM – hardware a materiály
        c.execute("""
            SELECT k.material_kod, k.mnozstvi, k.mnozstvi * ? as celkem,
                   m.nazev, m.typ, m.druh, m.umisteni, m.dodavatel, m.web_url, m.oblibeny,
                   COALESCE(s.skutecny_stav, s.naskladneno - s.pouzito, 0) as stav_skladu
            FROM kusovniky k
            JOIN materialy m ON m.kod = k.material_kod
            LEFT JOIN sklad s ON s.material_kod = k.material_kod
            WHERE k.typ_casu_id = ?
            ORDER BY m.oblibeny DESC, m.typ, m.nazev
        """, (pocet_ks, zak['typ_casu_id']))
        bom = db_rows_to_list(c.fetchall())

        # Spojovací materiál – agregovaný ze všech BOM položek
        spojeniky_agg = {}
        for polozka in bom:
            c.execute("""
                SELECT ms.spojovaci_kod, ms.mnozstvi_na_kus,
                       m.nazev, m.umisteni, m.oblibeny,
                       COALESCE(s.skutecny_stav, s.naskladneno - s.pouzito, 0) as stav_skladu
                FROM material_spojeniky ms
                JOIN materialy m ON m.kod = ms.spojovaci_kod
                LEFT JOIN sklad s ON s.material_kod = ms.spojovaci_kod
                WHERE ms.material_kod = ?
            """, (polozka['material_kod'],))
            for sp in c.fetchall():
                kod = sp['spojovaci_kod']
                mnozstvi = polozka['mnozstvi'] * sp['mnozstvi_na_kus'] * pocet_ks
                if kod in spojeniky_agg:
                    spojeniky_agg[kod]['celkem'] += mnozstvi
                else:
                    spojeniky_agg[kod] = {
                        'material_kod': kod,
                        'nazev': sp['nazev'],
                        'celkem': mnozstvi,
                        'umisteni': sp['umisteni'],
                        'oblibeny': sp['oblibeny'],
                        'stav_skladu': sp['stav_skladu'],
                    }
        for sp in spojeniky_agg.values():
            sp['celkem'] = round(sp['celkem'], 2)
        spojeniky = sorted(spojeniky_agg.values(), key=lambda x: x['nazev'])

        # Řezný plán – L profily
        c.execute("""
            SELECT poradi, ks, rozmer_mm, zakonceni
            FROM profily_plan
            WHERE typ_casu_id=? AND typ_profilu='L' AND ks > 0
            ORDER BY poradi
        """, (zak['typ_casu_id'],))
        profily_l = db_rows_to_list(c.fetchall())

        # Řezný plán – H profily
        c.execute("""
            SELECT poradi, ks, rozmer_mm, zakonceni
            FROM profily_plan
            WHERE typ_casu_id=? AND typ_profilu='H' AND ks > 0
            ORDER BY poradi
        """, (zak['typ_casu_id'],))
        profily_h = db_rows_to_list(c.fetchall())

        # Přimerguj per-zakázka data z pruvodni_profily (zarážka, rez, pid)
        if profily_l or profily_h:
            c.execute("SELECT COUNT(*) FROM pruvodni_profily WHERE zakazka_id=?", (zak_id,))
            if c.fetchone()[0] == 0:
                _pruvodni_init(conn, zak_id, zak['typ_casu_id'])

            # zarazka_2 nemusí existovat ve starší DB – přidá ji auto_migrate() při restartu
            _cols = {row[1] for row in c.execute("PRAGMA table_info(pruvodni_profily)")}
            _z2 = 'zarazka_2' if 'zarazka_2' in _cols else 'NULL'
            c.execute(f"""
                SELECT id as pid, typ_profilu, poradi, zarazka, {_z2} as zarazka_2, rez, zakonceni as rez_zakonceni
                FROM pruvodni_profily WHERE zakazka_id=?
            """, (zak_id,))
            pruv_map = {}
            for pr in c.fetchall():
                pruv_map[(pr['typ_profilu'], pr['poradi'])] = dict(pr)

            for p in profily_l:
                k = ('L', p['poradi'])
                if k in pruv_map:
                    p['pid']      = pruv_map[k]['pid']
                    p['zarazka']  = pruv_map[k]['zarazka']
                    p['zarazka_2']= pruv_map[k]['zarazka_2']
                    p['rez']      = pruv_map[k]['rez']
                    # H profily se nevrtají, L profily mají 2 zarážky – drill_passes se nepoužívají

            # H profily – zarážky nepoužíváme, nepřidáváme pid ani zarazka

    # Pracovní postupy – odkazy
    links = []
    if zak['typ_casu_id']:
        _lnk_tbl = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='typy_casu_links'").fetchone()
        if _lnk_tbl:
            c.execute("SELECT nazev, url FROM typy_casu_links WHERE typ_casu_id=? ORDER BY poradi, id", (zak['typ_casu_id'],))
            links = [dict(r) for r in c.fetchall()]

    conn.close()
    return jsonify({
        'zakazka': dict(zak),
        'bom': bom,
        'spojeniky': spojeniky,
        'profily_l': profily_l,
        'profily_h': profily_h,
        'links': links,
    })

# ─── PRŮVODKA MONTÁŽE ─────────────────────────────────────────────────────

def _pruvodni_init(conn, zakazka_id, typ_casu_id):
    """Inicializuje průvodka záznamy z profily_plan šablony, pokud ještě neexistují.
    Zarážky 1 a 2 = pozice zarážky děrovačky dle průchodů vrtáku.
    """
    c = conn.cursor()
    # L profily
    c.execute("""
        SELECT poradi, ks, rozmer_mm FROM profily_plan
        WHERE typ_casu_id=? AND typ_profilu='L' AND ks > 0
        ORDER BY poradi
    """, (typ_casu_id,))
    for row in c.fetchall():
        mm = row['rozmer_mm']
        passes = _drill_passes(mm)
        z1 = passes[0]['zarazka'] if len(passes) > 0 else None
        z2 = passes[1]['zarazka'] if len(passes) > 1 else None
        c.execute("""
            INSERT OR IGNORE INTO pruvodni_profily
            (zakazka_id, typ_profilu, poradi, ks, rozmer_mm, zarazka, zarazka_2, rez)
            VALUES (?, 'L', ?, ?, ?, ?, ?, '| |')
        """, (zakazka_id, row['poradi'], row['ks'], mm, z1, z2))
    # H profily – zarážky se nepoužívají
    c.execute("""
        SELECT poradi, ks, rozmer_mm, zakonceni FROM profily_plan
        WHERE typ_casu_id=? AND typ_profilu='H' AND ks > 0
        ORDER BY poradi
    """, (typ_casu_id,))
    for row in c.fetchall():
        mm = row['rozmer_mm']
        c.execute("""
            INSERT OR IGNORE INTO pruvodni_profily
            (zakazka_id, typ_profilu, poradi, ks, rozmer_mm, zakonceni)
            VALUES (?, 'H', ?, ?, ?, ?)
        """, (zakazka_id, row['poradi'], row['ks'], mm, row['zakonceni']))
    conn.commit()


def _drill_passes(L):
    """Vrátí seznam průchodů vrátáku pro profil délky L mm (rozteč 128 mm)."""
    import math
    if not L or L < 128:
        return []
    n = math.floor(L / 128)
    margin = (L - (n - 1) * 128) / 2
    holes = [round(margin + i * 128, 2) for i in range(n)]
    passes = []
    for idx in range(0, n, 6):
        batch = holes[idx:idx + 6]
        passes.append({'zarazka': batch[0], 'holes': batch, 'count': len(batch)})
    return passes


@app.route('/api/zakazky/<int:zak_id>/pruvodni-profily')
def api_pruvodni_get(zak_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT z.typ_casu_id, z.hn_cislo, z.pocet_ks,
               COALESCE(t.nazev, z.nazev) as nazev
        FROM zakazky z LEFT JOIN typy_casu t ON t.id = z.typ_casu_id
        WHERE z.id=?
    """, (zak_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Zakázka nenalezena'}), 404
    typ_casu_id = row['typ_casu_id']
    nazev = row['nazev'] or ''
    hn_cislo = row['hn_cislo'] or ''

    # Zkontroluj, zda průvodka již existuje
    c.execute("SELECT COUNT(*) FROM pruvodni_profily WHERE zakazka_id=?", (zak_id,))
    count = c.fetchone()[0]
    if count == 0 and typ_casu_id:
        _pruvodni_init(conn, zak_id, typ_casu_id)

    c.execute("""
        SELECT id, typ_profilu, poradi, ks, rozmer_mm, zarazka, rez, zakonceni, poznamka
        FROM pruvodni_profily WHERE zakazka_id=?
        ORDER BY typ_profilu, poradi
    """, (zak_id,))
    rows = db_rows_to_list(c.fetchall())
    conn.close()

    # Přidej vrtací průchody ke každému profilu
    for r in rows:
        r['drill_passes'] = _drill_passes(r['zarazka'])

    profily_l = [r for r in rows if r['typ_profilu'] == 'L']
    profily_h = [r for r in rows if r['typ_profilu'] == 'H']
    return jsonify({
        'nazev': nazev,
        'hn_cislo': hn_cislo,
        'profily_l': profily_l,
        'profily_h': profily_h,
    })


@app.route('/api/pruvodni-profily/<int:row_id>', methods=['PUT'])
def api_pruvodni_update(row_id):
    data = request.get_json() or {}
    conn = get_db()
    c = conn.cursor()
    fields = []
    vals = []
    _cols = {row[1] for row in c.execute("PRAGMA table_info(pruvodni_profily)")}
    allowed = ['ks', 'rozmer_mm', 'zarazka', 'rez', 'zakonceni', 'poznamka']
    if 'zarazka_2' in _cols:
        allowed.append('zarazka_2')
    for col in allowed:
        if col in data:
            fields.append(f"{col}=?")
            vals.append(data[col])
    if not fields:
        conn.close()
        return jsonify({'error': 'Žádná pole k aktualizaci'}), 400
    vals.append(row_id)
    c.execute(f"UPDATE pruvodni_profily SET {', '.join(fields)} WHERE id=?", vals)
    conn.commit()

    _z2 = 'zarazka_2' if 'zarazka_2' in _cols else 'NULL'
    c.execute(f"""
        SELECT id, typ_profilu, poradi, ks, rozmer_mm, zarazka, {_z2} as zarazka_2, rez, zakonceni, poznamka
        FROM pruvodni_profily WHERE id=?
    """, (row_id,))
    row = dict(c.fetchone())
    conn.close()
    return jsonify(row)


@app.route('/api/zakazky/<int:zak_id>/pruvodni-profily/reset', methods=['POST'])
def api_pruvodni_reset(zak_id):
    """Smaže průvodka záznamy a reinicializuje z šablony profily_plan."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT typ_casu_id FROM zakazky WHERE id=?", (zak_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Zakázka nenalezena'}), 404
    c.execute("DELETE FROM pruvodni_profily WHERE zakazka_id=?", (zak_id,))
    conn.commit()
    if row['typ_casu_id']:
        _pruvodni_init(conn, zak_id, row['typ_casu_id'])
    conn.close()
    return jsonify({'ok': True})


# ─── IMPORT ───────────────────────────────────────────────────────────────

@app.route('/api/import', methods=['POST'])
def api_import():
    """Import CSV souborů přes API"""
    import_type = request.form.get('type', '')
    if 'file' not in request.files:
        return jsonify({'error': 'Žádný soubor'}), 400

    import tempfile
    f = request.files['file']
    tmp_path = os.path.join(tempfile.gettempdir(), f'import_{import_type}.csv')
    f.save(tmp_path)

    try:
        from import_csv import import_material, import_vhw, get_db as iget_db
        conn = iget_db()
        if import_type == 'material':
            n = import_material(tmp_path, conn)
        elif import_type == 'vhw':
            n = import_vhw(tmp_path, conn)
        else:
            return jsonify({'error': 'Neznámý typ importu'}), 400
        conn.close()
        return jsonify({'ok': True, 'imported': n})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ─── DODAVATELÉ API ──────────────────────────────────────────────────────────

@app.route('/api/dodavatele')
def api_dodavatele():
    conn = get_db()
    c = conn.cursor()
    search = request.args.get('q', '')
    query = "SELECT * FROM dodavatele WHERE aktivni=1"
    params = []
    if search:
        query += " AND (nazev LIKE ? OR zkratka LIKE ? OR email LIKE ?)"
        params += [f'%{search}%', f'%{search}%', f'%{search}%']
    query += " ORDER BY nazev"
    c.execute(query, params)
    items = db_rows_to_list(c.fetchall())
    conn.close()
    return jsonify({'items': items})

@app.route('/api/dodavatele', methods=['POST'])
def api_dodavatel_create():
    data = request.json
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("""
            INSERT INTO dodavatele (nazev, zkratka, kontakt_jmeno, email, telefon,
            web, adresa, ic, dic, splatnost_dni, dodaci_lhuta_dni, mena, poznamka)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (data['nazev'], data.get('zkratka',''), data.get('kontakt_jmeno',''),
              data.get('email',''), data.get('telefon',''), data.get('web',''),
              data.get('adresa',''), data.get('ic',''), data.get('dic',''),
              data.get('splatnost_dni',14), data.get('dodaci_lhuta_dni',14),
              data.get('mena','CZK'), data.get('poznamka','')))
        conn.commit()
        dodavatel_id = c.lastrowid
        conn.close()
        return jsonify({'ok': True, 'id': dodavatel_id})
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 400

@app.route('/api/dodavatele/<int:did>', methods=['PUT'])
def api_dodavatel_update(did):
    data = request.json
    conn = get_db()
    c = conn.cursor()
    fields = ['nazev','zkratka','kontakt_jmeno','email','telefon','web',
              'adresa','ic','dic','splatnost_dni','dodaci_lhuta_dni','mena','poznamka','aktivni']
    updates = ', '.join(f"{f}=?" for f in fields if f in data)
    vals = [data[f] for f in fields if f in data]
    if updates:
        c.execute(f"UPDATE dodavatele SET {updates}, updated_at=datetime('now') WHERE id=?", vals + [did])
        conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/dodavatele/<int:did>', methods=['DELETE'])
def api_dodavatel_delete(did):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE dodavatele SET aktivni=0 WHERE id=?", (did,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ─── PŘÍJEMKY API ─────────────────────────────────────────────────────────────

@app.route('/api/prijemky')
def api_prijemky():
    conn = get_db()
    c = conn.cursor()
    limit = int(request.args.get('limit', 100))
    c.execute("""
        SELECT p.*, d.nazev as dodavatel_nazev,
               COUNT(pp.id) as pocet_polozek,
               COALESCE(SUM(pp.cena_celkem),0) as celkova_cena
        FROM prijemky p
        LEFT JOIN dodavatele d ON d.id = p.dodavatel_id
        LEFT JOIN prijemky_polozky pp ON pp.prijemka_id = p.id
        GROUP BY p.id
        ORDER BY p.datum DESC, p.id DESC
        LIMIT ?
    """, (limit,))
    items = db_rows_to_list(c.fetchall())
    conn.close()
    return jsonify({'items': items})

@app.route('/api/prijemky', methods=['POST'])
def api_prijemka_create():
    data = request.json
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("""
            INSERT INTO prijemky (cislo, dodavatel_id, datum, poznamka, uzivatel, mena, kurz)
            VALUES (?,?,?,?,?,?,?)
        """, (data.get('cislo',''), data.get('dodavatel_id'),
              data.get('datum', date.today().isoformat()),
              data.get('poznamka',''), data.get('uzivatel',''),
              data.get('mena', 'CZK'), float(data.get('kurz', 1.0))))
        conn.commit()
        prijemka_id = c.lastrowid
        conn.close()
        return jsonify({'ok': True, 'id': prijemka_id})
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 400

@app.route('/api/prijemky/<int:pid>')
def api_prijemka_detail(pid):
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT p.*, d.nazev as dodavatel_nazev
        FROM prijemky p
        LEFT JOIN dodavatele d ON d.id = p.dodavatel_id
        WHERE p.id=?
    """, (pid,))
    prijemka = c.fetchone()
    if not prijemka:
        conn.close()
        return jsonify({'error': 'Příjemka nenalezena'}), 404
    c.execute("""
        SELECT pp.*, m.nazev as material_nazev, m.typ, m.umisteni
        FROM prijemky_polozky pp
        JOIN materialy m ON m.kod = pp.material_kod
        WHERE pp.prijemka_id=?
        ORDER BY m.nazev
    """, (pid,))
    polozky = db_rows_to_list(c.fetchall())
    conn.close()
    return jsonify({'prijemka': dict(prijemka), 'polozky': polozky})

@app.route('/api/prijemky/<int:pid>/polozka', methods=['POST'])
def api_prijemka_add_polozku(pid):
    data = request.json
    conn = get_db()
    c = conn.cursor()
    try:
        mnozstvi = float(data['mnozstvi'])
        cena_j = float(data.get('cena_jednotka', 0))
        cena_c = mnozstvi * cena_j
        c.execute("""
            INSERT INTO prijemky_polozky (prijemka_id, material_kod, mnozstvi, cena_jednotka, cena_celkem)
            VALUES (?,?,?,?,?)
            ON CONFLICT(prijemka_id, material_kod) DO UPDATE SET
                mnozstvi = mnozstvi + excluded.mnozstvi,
                cena_celkem = cena_celkem + excluded.cena_celkem
        """, (pid, data['material_kod'], mnozstvi, cena_j, cena_c))
        conn.commit()
        conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 400

@app.route('/api/prijemky/<int:pid>/polozka/<mat_kod>', methods=['DELETE'])
def api_prijemka_remove_polozku(pid, mat_kod):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM prijemky_polozky WHERE prijemka_id=? AND material_kod=?", (pid, mat_kod))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/prijemky/<int:pid>/nastaveni', methods=['POST'])
def api_prijemka_nastaveni(pid):
    """Aktualizuje dopravné, měnu a kurz příjemky (jen pokud není zaúčtována)."""
    data = request.json
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT stav FROM prijemky WHERE id=?", (pid,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Příjemka nenalezena'}), 404
    if row['stav'] == 'zaúčtováno':
        conn.close()
        return jsonify({'error': 'Nelze měnit zaúčtovanou příjemku'}), 400
    fields, vals = [], []
    for col in ('dopravne', 'mena', 'kurz'):
        if col in data:
            fields.append(f"{col}=?")
            vals.append(data[col])
    if fields:
        vals.append(pid)
        c.execute(f"UPDATE prijemky SET {', '.join(fields)} WHERE id=?", vals)
        conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/prijemky/<int:pid>/zauctovat', methods=['POST'])
def api_prijemka_zauctovat(pid):
    """Zaúčtuje příjemku — naskladní všechny položky do skladu.
    Distribuuje dopravné váženým průměrem dle hodnoty položky.
    Pokud je měna EUR, přepočítá ceny kurzem na CZK.
    Aktualizuje nc_bez_dph v katalogu materiálů.
    """
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM prijemky WHERE id=?", (pid,))
    prijemka = c.fetchone()
    if not prijemka:
        conn.close()
        return jsonify({'error': 'Příjemka nenalezena'}), 404
    if prijemka['stav'] == 'zaúčtováno':
        conn.close()
        return jsonify({'error': 'Příjemka již byla zaúčtována'}), 400

    c.execute("""
        SELECT material_kod, mnozstvi, cena_jednotka, cena_celkem
        FROM prijemky_polozky WHERE prijemka_id=?
    """, (pid,))
    polozky = c.fetchall()

    dopravne  = float(prijemka['dopravne'] or 0)
    mena      = prijemka['mena'] or 'CZK'
    kurz      = float(prijemka['kurz'] or 1.0)
    total_val = sum(float(p['cena_celkem'] or 0) for p in polozky)

    for p in polozky:
        mnozstvi = float(p['mnozstvi'])
        cena_j   = float(p['cena_jednotka'] or 0)
        cena_c   = float(p['cena_celkem'] or 0)

        # 1. Přepočet měny EUR → CZK
        if mena == 'EUR' and kurz > 0:
            cena_j = round(cena_j * kurz, 4)
            cena_c = round(cena_c * kurz, 4)

        # 2. Distribuce dopravného váženým průměrem dle hodnoty položky
        if dopravne > 0 and total_val > 0 and mnozstvi > 0:
            freight_share = dopravne * (cena_c / total_val)
            cena_j = round(cena_j + freight_share / mnozstvi, 4)
            cena_c = round(cena_j * mnozstvi, 4)
            poznamka = f'Příjemka #{pid} · dopravné {dopravne:.2f} Kč'
        else:
            poznamka = f'Příjemka #{pid}'
        if mena == 'EUR':
            poznamka += f' · kurz {kurz} CZK/EUR'

        # 3. Pohyb skladu (s vazbou na příjemku pro přesné storno)
        c.execute("""
            INSERT INTO pohyby_skladu (material_kod, typ, mnozstvi, poznamka, prijemka_id)
            VALUES (?, 'prijem', ?, ?, ?)
        """, (p['material_kod'], mnozstvi, poznamka, pid))

        # 4. Aktualizuj NC v katalogu (přepíše na novou nakupní cenu vč. dopravného)
        if cena_j > 0:
            c.execute("""
                UPDATE materialy SET nc_bez_dph=?, updated_at=datetime('now') WHERE kod=?
            """, (cena_j, p['material_kod']))

        # 5. Zajisti řádek ve skladu
        c.execute("""
            INSERT OR IGNORE INTO sklad (material_kod, naskladneno, pouzito, skutecny_stav)
            VALUES (?, 0, 0, 0)
        """, (p['material_kod'],))
        aktualizuj_stav_skladu(conn, p['material_kod'])

    c.execute("UPDATE prijemky SET stav='zaúčtováno' WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'naskladneno': len(polozky)})

@app.route('/api/prijemky/<int:pid>/nahled')
def api_prijemka_nahled(pid):
    """Vypočte finální ceny (měna + dopravné) bez zápisu do DB — pro kontrolní náhled."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM prijemky WHERE id=?", (pid,))
    pr = c.fetchone()
    if not pr:
        conn.close()
        return jsonify({'error': 'Příjemka nenalezena'}), 404
    c.execute("""
        SELECT pp.*, m.nazev as material_nazev, m.umisteni
        FROM prijemky_polozky pp
        JOIN materialy m ON m.kod = pp.material_kod
        WHERE pp.prijemka_id=?
        ORDER BY m.nazev
    """, (pid,))
    polozky = c.fetchall()
    conn.close()

    dopravne  = float(pr['dopravne'] or 0)
    mena      = pr['mena'] or 'CZK'
    kurz      = float(pr['kurz'] or 1.0)
    total_val = sum(float(p['cena_celkem'] or 0) for p in polozky)

    result = []
    for p in polozky:
        mn   = float(p['mnozstvi'])
        cj   = float(p['cena_jednotka'] or 0)
        cc   = float(p['cena_celkem'] or 0)
        # EUR → CZK
        cj_czk = round(cj * kurz, 4) if mena == 'EUR' else cj
        # Dopravné
        freight_share = 0.0
        if dopravne > 0 and total_val > 0 and mn > 0:
            freight_share = dopravne * (cc / total_val)
            cj_fin = round(cj_czk + freight_share / mn, 4)
        else:
            cj_fin = cj_czk
        cc_fin = round(cj_fin * mn, 4)
        result.append({
            'material_kod':   p['material_kod'],
            'material_nazev': p['material_nazev'],
            'umisteni':       p['umisteni'],
            'mnozstvi':       mn,
            'cena_j_orig':    cj,
            'mena':           mena,
            'cena_j_czk':     round(cj_czk, 4) if mena == 'EUR' else None,
            'freight_share':  round(freight_share, 4),
            'cena_j_fin':     cj_fin,
            'cena_celkem_fin': cc_fin,
        })

    total_fin = sum(r['cena_celkem_fin'] for r in result)
    return jsonify({
        'ok': True, 'polozky': result,
        'total_fin': round(total_fin, 2),
        'dopravne': dopravne, 'mena': mena, 'kurz': kurz,
    })


@app.route('/api/prijemky/<int:pid>', methods=['DELETE'])
def api_prijemka_delete(pid):
    """Smaže příjemku. Pro zaúčtované příjemky provede storno pohybů (obrátí naskladnění)."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM prijemky WHERE id=?", (pid,))
    pr = c.fetchone()
    if not pr:
        conn.close()
        return jsonify({'error': 'Příjemka nenalezena'}), 404

    if pr['stav'] == 'zaúčtováno':
        # Zjisti dotčené materiály
        c.execute("""
            SELECT DISTINCT material_kod FROM pohyby_skladu WHERE prijemka_id=?
        """, (pid,))
        dotcene = [r['material_kod'] for r in c.fetchall()]
        # Smaž pohyby příjemky — aktualizuj_stav_skladu přepočítá součty
        c.execute("DELETE FROM pohyby_skladu WHERE prijemka_id=?", (pid,))
        for mat_kod in dotcene:
            aktualizuj_stav_skladu(conn, mat_kod)

    c.execute("DELETE FROM prijemky_polozky WHERE prijemka_id=?", (pid,))
    c.execute("DELETE FROM prijemky WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ─── PROŘEZ API ───────────────────────────────────────────────────────────

@app.route('/api/prorez')
def api_prorez():
    conn = get_db()
    c = conn.cursor()
    # Vrátí prořez pro všechny typy materiálů (existující i nové)
    c.execute("""
        SELECT m.typ, COALESCE(p.procento, 0) AS procento
        FROM (SELECT DISTINCT typ FROM materialy WHERE typ IS NOT NULL AND typ != '') m
        LEFT JOIN prorez p ON p.typ = m.typ
        ORDER BY m.typ
    """)
    items = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify({'prorez': items})

@app.route('/api/prorez', methods=['POST'])
def api_prorez_save():
    """Uloží prořez pro jeden nebo více typů najednou.
    Body: { "typ": "DESKA", "procento": 20 }
      nebo { "items": [{"typ": ..., "procento": ...}, ...] }
    """
    d = request.get_json()
    items = d.get('items') or [{'typ': d.get('typ'), 'procento': d.get('procento', 0)}]
    conn = get_db()
    c = conn.cursor()
    for item in items:
        typ = item.get('typ')
        pct = float(item.get('procento', 0))
        if not typ:
            continue
        c.execute("INSERT OR REPLACE INTO prorez (typ, procento) VALUES (?, ?)", (typ, pct))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ─── OPRAVNÉ DOKLADY API ──────────────────────────────────────────────────

@app.route('/api/opravne-doklady')
def api_opravne_doklady():
    """Seznam opravných dokladů s možností filtrování."""
    conn = get_db()
    c = conn.cursor()
    typ    = request.args.get('typ')
    mat    = request.args.get('material_kod')
    od_dat = request.args.get('od')
    do_dat = request.args.get('do')
    limit  = int(request.args.get('limit', 200))

    q = """
        SELECT od.*, m.nazev as material_nazev, m.jednotka, m.typ as material_typ
        FROM opravne_doklady od
        JOIN materialy m ON m.kod = od.material_kod
        WHERE 1=1
    """
    params = []
    if typ:
        q += " AND od.typ=?"; params.append(typ)
    if mat:
        q += " AND od.material_kod=?"; params.append(mat)
    if od_dat:
        q += " AND od.datum>=?"; params.append(od_dat)
    if do_dat:
        q += " AND od.datum<=?"; params.append(do_dat)
    q += " ORDER BY od.datum DESC, od.id DESC LIMIT ?"
    params.append(limit)

    c.execute(q, params)
    items = db_rows_to_list(c.fetchall())
    conn.close()
    return jsonify({'items': items})


@app.route('/api/opravne-doklady', methods=['POST'])
def api_opravny_doklad_create():
    """Vytvoří opravný doklad a zapíše pohyb do skladu."""
    d = request.get_json()
    typ      = d.get('typ')         # 'manko' nebo 'prebytek'
    mat_kod  = d.get('material_kod')
    mnozstvi = float(d.get('mnozstvi', 0))
    cena     = float(d.get('cena_bez_dph', 0))
    datum    = d.get('datum') or date.today().isoformat()
    poznamka = d.get('poznamka', '')
    uzivatel = d.get('uzivatel', '')

    if typ not in ('manko', 'prebytek'):
        return jsonify({'error': 'Typ musí být manko nebo prebytek'}), 400
    if not mat_kod or mnozstvi <= 0:
        return jsonify({'error': 'Materiál a množství jsou povinné'}), 400

    conn = get_db()
    c = conn.cursor()

    # Ověř existenci materiálu
    c.execute("SELECT kod FROM materialy WHERE kod=?", (mat_kod,))
    if not c.fetchone():
        conn.close()
        return jsonify({'error': 'Materiál nenalezen'}), 404

    # Vlož doklad
    c.execute("""
        INSERT INTO opravne_doklady
            (datum, typ, material_kod, mnozstvi, cena_bez_dph, poznamka, uzivatel)
        VALUES (?,?,?,?,?,?,?)
    """, (datum, typ, mat_kod, mnozstvi, cena, poznamka, uzivatel))
    oid = c.lastrowid

    # Pohyb skladu — manko = vydej, přebytek = prijem
    pohyb_typ = 'vydej' if typ == 'manko' else 'prijem'
    label = 'Manko' if typ == 'manko' else 'Přebytek'
    poz_pohyb = f'{label} — opravný doklad #{oid}'
    if poznamka:
        poz_pohyb += f': {poznamka}'

    c.execute("""
        INSERT INTO pohyby_skladu
            (material_kod, typ, mnozstvi, datum, poznamka, uzivatel, opravny_doklad_id)
        VALUES (?,?,?,?,?,?,?)
    """, (mat_kod, pohyb_typ, mnozstvi, datum, poz_pohyb, uzivatel, oid))

    # Zajisti řádek ve skladu + přepočítej
    c.execute("""
        INSERT OR IGNORE INTO sklad (material_kod, naskladneno, pouzito, skutecny_stav)
        VALUES (?, 0, 0, 0)
    """, (mat_kod,))
    aktualizuj_stav_skladu(conn, mat_kod)

    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'id': oid})


@app.route('/api/opravne-doklady/<int:oid>', methods=['DELETE'])
def api_opravny_doklad_delete(oid):
    """Smaže opravný doklad a obrátí pohyb skladu."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM opravne_doklady WHERE id=?", (oid,))
    od = c.fetchone()
    if not od:
        conn.close()
        return jsonify({'error': 'Doklad nenalezen'}), 404

    # Stornuj pohyb
    c.execute("DELETE FROM pohyby_skladu WHERE opravny_doklad_id=?", (oid,))
    aktualizuj_stav_skladu(conn, od['material_kod'])

    c.execute("DELETE FROM opravne_doklady WHERE id=?", (oid,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


def _mat_jednotka(typ):
    """Vrátí textovou jednotku ('m2', 'm', 'ks') podle typu materiálu."""
    nt = (typ or '').upper()
    # Odstranění diakritiky (jednoduché)
    nt = nt.replace('Á','A').replace('Č','C').replace('Š','S').replace('Ž','Z') \
           .replace('Ě','E').replace('Í','I').replace('Ý','Y').replace('Ú','U') \
           .replace('Ů','U').replace('Ó','O').replace('É','E')
    if any(k in nt for k in ('DESKA','PREKLIZK','PLAYWOOD','PLAST','PEN','FOAM','BALDACHIN','BALDACYN')):
        return 'm2'
    if any(k in nt for k in ('PROFIL','HLINIK','LIŠTA','LISTU')):
        return 'm'
    return 'ks'


@app.route('/api/materialy/<kod>/fifo-cena')
def api_material_fifo_cena(kod):
    """Vrátí FIFO cenu materiálu přepočítanou na správnou jednotku (m², m nebo ks)."""
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT m.nc_bez_dph, m.nakup_jednotka, m.nakup_baleni, m.nazev, m.typ, m.balenf,
               (SELECT pp.cena_jednotka FROM prijemky_polozky pp
                JOIN prijemky pr ON pr.id = pp.prijemka_id
                WHERE pp.material_kod = m.kod AND pr.stav = 'zaúčtováno'
                ORDER BY pr.datum DESC, pr.id DESC LIMIT 1
               ) as posledni_nc
        FROM materialy m WHERE m.kod=?
    """, (kod,))
    row = c.fetchone()
    # Zkus také FIFO dávky
    fifo_davky_cena = _fifo_cena(c, kod) if row else 0
    if not row:
        conn.close()
        return jsonify({'error': 'Materiál nenalezen'}), 404

    # Cena za kus/sheet: FIFO → poslední příjemka → nc_bez_dph → nakup_jednotka → nakup_baleni
    cena_za_kus = (
        fifo_davky_cena
        or float(row['posledni_nc'] or 0)
        or float(row['nc_bez_dph'] or 0)
        or float(row['nakup_jednotka'] or 0)
        or float(row['nakup_baleni'] or 0)
        or 0
    )
    balenf = float(row['balenf'] or 0) or 1   # 0 → fallback 1
    jednotka = _mat_jednotka(row['typ'])

    # Přepočet na správnou jednotku (m², m) – cena_za_kus ÷ balenf
    if jednotka in ('m2', 'm'):
        cena_per_unit = round(cena_za_kus / balenf, 4)
    else:
        cena_per_unit = round(cena_za_kus, 4)

    prorez_pct = _get_prorez(c, row['typ'])
    conn.close()

    return jsonify({
        'cena':            cena_per_unit,   # cena v Kč na jednotku (m², m nebo ks)
        'cena_za_kus':     cena_za_kus,
        'balenf':          balenf,
        'jednotka':        jednotka,
        'nazev':           row['nazev'],
        'typ':             row['typ'],
        'prorez_procento': prorez_pct,
        # Debug – z čeho cena pochází:
        '_fifo_davky':     fifo_davky_cena,
        '_posledni_nc':    float(row['posledni_nc'] or 0),
        '_nc_bez_dph':     float(row['nc_bez_dph'] or 0),
        '_nakup_jednotka': float(row['nakup_jednotka'] or 0),
        '_nakup_baleni':   float(row['nakup_baleni'] or 0),
    })


def _get_prorez(c, typ):
    """Vrátí prořez v % pro daný typ materiálu (0 = bez prořezu)."""
    c.execute("SELECT procento FROM prorez WHERE typ=?", (typ or '',))
    r = c.fetchone()
    return float(r[0]) if r and r[0] else 0.0


# ─── UŽIVATELÉ API ────────────────────────────────────────────────────────

@app.route('/api/uzivatele')
def api_uzivatele():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM uzivatele ORDER BY jmeno")
    items = db_rows_to_list(c.fetchall())
    conn.close()
    return jsonify({'uzivatele': items})

@app.route('/api/uzivatele', methods=['POST'])
def api_uzivatel_create():
    d = request.get_json()
    if not d.get('jmeno'):
        return jsonify({'error': 'Jméno je povinné'}), 400
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT INTO uzivatele (jmeno, role, barva) VALUES (?,?,?)",
              (d['jmeno'].strip(), d.get('role','Dílna'), d.get('barva','#3b82f6')))
    uid = c.lastrowid
    conn.commit()
    c.execute("SELECT * FROM uzivatele WHERE id=?", (uid,))
    row = dict(c.fetchone())
    conn.close()
    return jsonify({'ok': True, 'uzivatel': row})

@app.route('/api/uzivatele/<int:uid>', methods=['POST'])
def api_uzivatel_update(uid):
    d = request.get_json()
    conn = get_db()
    c = conn.cursor()
    fields = []
    vals = []
    for col in ('jmeno', 'role', 'barva', 'aktivni'):
        if col in d:
            fields.append(f"{col}=?")
            vals.append(d[col])
    if not fields:
        conn.close()
        return jsonify({'error': 'Nic k aktualizaci'}), 400
    vals.append(uid)
    c.execute(f"UPDATE uzivatele SET {', '.join(fields)} WHERE id=?", vals)
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/uzivatele/<int:uid>', methods=['DELETE'])
def api_uzivatel_delete(uid):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE uzivatele SET aktivni=0 WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ─── KANCELÁŘ – ZAKÁZKY API ───────────────────────────────────────────────

def _kancelar_sync_zakaznik(c, nazev, tel, mail):
    """Uloží nebo aktualizuje zákazníka v číselníku zákazníků."""
    if not nazev:
        return
    c.execute("SELECT id FROM kancelar_zakaznici WHERE nazev=?", (nazev,))
    row = c.fetchone()
    if row:
        if tel or mail:
            c.execute("UPDATE kancelar_zakaznici SET tel=COALESCE(NULLIF(?,''),tel), mail=COALESCE(NULLIF(?,''),mail) WHERE id=?",
                      (tel, mail, row[0]))
    else:
        c.execute("INSERT INTO kancelar_zakaznici (nazev, tel, mail) VALUES (?,?,?)", (nazev, tel, mail))

def _kancelar_zakazka_dict(conn, row):
    """Doplní štítky a řešitele k řádku kancelar_zakazky."""
    d = dict(row)
    c = conn.cursor()
    c.execute("""
        SELECT s.id, s.nazev, s.barva
        FROM kancelar_zakazky_stitky ks
        JOIN kancelar_stitky s ON s.id = ks.stitek_id
        WHERE ks.zakazka_id = ?
        ORDER BY s.poradi
    """, (d['id'],))
    d['stitky'] = db_rows_to_list(c.fetchall())
    if d.get('resitel_id'):
        c.execute("SELECT id, jmeno, barva, role FROM uzivatele WHERE id=?", (d['resitel_id'],))
        r = c.fetchone()
        d['resitel'] = dict(r) if r else None
    else:
        d['resitel'] = None
    return d

@app.route('/api/kancelar/zakazky')
def api_kancelar_zakazky():
    conn = get_db()
    c = conn.cursor()
    q       = request.args.get('q', '')
    aktivni = request.args.get('aktivni', '1')   # '1'=aktivní, '0'=neaktivní, ''=vše
    sql  = "SELECT kz.* FROM kancelar_zakazky kz WHERE 1=1"
    params = []
    if q:
        sql += " AND (kz.nazev LIKE ? OR kz.zakaznik LIKE ? OR kz.hn_kod LIKE ?)"
        params += [f'%{q}%', f'%{q}%', f'%{q}%']
    if aktivni in ('0', '1'):
        sql += " AND kz.aktivni=?"
        params.append(int(aktivni))
    sql += " ORDER BY kz.id DESC"
    c.execute(sql, params)
    rows = c.fetchall()
    zakazky = [_kancelar_zakazka_dict(conn, r) for r in rows]
    conn.close()
    return jsonify({'zakazky': zakazky})

@app.route('/api/kancelar/zakazky/<int:kid>')
def api_kancelar_zakazka_detail(kid):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM kancelar_zakazky WHERE id=?", (kid,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Nenalezena'}), 404
    result = _kancelar_zakazka_dict(conn, row)
    conn.close()
    return jsonify({'zakazka': result})

@app.route('/api/kancelar/zakazky', methods=['POST'])
def api_kancelar_zakazka_create():
    d = request.get_json()
    if not d.get('nazev'):
        return jsonify({'error': 'Název je povinný'}), 400
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO kancelar_zakazky
            (nazev, zakaznik, tel, mail, hn_kod, popis, resitel_id, priorita, termin,
             co_hotovo, aktivni, nabidka_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,1,?)
    """, (d['nazev'].strip(), d.get('zakaznik',''), d.get('tel',''), d.get('mail',''),
          d.get('hn_kod',''), d.get('popis',''),
          d.get('resitel_id') or None, d.get('priorita','Střední'), d.get('termin') or None,
          d.get('co_hotovo') or None, d.get('nabidka_id') or None))
    kid = c.lastrowid
    for sid in (d.get('stitky') or []):
        c.execute("INSERT OR IGNORE INTO kancelar_zakazky_stitky (zakazka_id, stitek_id) VALUES (?,?)", (kid, sid))
    # Uložit/aktualizovat zákazníka v číselníku
    _kancelar_sync_zakaznik(c, d.get('zakaznik',''), d.get('tel',''), d.get('mail',''))
    conn.commit()
    c.execute("SELECT * FROM kancelar_zakazky WHERE id=?", (kid,))
    result = _kancelar_zakazka_dict(conn, c.fetchone())
    conn.close()
    return jsonify({'ok': True, 'zakazka': result})

@app.route('/api/kancelar/zakazky/<int:kid>', methods=['POST'])
def api_kancelar_zakazka_update(kid):
    d = request.get_json()
    conn = get_db()
    c = conn.cursor()
    fields, vals = [], []
    nullable = ('resitel_id','termin','vyrobni_zakazka_id','co_hotovo','nabidka_id')
    allowed  = ('nazev','zakaznik','tel','mail','hn_kod','popis','resitel_id',
                'priorita','termin','vyrobni_zakazka_id','co_hotovo','aktivni','nabidka_id')
    for col in allowed:
        if col in d:
            fields.append(f"{col}=?")
            vals.append(d[col] or None if col in nullable else d[col])
    if fields:
        vals += [datetime.now().isoformat(), kid]
        c.execute(f"UPDATE kancelar_zakazky SET {', '.join(fields)}, updated_at=? WHERE id=?", vals)
    if 'stitky' in d:
        c.execute("DELETE FROM kancelar_zakazky_stitky WHERE zakazka_id=?", (kid,))
        for sid in d['stitky']:
            c.execute("INSERT OR IGNORE INTO kancelar_zakazky_stitky (zakazka_id, stitek_id) VALUES (?,?)", (kid, sid))
    if any(k in d for k in ('zakaznik','tel','mail')):
        c.execute("SELECT zakaznik, tel, mail FROM kancelar_zakazky WHERE id=?", (kid,))
        row = c.fetchone()
        if row:
            _kancelar_sync_zakaznik(c, row['zakaznik'] or '', row['tel'] or '', row['mail'] or '')
    conn.commit()
    c.execute("SELECT * FROM kancelar_zakazky WHERE id=?", (kid,))
    result = _kancelar_zakazka_dict(conn, c.fetchone())
    conn.close()
    return jsonify({'ok': True, 'zakazka': result})

@app.route('/api/kancelar/zakazky/<int:kid>', methods=['DELETE'])
def api_kancelar_zakazka_delete(kid):
    conn = get_db()
    c = conn.cursor()
    # Smaž fyzické soubory příloh
    c.execute("SELECT filepath FROM kancelar_prilohy WHERE zakazka_id=?", (kid,))
    for (fp,) in c.fetchall():
        try:
            if os.path.exists(fp):
                os.remove(fp)
        except Exception:
            pass
    c.execute("DELETE FROM kancelar_zakazky WHERE id=?", (kid,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ── KANCELÁŘ – POZNÁMKY ───────────────────────────────────────────────────

@app.route('/api/kancelar/zakazky/<int:kid>/poznamky')
def api_kan_poznamky_list(kid):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM kancelar_poznamky WHERE zakazka_id=? ORDER BY created_at", (kid,))
    items = db_rows_to_list(c.fetchall())
    conn.close()
    return jsonify({'poznamky': items})

@app.route('/api/kancelar/zakazky/<int:kid>/poznamky', methods=['POST'])
def api_kan_poznamka_create(kid):
    d = request.get_json()
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO kancelar_poznamky (zakazka_id, obsah, uzivatel)
        VALUES (?,?,?)
    """, (kid, d.get('obsah',''), d.get('uzivatel','')))
    nid = c.lastrowid
    conn.commit()
    c.execute("SELECT * FROM kancelar_poznamky WHERE id=?", (nid,))
    row = dict(c.fetchone())
    conn.close()
    return jsonify({'ok': True, 'poznamka': row})

@app.route('/api/kancelar/poznamky/<int:nid>', methods=['POST'])
def api_kan_poznamka_update(nid):
    d = request.get_json()
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE kancelar_poznamky SET obsah=?, updated_at=datetime('now') WHERE id=?",
              (d.get('obsah',''), nid))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/kancelar/poznamky/<int:nid>', methods=['DELETE'])
def api_kan_poznamka_delete(nid):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM kancelar_poznamky WHERE id=?", (nid,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ── KANCELÁŘ – PŘÍLOHY ────────────────────────────────────────────────────

PRILOHY_DIR = os.path.join('/data', 'prilohy') if os.path.isdir('/data') else os.path.join(os.path.dirname(__file__), 'data', 'prilohy')

@app.route('/api/kancelar/zakazky/<int:kid>/prilohy')
def api_kan_prilohy_list(kid):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM kancelar_prilohy WHERE zakazka_id=? ORDER BY created_at", (kid,))
    items = db_rows_to_list(c.fetchall())
    conn.close()
    return jsonify({'prilohy': items})

@app.route('/api/kancelar/zakazky/<int:kid>/prilohy', methods=['POST'])
def api_kan_priloha_upload(kid):
    if 'file' not in request.files:
        return jsonify({'error': 'Žádný soubor'}), 400
    f = request.files['file']
    folder = os.path.join(PRILOHY_DIR, str(kid))
    os.makedirs(folder, exist_ok=True)
    # Bezpečný název souboru
    safe_name = f.filename.replace('/', '_').replace('\\', '_')
    filepath = os.path.join(folder, safe_name)
    # Pokud soubor existuje, přidej číslo
    base, ext = os.path.splitext(safe_name)
    counter = 1
    while os.path.exists(filepath):
        safe_name = f"{base}_{counter}{ext}"
        filepath = os.path.join(folder, safe_name)
        counter += 1
    f.save(filepath)
    velikost = os.path.getsize(filepath)
    mime = f.content_type or 'application/octet-stream'
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO kancelar_prilohy (zakazka_id, filename, filepath, mime_type, velikost)
        VALUES (?,?,?,?,?)
    """, (kid, safe_name, filepath, mime, velikost))
    fid = c.lastrowid
    conn.commit()
    c.execute("SELECT * FROM kancelar_prilohy WHERE id=?", (fid,))
    row = dict(c.fetchone())
    conn.close()
    return jsonify({'ok': True, 'priloha': row})

@app.route('/api/kancelar/prilohy/<int:fid>/download')
def api_kan_priloha_download(fid):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM kancelar_prilohy WHERE id=?", (fid,))
    row = c.fetchone()
    conn.close()
    if not row or not os.path.exists(row['filepath']):
        return jsonify({'error': 'Soubor nenalezen'}), 404
    return send_file(row['filepath'], as_attachment=True, download_name=row['filename'])

@app.route('/api/kancelar/prilohy/<int:fid>', methods=['DELETE'])
def api_kan_priloha_delete(fid):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT filepath FROM kancelar_prilohy WHERE id=?", (fid,))
    row = c.fetchone()
    if row:
        try:
            if os.path.exists(row['filepath']):
                os.remove(row['filepath'])
        except Exception:
            pass
        c.execute("DELETE FROM kancelar_prilohy WHERE id=?", (fid,))
        conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ── KANCELÁŘ – STAV HOTOVO (číselník) ────────────────────────────────────

@app.route('/api/kancelar/stav-hotovo')
def api_kan_stav_hotovo():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM kancelar_stav_hotovo ORDER BY poradi, nazev")
    items = db_rows_to_list(c.fetchall())
    conn.close()
    return jsonify({'items': items})

@app.route('/api/kancelar/stav-hotovo', methods=['POST'])
def api_kan_stav_hotovo_create():
    d = request.get_json()
    if not d.get('nazev'):
        return jsonify({'error': 'Název je povinný'}), 400
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COALESCE(MAX(poradi),0)+1 FROM kancelar_stav_hotovo")
    poradi = c.fetchone()[0]
    c.execute("INSERT OR IGNORE INTO kancelar_stav_hotovo (nazev, poradi) VALUES (?,?)",
              (d['nazev'].strip(), poradi))
    conn.commit()
    c.execute("SELECT * FROM kancelar_stav_hotovo ORDER BY poradi, nazev")
    items = db_rows_to_list(c.fetchall())
    conn.close()
    return jsonify({'ok': True, 'items': items})

@app.route('/api/kancelar/stav-hotovo/<int:sid>', methods=['DELETE'])
def api_kan_stav_hotovo_delete(sid):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM kancelar_stav_hotovo WHERE id=?", (sid,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ── KANCELÁŘ – ZÁKAZNÍCI (číselník) ──────────────────────────────────────

@app.route('/api/kancelar/zakaznici')
def api_kan_zakaznici():
    conn = get_db()
    c = conn.cursor()
    q = request.args.get('q', '')
    if q:
        c.execute("SELECT * FROM kancelar_zakaznici WHERE nazev LIKE ? OR tel LIKE ? OR mail LIKE ? ORDER BY nazev LIMIT 20",
                  (f'%{q}%', f'%{q}%', f'%{q}%'))
    else:
        c.execute("SELECT * FROM kancelar_zakaznici ORDER BY nazev")
    items = db_rows_to_list(c.fetchall())
    conn.close()
    return jsonify({'zakaznici': items})

@app.route('/api/kancelar/zakaznici/<int:zid>', methods=['DELETE'])
def api_kan_zakaznik_delete(zid):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM kancelar_zakaznici WHERE id=?", (zid,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/kancelar/zakazky/<int:kid>/prevest', methods=['POST'])
def api_kancelar_prevest(kid):
    """Převede kancelářskou zakázku na výrobní zakázku."""
    d = request.get_json() or {}
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM kancelar_zakazky WHERE id=?", (kid,))
    kz = c.fetchone()
    if not kz:
        conn.close()
        return jsonify({'error': 'Zakázka nenalezena'}), 404
    c.execute("""
        INSERT INTO zakazky (nazev, zakaznik, hn_cislo, typ_casu_id, stav, pocet_ks, termin, poznamka_dilna)
        VALUES (?,?,?,?,?,?,?,?)
    """, (kz['nazev'], kz['zakaznik'] or '', d.get('hn_cislo',''),
          d.get('typ_casu_id') or None, 'Čeká',
          d.get('pocet_ks', 1), kz['termin'], kz['popis'] or ''))
    zak_id = c.lastrowid
    c.execute("UPDATE kancelar_zakazky SET vyrobni_zakazka_id=?, updated_at=? WHERE id=?",
              (zak_id, datetime.now().isoformat(), kid))
    # Přidej štítek Potvrzeno_zákazníkem pokud existuje
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'vyrobni_zakazka_id': zak_id})

# ─── KANCELÁŘ – ŠTÍTKY API ────────────────────────────────────────────────

@app.route('/api/kancelar/stitky')
def api_stitky():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM kancelar_stitky ORDER BY poradi, nazev")
    items = db_rows_to_list(c.fetchall())
    conn.close()
    return jsonify({'stitky': items})

@app.route('/api/kancelar/stitky', methods=['POST'])
def api_stitek_create():
    d = request.get_json()
    if not d.get('nazev'):
        return jsonify({'error': 'Název je povinný'}), 400
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COALESCE(MAX(poradi),0)+1 FROM kancelar_stitky")
    poradi = c.fetchone()[0]
    try:
        c.execute("INSERT INTO kancelar_stitky (nazev, barva, poradi) VALUES (?,?,?)",
                  (d['nazev'].strip(), d.get('barva','#e5e7eb'), poradi))
        sid = c.lastrowid
        conn.commit()
        c.execute("SELECT * FROM kancelar_stitky WHERE id=?", (sid,))
        row = dict(c.fetchone())
        conn.close()
        return jsonify({'ok': True, 'stitek': row})
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 409

@app.route('/api/kancelar/stitky/<int:sid>', methods=['POST'])
def api_stitek_update(sid):
    d = request.get_json()
    conn = get_db()
    c = conn.cursor()
    fields, vals = [], []
    for col in ('nazev', 'barva', 'poradi', 'aktivni'):
        if col in d:
            fields.append(f"{col}=?")
            vals.append(d[col])
    if not fields:
        conn.close()
        return jsonify({'error': 'Nic k aktualizaci'}), 400
    vals.append(sid)
    c.execute(f"UPDATE kancelar_stitky SET {', '.join(fields)} WHERE id=?", vals)
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/kancelar/stitky/<int:sid>', methods=['DELETE'])
def api_stitek_delete(sid):
    conn = get_db()
    c = conn.cursor()
    # Smaž jen pokud se štítek nikde nepoužívá, jinak jen deaktivuj
    c.execute("SELECT COUNT(*) FROM kancelar_zakazky_stitky WHERE stitek_id=?", (sid,))
    cnt = c.fetchone()[0]
    if cnt == 0:
        c.execute("DELETE FROM kancelar_stitky WHERE id=?", (sid,))
    else:
        c.execute("UPDATE kancelar_stitky SET aktivni=0 WHERE id=?", (sid,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ─── ARES – dohledání firmy podle IČO ─────────────────────────────────────

@app.route('/api/ares/<ico>')
def api_ares_lookup(ico):
    """Dohledá firmu v ARES (CZ business register) podle IČO."""
    import urllib.request, json as _json
    ico = ico.strip().zfill(8)
    url = f'https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty/{ico}'
    try:
        req = urllib.request.Request(url, headers={'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = _json.loads(resp.read().decode('utf-8'))
        sidlo = data.get('sidlo', {})
        ulice = sidlo.get('nazevUlice', '') or ''
        cd = sidlo.get('cisloDomovni', '')
        co = sidlo.get('cisloOrientacni', '')
        if cd and co:
            ulice = f"{ulice} {cd}/{co}".strip()
        elif cd:
            ulice = f"{ulice} {cd}".strip()
        psc = sidlo.get('psc', '') or ''
        obec = sidlo.get('nazevObce', '') or ''
        mesto = f"{psc} {obec}".strip()
        return jsonify({
            'nazev': data.get('obchodniJmeno', ''),
            'ulice': ulice,
            'mesto': mesto,
            'ic':    ico,
            'dic':   data.get('dic', '') or '',
        })
    except Exception as e:
        return jsonify({'error': f'ARES nedostupné: {e}'}), 502


# ─── FAKTURACE API ────────────────────────────────────────────────────────

MARZE = 1.047   # 4,7 % marže
DPH   = 1.21    # 21 % DPH
POCATECNI_CISLO = 11526049  # výchozí číselná řada

@app.route('/api/faktury/next-cislo')
def api_faktura_next_cislo():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT cislo FROM faktury ORDER BY id DESC LIMIT 1")
    row = c.fetchone()
    conn.close()
    if row:
        try:
            next_cislo = str(int(row['cislo']) + 1)
        except (ValueError, TypeError):
            next_cislo = str(POCATECNI_CISLO)
    else:
        next_cislo = str(POCATECNI_CISLO)
    return jsonify({'cislo': next_cislo})

@app.route('/api/faktury')
def api_faktury_list():
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT f.*, COUNT(fp.id) as pocet_polozek
        FROM faktury f
        LEFT JOIN faktury_polozky fp ON fp.faktura_id = f.id
        GROUP BY f.id
        ORDER BY f.id DESC
    """)
    faktury = db_rows_to_list(c.fetchall())
    conn.close()
    return jsonify({'faktury': faktury})

@app.route('/api/faktury/<int:fak_id>')
def api_faktura_detail(fak_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM faktury WHERE id=?", (fak_id,))
    fak = c.fetchone()
    if not fak:
        conn.close()
        return jsonify({'error': 'Faktura nenalezena'}), 404
    c.execute("SELECT * FROM faktury_polozky WHERE faktura_id=? ORDER BY id", (fak_id,))
    polozky = db_rows_to_list(c.fetchall())
    conn.close()
    return jsonify({'faktura': dict(fak), 'polozky': polozky})

@app.route('/api/faktury', methods=['POST'])
def api_faktura_create():
    data = request.get_json()
    polozky_in = data.get('polozky', [])
    if not polozky_in:
        return jsonify({'error': 'Faktura musí mít alespoň jednu položku'}), 400

    dnes = date.today().isoformat()
    splatnost = (date.today() + timedelta(days=14)).isoformat()

    conn = get_db()
    c = conn.cursor()

    # Zjisti příští číslo (atomicky)
    c.execute("SELECT cislo FROM faktury ORDER BY id DESC LIMIT 1")
    row = c.fetchone()
    if row:
        try:
            cislo = str(int(row['cislo']) + 1)
        except (ValueError, TypeError):
            cislo = str(POCATECNI_CISLO)
    else:
        cislo = data.get('cislo') or str(POCATECNI_CISLO)

    vystavil = data.get('vystavil', 'Kateřina Otradovcová')

    # Odběratel — defaultně AUDIO PARTNER s.r.o., ale přepísatelný
    AUDIO_PARTNER = {
        'nazev': 'AUDIO PARTNER s.r.o.',
        'ulice': 'Mezi vodami 2044/23',
        'mesto': '143 00 Praha 4',
        'ic':    '27114147',
        'dic':   'CZ27114147',
    }
    odb = data.get('odberatel') or {}
    odberatel = {
        'nazev': odb.get('nazev') or AUDIO_PARTNER['nazev'],
        'ulice': odb.get('ulice') or AUDIO_PARTNER['ulice'],
        'mesto': odb.get('mesto') or AUDIO_PARTNER['mesto'],
        'ic':    odb.get('ic')    or AUDIO_PARTNER['ic'],
        'dic':   odb.get('dic')   or AUDIO_PARTNER['dic'],
    }

    # Spočítej položky + celkové součty
    celkem_bez_dph = 0.0
    celkem_dph_total = 0.0
    celkem_s_dph = 0.0
    polozky_vypocet = []

    for p in polozky_in:
        zak_id = p.get('zakazka_id')
        ks = int(p.get('ks', 1))
        cena_dilu = float(p.get('cena_dilu', 0))
        cena_vyroby = float(p.get('cena_vyroby', 0))

        cena_za_mj = round((cena_dilu + cena_vyroby) * MARZE, 4)
        zaklad     = round(cena_za_mj * ks, 4)
        dph_cast   = round(zaklad * (DPH - 1), 4)
        celkem_pol = round(zaklad + dph_cast, 4)

        celkem_bez_dph  += zaklad
        celkem_dph_total += dph_cast
        celkem_s_dph     += celkem_pol

        polozky_vypocet.append({
            'zakazka_id':           zak_id,
            'hn_cislo':             p.get('hn_cislo', ''),
            'nazev':                p.get('nazev', ''),
            'ks':                   ks,
            'cena_dilu_snapshot':   cena_dilu,
            'cena_vyroby_snapshot': cena_vyroby,
            'cena_za_mj':           cena_za_mj,
            'sazba_dph':            21,
            'zaklad':               zaklad,
            'dph':                  dph_cast,
            'celkem_s_dph':         celkem_pol,
        })

    celkem_bez_dph  = round(celkem_bez_dph, 2)
    celkem_dph_total = round(celkem_dph_total, 2)
    celkem_s_dph     = round(celkem_s_dph, 2)

    try:
        c.execute("""
            INSERT INTO faktury
                (cislo, datum_vystaveni, datum_splatnosti, datum_plneni,
                 var_symbol, vystavil, stav,
                 celkem_bez_dph, celkem_dph, celkem_s_dph, poznamka,
                 odberatel_nazev, odberatel_ulice, odberatel_mesto, odberatel_ic, odberatel_dic)
            VALUES (?,?,?,?,?,?,'vydána',?,?,?,?,?,?,?,?,?)
        """, (cislo, dnes, splatnost, dnes,
              cislo, vystavil,
              celkem_bez_dph, celkem_dph_total, celkem_s_dph,
              data.get('poznamka', ''),
              odberatel['nazev'], odberatel['ulice'], odberatel['mesto'],
              odberatel['ic'], odberatel['dic']))
        fak_id = c.lastrowid

        for p in polozky_vypocet:
            c.execute("""
                INSERT INTO faktury_polozky
                    (faktura_id, zakazka_id, hn_cislo, nazev, ks,
                     cena_dilu_snapshot, cena_vyroby_snapshot,
                     cena_za_mj, sazba_dph, zaklad, dph, celkem_s_dph)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (fak_id, p['zakazka_id'], p['hn_cislo'], p['nazev'], p['ks'],
                  p['cena_dilu_snapshot'], p['cena_vyroby_snapshot'],
                  p['cena_za_mj'], p['sazba_dph'], p['zaklad'],
                  p['dph'], p['celkem_s_dph']))
            # Označ zakázku jako vyfakturovanou
            if p['zakazka_id']:
                c.execute("""
                    UPDATE zakazky SET
                        fakturovano=1,
                        faktura_cislo=?,
                        faktura_datum=?,
                        stav=CASE WHEN stav='Hotovo' THEN 'Expedováno' ELSE stav END
                    WHERE id=?
                """, (cislo, dnes, p['zakazka_id']))

        conn.commit()

        # Odpis materiálu ze skladu pro každou zakázku (pokud ještě neproběhl)
        for p in polozky_vypocet:
            if p['zakazka_id']:
                c.execute("SELECT sklad_odepsano, typ_casu_id FROM zakazky WHERE id=?", (p['zakazka_id'],))
                zr = c.fetchone()
                if zr and not zr['sklad_odepsano'] and zr['typ_casu_id']:
                    odepis_material_ze_skladu(conn, p['zakazka_id'])
    except sqlite3.IntegrityError as e:
        conn.close()
        return jsonify({'error': str(e)}), 409

    # Načti kompletní faktura pro odpověď
    c.execute("SELECT * FROM faktury WHERE id=?", (fak_id,))
    fak = dict(c.fetchone())
    c.execute("SELECT * FROM faktury_polozky WHERE faktura_id=? ORDER BY id", (fak_id,))
    polozky_out = db_rows_to_list(c.fetchall())
    conn.close()
    return jsonify({'ok': True, 'faktura': fak, 'polozky': polozky_out})

@app.route('/api/faktury/<int:fak_id>/pdf')
def api_faktura_pdf(fak_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM faktury WHERE id=?", (fak_id,))
    fak = c.fetchone()
    if not fak:
        conn.close()
        return jsonify({'error': 'Faktura nenalezena'}), 404
    c.execute("SELECT * FROM faktury_polozky WHERE faktura_id=? ORDER BY id", (fak_id,))
    polozky = db_rows_to_list(c.fetchall())
    conn.close()

    pdf_bytes = vygeneruj_pdf(dict(fak), polozky)
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype='application/pdf',
        as_attachment=False,
        download_name=f"Faktura_{fak['cislo']}.pdf"
    )

@app.route('/api/faktury/<int:fak_id>/stav', methods=['POST'])
def api_faktura_stav(fak_id):
    """Změna stavu faktury: zaplacena / storno"""
    data = request.get_json()
    stav = data.get('stav')
    if stav not in ('vydána', 'zaplacena', 'storno'):
        return jsonify({'error': 'Neplatný stav'}), 400
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE faktury SET stav=? WHERE id=?", (stav, fak_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'stav': stav})

@app.route('/api/odchylky/pocet-novych')
def api_odchylky_pocet():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM odchylky_karty WHERE stav='Nová'")
    pocet = c.fetchone()[0]
    conn.close()
    return jsonify({'pocet': pocet})

@app.route('/api/odchylky', methods=['GET'])
def api_odchylky_list():
    conn = get_db()
    c = conn.cursor()
    stav = request.args.get('stav', '')
    query = "SELECT * FROM odchylky_karty WHERE 1=1"
    params = []
    if stav:
        query += " AND stav=?"
        params.append(stav)
    query += " ORDER BY created_at DESC LIMIT 200"
    c.execute(query, params)
    items = db_rows_to_list(c.fetchall())
    conn.close()
    return jsonify({'items': items})

@app.route('/api/odchylky', methods=['POST'])
def api_odchylky_create():
    data = request.json
    text = (data.get('text') or '').strip()
    if not text:
        return jsonify({'error': 'Chybí popis odchylky'}), 400
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO odchylky_karty (zakazka_id, typ_casu_id, hn_cislo, text)
        VALUES (?,?,?,?)
    """, (data.get('zakazka_id'), data.get('typ_casu_id'), data.get('hn_cislo',''), text))
    conn.commit()
    new_id = c.lastrowid
    conn.close()
    return jsonify({'ok': True, 'id': new_id})

@app.route('/api/odchylky/<int:odch_id>', methods=['PUT'])
def api_odchylky_update(odch_id):
    data = request.json
    conn = get_db()
    c = conn.cursor()
    stav = data.get('stav', 'Vyřešeno')
    c.execute("""
        UPDATE odchylky_karty SET stav=?,
        vyreseno_at = CASE WHEN ? = 'Vyřešeno' THEN datetime('now') ELSE vyreseno_at END
        WHERE id=?
    """, (stav, stav, odch_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/zakazky/k-fakturaci')
def api_zakazky_k_fakturaci():
    """Všechny zakázky, které ještě nebyly vyfakturovány (bez filtru stavu)."""
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT z.id, z.hn_cislo, z.nazev, z.pocet_ks, z.stav,
               t.cena_dilu, t.cena_vyroby,
               round((t.cena_dilu + t.cena_vyroby) * 1.047, 2) as cena_za_mj,
               round((t.cena_dilu + t.cena_vyroby) * 1.047 * z.pocet_ks, 2) as zaklad,
               round((t.cena_dilu + t.cena_vyroby) * 1.047 * z.pocet_ks * 0.21, 2) as dph,
               round((t.cena_dilu + t.cena_vyroby) * 1.047 * z.pocet_ks * 1.21, 2) as celkem_s_dph
        FROM zakazky z
        LEFT JOIN typy_casu t ON t.id = z.typ_casu_id
        WHERE (z.fakturovano IS NULL OR z.fakturovano = 0)
          AND (z.stav IS NULL OR z.stav != 'Zrušeno')
        ORDER BY z.datum_zapsani
    """)
    zakazky = db_rows_to_list(c.fetchall())
    conn.close()
    return jsonify({'zakazky': zakazky})

# ─── MATERIÁL – SPOJOVACÍ MATERIÁL API ───────────────────────────────────────

@app.route('/api/materialy/spojeniky/hromadne', methods=['POST'])
def api_material_spojeniky_hromadne():
    """Hromadně nastaví závislost NYTY pro všechny materiály s nity > 0.
    Body: { nyty_kod: 'KOD_NYTU', prepsat: true/false }
    """
    data = request.json
    nyty_kod = (data.get('nyty_kod') or '').strip()
    prepsat = bool(data.get('prepsat', False))
    if not nyty_kod:
        return jsonify({'error': 'Chybí kód materiálu NYTY'}), 400
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT kod FROM materialy WHERE kod=?", (nyty_kod,))
    if not c.fetchone():
        conn.close()
        return jsonify({'error': f'Materiál s kódem „{nyty_kod}" nenalezen'}), 404
    c.execute("SELECT kod, nity FROM materialy WHERE nity > 0")
    rows = c.fetchall()
    pocet = 0
    for row in rows:
        if prepsat:
            c.execute("INSERT OR REPLACE INTO material_spojeniky (material_kod, spojovaci_kod, mnozstvi_na_kus) VALUES (?,?,?)",
                      (row['kod'], nyty_kod, row['nity']))
        else:
            c.execute("INSERT OR IGNORE INTO material_spojeniky (material_kod, spojovaci_kod, mnozstvi_na_kus) VALUES (?,?,?)",
                      (row['kod'], nyty_kod, row['nity']))
        pocet += 1
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'pocet': pocet})

@app.route('/api/materialy/<kod>/spojeniky')
def api_material_spojeniky_list(kod):
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT ms.id, ms.spojovaci_kod, ms.mnozstvi_na_kus, m.nazev, m.nc_bez_dph
        FROM material_spojeniky ms
        JOIN materialy m ON m.kod = ms.spojovaci_kod
        WHERE ms.material_kod = ?
        ORDER BY m.nazev
    """, (kod,))
    items = db_rows_to_list(c.fetchall())
    conn.close()
    return jsonify({'items': items})

@app.route('/api/materialy/<kod>/spojeniky', methods=['POST'])
def api_material_spojeniky_add(kod):
    data = request.json
    spoj_kod = (data.get('spojovaci_kod') or '').strip()
    mnozstvi = float(data.get('mnozstvi_na_kus', 0))
    if not spoj_kod or mnozstvi <= 0:
        return jsonify({'error': 'Chybí kód nebo množství'}), 400
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("INSERT OR REPLACE INTO material_spojeniky (material_kod, spojovaci_kod, mnozstvi_na_kus) VALUES (?,?,?)",
                  (kod, spoj_kod, mnozstvi))
        conn.commit()
        new_id = c.lastrowid
        conn.close()
        return jsonify({'ok': True, 'id': new_id})
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 400

@app.route('/api/materialy/spojeniky/<int:spoj_id>', methods=['DELETE'])
def api_material_spojeniky_delete(spoj_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM material_spojeniky WHERE id=?", (spoj_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ─── AKTUALIZACE / RESTART ────────────────────────────────────────────────

@app.route('/api/ping')
def api_ping():
    return jsonify({'ok': True})

@app.route('/api/aktualizace', methods=['POST'])
def api_aktualizace():
    """Spustí migrace přímo v procesu a restartuje server.
    Bez subprocesu — žádné problémy se zamčením DB.
    """
    log_lines = []
    ok = True

    try:
        # Zachyť výstup auto_migrate
        import io as _io
        from contextlib import redirect_stdout
        buf = _io.StringIO()
        with redirect_stdout(buf):
            auto_migrate()
            init_db()
        log_lines = [l for l in buf.getvalue().splitlines() if l.strip()]
        log_lines.append('[OK] Migrace dokoncena')
    except Exception as e:
        log_lines.append(f'[CHYBA] {e}')
        ok = False

    # Naplánuj restart za 1 sekundu (dá čas odeslat odpověď)
    def _restart():
        time.sleep(1)
        if os.name == 'nt':
            # Windows (lokální vývoj): spusť nový proces s novou konzolí
            subprocess.Popen(
                [sys.executable] + sys.argv,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
                close_fds=True,
            )
            os._exit(0)
        else:
            # Linux produkce: restartuj přes systemd (preferováno)
            # Vyžaduje: /etc/sudoers.d/flightcase s řádkem:
            #   www-data ALL=(ALL) NOPASSWD: /bin/systemctl restart flightcase
            result = subprocess.run(
                ['sudo', 'systemctl', 'restart', 'flightcase'],
                capture_output=True, timeout=10
            )
            if result.returncode != 0:
                # Fallback: přímý restart procesu (gunicorn to zvládne přes execv)
                os.execv(sys.executable, [sys.executable] + sys.argv)

    threading.Thread(target=_restart, daemon=False).start()

    return jsonify({'ok': ok, 'vystup': '\n'.join(log_lines)})

# ─── PARAMETRY VÝPOČTU ČASŮ ───────────────────────────────────────────────

@app.route('/api/cas-parametry', methods=['GET'])
def api_cas_parametry_get():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT sekce, klic, hodnota, popis FROM cas_parametry ORDER BY sekce, id")
    rows = db_rows_to_list(c.fetchall())
    conn.close()
    # Organize by section for easy frontend use
    by_sekce = {}
    for r in rows:
        by_sekce.setdefault(r['sekce'], {})[r['klic']] = {
            'hodnota': r['hodnota'], 'popis': r['popis']
        }
    return jsonify({'parametry': rows, 'by_sekce': by_sekce})

@app.route('/api/cas-parametry', methods=['PUT'])
def api_cas_parametry_put():
    """Aktualizace jednoho nebo více parametrů. Body: [ {sekce, klic, hodnota}, ... ]"""
    data = request.json  # list nebo dict
    if isinstance(data, dict):
        data = [data]
    conn = get_db()
    c = conn.cursor()
    updated = 0
    for item in data:
        sekce = item.get('sekce', '').strip()
        klic  = item.get('klic', '').strip()
        try:
            hodnota = float(item.get('hodnota', 0))
        except (ValueError, TypeError):
            continue
        if not sekce or not klic:
            continue
        c.execute(
            "UPDATE cas_parametry SET hodnota=? WHERE sekce=? AND klic=?",
            (hodnota, sekce, klic)
        )
        if c.rowcount == 0:
            c.execute(
                "INSERT OR IGNORE INTO cas_parametry (sekce, klic, hodnota) VALUES (?,?,?)",
                (sekce, klic, hodnota)
            )
        updated += 1
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'updated': updated})

@app.route('/api/cas-parametry/prepocitat-vse', methods=['POST'])
def api_cas_prepocitat_vse():
    """Přepočítá cas_narocnost (hodiny) pro VŠECHNY aktivní typy casů.
    Používá aktuálně uložené parametry z cas_parametry.
    Vrací { updated: N, errors: [] }.
    """
    import math
    conn = get_db()
    c = conn.cursor()

    # Načti parametry jednou
    c.execute("SELECT sekce, klic, hodnota FROM cas_parametry")
    par = {}
    for r in c.fetchall():
        par.setdefault(r['sekce'], {})[r['klic']] = r['hodnota']

    def p(sekce, klic, default=0):
        return par.get(sekce, {}).get(klic, default)

    ref_s = p('CNC',    'ref_sirka',   600)
    ref_h = p('CNC',    'ref_hloubka', 500)
    ref_v = p('Montaz', 'ref_vyska',   350)

    deska_fallback = p('CNC',    'deska_default_s', 180)
    hw_fallback    = p('Montaz', 'hw_default_s',     30)
    pena_fallback  = p('Peny',   'pena_default_s',  240)
    cas_na_nyt     = p('Montaz', 'cas_na_nyt',       15)

    cnc_setup     = p('CNC', 'setup',      900)
    cnc_data_prep = p('CNC', 'data_prep',  600)
    cnc_fix_pm    = p('CNC', 'fix_per_mat', 60)
    mont_setup    = p('Montaz', 'setup',    600)
    mont_cleanup  = p('Montaz', 'cleanup',  300)
    mont_kontrola = p('Montaz', 'kontrola', 180)
    peny_pistole  = p('Peny', 'pistole',   180)
    peny_cleanup  = p('Peny', 'cleanup',   120)
    peny_fix      = p('Peny', 'fix_session', 60)

    def norm_typ(t):
        if not t: return ''
        t = t.upper()
        for f, r in [('Á','A'),('Č','C'),('Ď','D'),('É','E'),('Ě','E'),
                     ('Í','I'),('Ň','N'),('Ó','O'),('Ř','R'),('Š','S'),
                     ('Ť','T'),('Ú','U'),('Ů','U'),('Ý','Y'),('Ž','Z')]:
            t = t.replace(f, r)
        return t

    def is_deska(nt): return 'DESKA' in nt or 'PREKLIZK' in nt or 'PLAYWOOD' in nt
    def is_hw(nt):    return (nt.startswith('HW') or 'PROFIL' in nt or 'KOULE' in nt or
                              'MADLO' in nt or 'KOLECKO' in nt or 'PANTL' in nt or
                              'ZAPAD' in nt or 'ZAMEK' in nt or 'LOGO' in nt)
    def is_pena(nt):  return 'PEN' in nt or 'FOAM' in nt or 'BALDACHIN' in nt or 'BALDACYN' in nt

    # Načti všechny aktivní typy
    c.execute("SELECT id, vnitrni_sirka, vnitrni_vyska, vnitrni_hloubka FROM typy_casu WHERE aktivni=1")
    typy = c.fetchall()

    updated = 0
    errors  = []

    for typ in typy:
        try:
            sirka   = typ['vnitrni_sirka']   or 0
            vyska   = typ['vnitrni_vyska']   or 0
            hloubka = typ['vnitrni_hloubka'] or 0

            if sirka > 0 and hloubka > 0 and ref_s > 0 and ref_h > 0:
                size_factor = math.sqrt((sirka * hloubka) / (ref_s * ref_h))
            else:
                size_factor = 1.0
            size_factor = max(0.3, min(size_factor, 6.0))

            if sirka > 0 and vyska > 0 and hloubka > 0 and ref_s > 0 and ref_v > 0 and ref_h > 0:
                handling_factor = (sirka * vyska * hloubka / (ref_s * ref_v * ref_h)) ** (1/3)
            else:
                handling_factor = size_factor
            handling_factor = max(0.3, min(handling_factor, 6.0))

            c.execute("""
                SELECT k.material_kod, k.mnozstvi, m.typ, m.cas_s,
                       COALESCE(m.nity, 0) as nity
                FROM kusovniky k
                JOIN materialy m ON m.kod = k.material_kod
                WHERE k.typ_casu_id = ?
            """, (typ['id'],))
            bom = c.fetchall()

            # CNC
            cnc_s = cnc_setup + cnc_data_prep
            n_desky = 0
            for pol in bom:
                nt = norm_typ(pol['typ'] or '')
                if not is_deska(nt): continue
                cas_s = (pol['cas_s'] or 0) or deska_fallback
                cnc_s += cas_s * pol['mnozstvi'] * size_factor
                n_desky += 1
            cnc_s += cnc_fix_pm * n_desky

            # Montáž
            total_nits  = sum(pol['mnozstvi'] * (pol['nity'] or 0) for pol in bom)
            fixed_scaled = (mont_setup + mont_cleanup + mont_kontrola) * handling_factor
            hw_s = 0.0
            for pol in bom:
                nt = norm_typ(pol['typ'] or '')
                if not is_hw(nt): continue
                cas_s = (pol['cas_s'] or 0) or hw_fallback
                if cas_s == 0: continue
                hw_s += cas_s * pol['mnozstvi'] * handling_factor
            mont_s = fixed_scaled + hw_s + total_nits * cas_na_nyt

            # Pěny
            has_peny = any(is_pena(norm_typ(pol['typ'] or '')) for pol in bom)
            peny_s = 0.0
            if has_peny:
                peny_s += peny_pistole + peny_cleanup + peny_fix
                for pol in bom:
                    nt = norm_typ(pol['typ'] or '')
                    if not is_pena(nt): continue
                    cas_s = (pol['cas_s'] or 0) or pena_fallback
                    peny_s += cas_s * pol['mnozstvi'] * size_factor

            celkem_s      = cnc_s + mont_s + peny_s
            cas_narocnost = round(celkem_s / 3600, 2)  # sekundy → hodiny

            c.execute(
                "UPDATE typy_casu SET cas_narocnost=?, updated_at=datetime('now') WHERE id=?",
                (cas_narocnost, typ['id'])
            )
            updated += 1
        except Exception as e:
            errors.append({'typ_id': typ['id'], 'error': str(e)})

    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'updated': updated, 'errors': errors})

@app.route('/api/typy-casu/<int:typ_id>/cas-vypocet', methods=['GET'])
def api_cas_vypocet(typ_id):
    """Výpočet odhadovaného výrobního času pro daný BOM.

    Logika:
      CNC    = setup + data_prep + fix×(počet typů desek) + Σ(cas_s_eff × qty × size_factor)
               size_factor = √(šířka×hloubka / ref_šířka×ref_hloubka)  — plocha, 2D

      Montáž = (setup + cleanup + kontrola) × handling_factor
               + Σ(cas_s_eff × qty × handling_factor)
               + total_nýtů × cas_na_nýt
               handling_factor = ∛(š×v×h / ref_š×ref_v×ref_h)  — objem, 3D

      Pěny   = pistole + fix + cleanup + Σ(cas_s_eff × qty × size_factor)
               Pěny jsou plošná práce → stejný size_factor jako CNC

    Položky s cas_s=0 používají záložní hodnotu z cas_parametry (pouzit_default=True).
    """
    import math
    conn = get_db()
    c = conn.cursor()

    # Načti typ case + rozměry
    c.execute("SELECT * FROM typy_casu WHERE id=?", (typ_id,))
    typ = c.fetchone()
    if not typ:
        conn.close()
        return jsonify({'error': 'Typ nenalezen'}), 404

    # Načti parametry
    c.execute("SELECT sekce, klic, hodnota FROM cas_parametry")
    par = {}
    for r in c.fetchall():
        par.setdefault(r['sekce'], {})[r['klic']] = r['hodnota']

    def p(sekce, klic, default=0):
        return par.get(sekce, {}).get(klic, default)

    # ── Rozměry case ──────────────────────────────────────────────────────────
    sirka   = typ['vnitrni_sirka']   or 0
    vyska   = typ['vnitrni_vyska']   or 0
    hloubka = typ['vnitrni_hloubka'] or 0
    ref_s   = p('CNC',    'ref_sirka',   600)
    ref_h   = p('CNC',    'ref_hloubka', 500)
    ref_v   = p('Montaz', 'ref_vyska',   350)

    # SIZE-FACTOR: √(plocha / ref_plocha) – pro CNC a Pěny (plošná práce)
    if sirka > 0 and hloubka > 0 and ref_s > 0 and ref_h > 0:
        size_factor = math.sqrt((sirka * hloubka) / (ref_s * ref_h))
    else:
        size_factor = 1.0
    size_factor = max(0.3, min(size_factor, 6.0))

    # HANDLING-FACTOR: ∛(objem / ref_objem) – pro Montáž (3D manipulace)
    # Pokud chybí výška, použij size_factor jako proxy
    if sirka > 0 and vyska > 0 and hloubka > 0 and ref_s > 0 and ref_v > 0 and ref_h > 0:
        handling_factor = (sirka * vyska * hloubka / (ref_s * ref_v * ref_h)) ** (1/3)
    else:
        handling_factor = size_factor  # fallback na 2D factor
    handling_factor = max(0.3, min(handling_factor, 6.0))

    # Načti BOM materiály s typem, cas_s a nity
    c.execute("""
        SELECT k.material_kod, k.mnozstvi, m.nazev, m.typ, m.cas_s,
               COALESCE(m.nity, 0) as nity
        FROM kusovniky k
        JOIN materialy m ON m.kod = k.material_kod
        WHERE k.typ_casu_id = ?
    """, (typ_id,))
    bom = db_rows_to_list(c.fetchall())
    conn.close()

    # Pomocná normalizace typu
    def norm_typ(t):
        if not t:
            return ''
        t = t.upper()
        for f, r in [('Á','A'),('Č','C'),('Ď','D'),('É','E'),('Ě','E'),
                     ('Í','I'),('Ň','N'),('Ó','O'),('Ř','R'),('Š','S'),
                     ('Ť','T'),('Ú','U'),('Ů','U'),('Ý','Y'),('Ž','Z')]:
            t = t.replace(f, r)
        return t

    def is_deska(nt):
        return 'DESKA' in nt or 'PREKLIZK' in nt or 'PLAYWOOD' in nt

    def is_hw(nt):
        return (nt.startswith('HW') or 'PROFIL' in nt or 'KOULE' in nt or
                'MADLO' in nt or 'KOLECKO' in nt or 'PANTL' in nt or
                'ZAPAD' in nt or 'ZAMEK' in nt or 'LOGO' in nt)

    def is_pena(nt):
        return 'PEN' in nt or 'FOAM' in nt or 'BALDACHIN' in nt or 'BALDACYN' in nt

    # Záložní hodnoty (fallback) pro případ cas_s = 0
    deska_fallback = p('CNC',    'deska_default_s', 180)
    hw_fallback    = p('Montaz', 'hw_default_s',     30)
    pena_fallback  = p('Peny',   'pena_default_s',  240)

    # ── CNC ──────────────────────────────────────────────────────────────────
    # Každý typ desky: pevný overhead (příprava materiálu) + čas řezání × qty × size_factor
    cnc_materialy  = []
    cnc_mat_pocet  = 0   # počet unikátních typů desek
    for pol in bom:
        nt = norm_typ(pol.get('typ') or '')
        if not is_deska(nt):
            continue
        cas_s_raw      = pol['cas_s'] or 0
        pouzit_default = cas_s_raw == 0
        cas_s_eff      = cas_s_raw if not pouzit_default else deska_fallback
        cas            = cas_s_eff * pol['mnozstvi'] * size_factor
        cnc_materialy.append({
            'material_kod':  pol['material_kod'],
            'nazev':         pol['nazev'],
            'typ':           pol['typ'],
            'mnozstvi':      pol['mnozstvi'],
            'cas_s_j':       cas_s_eff,
            'cas_s_orig':    cas_s_raw,
            'pouzit_default': pouzit_default,
            'size_factor':   round(size_factor, 3),
            'cas_celkem_s':  round(cas, 1),
        })
        cnc_mat_pocet += 1

    cnc_setup         = p('CNC', 'setup', 900)
    cnc_data_prep     = p('CNC', 'data_prep', 600)
    cnc_fix           = p('CNC', 'fix_per_mat', 60) * cnc_mat_pocet
    cnc_mat_cas       = sum(m['cas_celkem_s'] for m in cnc_materialy)
    cnc_celkem        = cnc_setup + cnc_data_prep + cnc_fix + cnc_mat_cas
    cnc_defaults_cnt  = sum(1 for m in cnc_materialy if m['pouzit_default'])

    # ── MONTÁŽ ────────────────────────────────────────────────────────────────
    # Fixní časy ŠKÁLOVANÉ handling_factorem: velký case se hůř otáčí, pokládá, pracuje s ním
    # HW časy (cas_s × qty) TAKÉ škálované: stejný počet rohů, ale na velkém casu trvá práce déle
    # Nýty: celkový počet nýtů × čas/nýt (neškálováno – každý nýt je stejná operace)
    mont_materialy  = []
    total_nits      = 0.0   # Σ(mnozstvi × nity)
    for pol in bom:
        nt = norm_typ(pol.get('typ') or '')
        # Nýty sbíráme ze všech položek s nity > 0 (ne jen z HW)
        total_nits += pol['mnozstvi'] * (pol.get('nity') or 0)
        if not is_hw(nt):
            continue
        cas_s_raw      = pol['cas_s'] or 0
        pouzit_default = cas_s_raw == 0
        cas_s_eff      = cas_s_raw if not pouzit_default else hw_fallback
        if cas_s_eff == 0:
            continue
        # HW čas škálovaný handling_factorem (velký case = těžší manipulace)
        cas = cas_s_eff * pol['mnozstvi'] * handling_factor
        mont_materialy.append({
            'material_kod':   pol['material_kod'],
            'nazev':          pol['nazev'],
            'typ':            pol['typ'],
            'mnozstvi':       pol['mnozstvi'],
            'cas_s_j':        cas_s_eff,
            'cas_s_orig':     cas_s_raw,
            'pouzit_default': pouzit_default,
            'handling_factor': round(handling_factor, 3),
            'cas_celkem_s':   round(cas, 1),
        })

    mont_setup         = p('Montaz', 'setup',    600)
    mont_cleanup       = p('Montaz', 'cleanup',  300)
    mont_kontrola      = p('Montaz', 'kontrola', 180)
    cas_na_nyt         = p('Montaz', 'cas_na_nyt', 15)
    total_nits         = round(total_nits)
    nit_cas            = round(total_nits * cas_na_nyt)
    # Fixní overhead rovněž škálovaný: velký case = pomalejší příprava/úklid pracoviště
    mont_fixed_scaled  = round((mont_setup + mont_cleanup + mont_kontrola) * handling_factor)
    mont_hw_cas        = sum(m['cas_celkem_s'] for m in mont_materialy)
    mont_celkem        = mont_fixed_scaled + mont_hw_cas + nit_cas
    mont_defaults_cnt  = sum(1 for m in mont_materialy if m['pouzit_default'])

    # ── PĚNY ──────────────────────────────────────────────────────────────────
    # Pěny jsou plošná práce (fitting, řezání, lepení) → škálujeme size_factorem stejně jako CNC
    peny_materialy  = []
    has_peny        = False
    for pol in bom:
        nt = norm_typ(pol.get('typ') or '')
        if not is_pena(nt):
            continue
        has_peny       = True
        cas_s_raw      = pol['cas_s'] or 0
        pouzit_default = cas_s_raw == 0
        cas_s_eff      = cas_s_raw if not pouzit_default else pena_fallback
        cas            = cas_s_eff * pol['mnozstvi'] * size_factor   # ← size_factor!
        peny_materialy.append({
            'material_kod':   pol['material_kod'],
            'nazev':          pol['nazev'],
            'typ':            pol['typ'],
            'mnozstvi':       pol['mnozstvi'],
            'cas_s_j':        cas_s_eff,
            'cas_s_orig':     cas_s_raw,
            'pouzit_default': pouzit_default,
            'size_factor':    round(size_factor, 3),
            'cas_celkem_s':   round(cas, 1),
        })

    peny_pistole       = p('Peny', 'pistole',     180) if has_peny else 0
    peny_cleanup       = p('Peny', 'cleanup',     120) if has_peny else 0
    peny_fix           = p('Peny', 'fix_session',  60) if has_peny else 0
    peny_mat_cas       = sum(m['cas_celkem_s'] for m in peny_materialy)
    peny_celkem        = peny_pistole + peny_cleanup + peny_fix + peny_mat_cas
    peny_defaults_cnt  = sum(1 for m in peny_materialy if m['pouzit_default'])

    celkem_s       = cnc_celkem + mont_celkem + peny_celkem
    total_defaults = cnc_defaults_cnt + mont_defaults_cnt + peny_defaults_cnt

    def fmt_hm(s):
        h = int(s // 3600)
        m = int((s % 3600) // 60)
        return f"{h}h {m:02d}min" if h else f"{m}min"

    # Cenové parametry pro výpočet správné MC
    c2 = conn.cursor() if not conn else conn.cursor()
    conn2 = get_db()
    c2 = conn2.cursor()
    c2.execute("SELECT klic, hodnota FROM cas_parametry WHERE sekce='Ceny'")
    ceny_par = {r['klic']: r['hodnota'] for r in c2.fetchall()}
    conn2.close()

    return jsonify({
        'typ_id': typ_id,
        'hn_cislo': typ['hn_cislo'],
        'size_factor':     round(size_factor, 3),
        'handling_factor': round(handling_factor, 3),
        'ma_defaults':     total_defaults > 0,
        'defaults_cnt':    total_defaults,
        'ceny_par':        ceny_par,
        'cnc': {
            'celkem_s':      round(cnc_celkem),
            'celkem_fmt':    fmt_hm(cnc_celkem),
            'setup_s':       cnc_setup,
            'data_prep_s':   cnc_data_prep,
            'fix_mat_s':     cnc_fix,
            'mat_cas_s':     round(cnc_mat_cas, 1),
            'defaults_cnt':  cnc_defaults_cnt,
            'deska_fallback': deska_fallback,
            'n_typy_desek':  cnc_mat_pocet,
            'polozky':       cnc_materialy,
        },
        'montaz': {
            'celkem_s':         round(mont_celkem),
            'celkem_fmt':       fmt_hm(mont_celkem),
            'fixed_scaled_s':   mont_fixed_scaled,
            'hw_cas_s':         round(mont_hw_cas, 1),
            'handling_factor':  round(handling_factor, 3),
            'total_nits':       total_nits,
            'nit_cas_s':        nit_cas,
            'cas_na_nyt':       cas_na_nyt,
            'defaults_cnt':     mont_defaults_cnt,
            'hw_fallback':      hw_fallback,
            'polozky':          mont_materialy,
        },
        'peny': {
            'celkem_s':      round(peny_celkem),
            'celkem_fmt':    fmt_hm(peny_celkem),
            'pistole_s':     peny_pistole,
            'cleanup_s':     peny_cleanup,
            'fix_s':         peny_fix,
            'mat_cas_s':     round(peny_mat_cas, 1),
            'defaults_cnt':  peny_defaults_cnt,
            'pena_fallback': pena_fallback,
            'polozky':       peny_materialy,
        },
        'celkem_s':    round(celkem_s),
        'celkem_fmt':  fmt_hm(celkem_s),
    })

# ─── NÁKUPY – PRŮMĚRNÁ SPOTŘEBA & NÁVRH OBJEDNÁVKY ───────────────────────

@app.route('/api/nakupy/spotreba')
def api_nakupy_spotreba():
    """Průměrná denní spotřeba za posledních N dní pro všechny materiály.
    Param: ?dni=90 (výchozí 90)
    """
    dni = int(request.args.get('dni', 90))
    conn = get_db()
    c = conn.cursor()
    c.execute(f"""
        SELECT material_kod,
               COALESCE(SUM(mnozstvi), 0)        AS celkem_vydano,
               COALESCE(SUM(mnozstvi), 0) / ?    AS avg_den
        FROM pohyby_skladu
        WHERE typ IN ('vydej','spotřeba')
          AND datum >= date('now', '-{dni} days')
        GROUP BY material_kod
    """, (float(dni),))
    rows = {r[0]: {'celkem_vydano': r[1], 'avg_den': r[2]} for r in c.fetchall()}
    conn.close()
    return jsonify(rows)


@app.route('/api/nakupy/navrh')
def api_nakupy_navrh():
    """Návrh nákupu: materiály kde zásoby nevydrží X dní.
    Params: ?dni=30&dodavatel_id=5&okno=90
      dni        – horizont zásoby v dnech (default 30)
      dodavatel_id – filtr dle dodavatele (optional)
      okno       – počet dní pro výpočet průměrné spotřeby (default 90)
    """
    dni    = int(request.args.get('dni', 30))
    dod_id = request.args.get('dodavatel_id')
    okno   = int(request.args.get('okno', 90))

    conn = get_db()
    c = conn.cursor()

    # Průměrná denní spotřeba za posledních `okno` dní
    c.execute(f"""
        SELECT material_kod,
               SUM(mnozstvi)           AS celkem_vydano,
               SUM(mnozstvi) / {okno}.0 AS avg_den
        FROM pohyby_skladu
        WHERE typ IN ('vydej','spotřeba')
          AND datum >= date('now', '-{okno} days')
        GROUP BY material_kod
    """)
    spotreba = {r[0]: {'celkem': r[1], 'avg_den': r[2]} for r in c.fetchall()}

    # Aktuální stav skladu + dodavatel info
    dod_filter = "AND m.dodavatel_id = ?" if dod_id else ""
    params = [dod_id] if dod_id else []
    c.execute(f"""
        SELECT m.kod, m.nazev, m.jednotka, m.typ,
               m.dodavatel_id,
               d.nazev AS dodavatel_nazev,
               COALESCE(s.skutecny_stav, s.naskladneno - s.pouzito, 0) AS stav,
               COALESCE(s.min_skladem, 0) AS min_skladem
        FROM materialy m
        LEFT JOIN sklad s ON s.material_kod = m.kod
        LEFT JOIN dodavatele d ON d.id = m.dodavatel_id
        WHERE s.material_kod IS NOT NULL
          {dod_filter}
        ORDER BY m.kod
    """, params)
    materialy = c.fetchall()
    conn.close()

    result = []
    for row in materialy:
        kod, nazev, jednotka, typ, dod_mat_id, dod_nazev, stav, min_sk = row
        sp = spotreba.get(kod, {})
        avg_den = sp.get('avg_den', 0)
        celkem_vydano = sp.get('celkem', 0)

        # Dní zásoby
        if avg_den > 0:
            dnu_zasoby = stav / avg_den
        else:
            dnu_zasoby = None   # Žádná spotřeba → nelze odhadnout

        # Zahrnout do návrhu jen pokud:
        # a) máme data o spotřebě a zásoby nevydrží na cílový horizont
        # b) nebo stav < min_skladem (i bez spotřeby)
        pod_min = min_sk > 0 and stav < min_sk
        pod_horizontem = dnu_zasoby is not None and dnu_zasoby < dni

        if not pod_min and not pod_horizontem:
            continue

        # Navrhované množství k objednání
        if avg_den > 0:
            cilovy_stav = avg_den * dni
            navrh_qty = max(0, round(cilovy_stav - stav, 2))
        else:
            navrh_qty = max(0, round(min_sk * 2 - stav, 2)) if min_sk > 0 else 0

        result.append({
            'kod': kod,
            'nazev': nazev,
            'jednotka': jednotka or 'ks',
            'typ': typ,
            'dodavatel_id': dod_mat_id,
            'dodavatel': dod_nazev or '–',
            'stav': round(stav, 3),
            'min_skladem': min_sk,
            'avg_den': round(avg_den, 4),
            'celkem_vydano_okno': round(celkem_vydano, 2),
            'dnu_zasoby': round(dnu_zasoby, 1) if dnu_zasoby is not None else None,
            'navrh_qty': navrh_qty,
            'pod_min': pod_min,
            'bez_spotreby': avg_den == 0,
        })

    # Seřaď: nejkritičtější první (nejméně dní zásoby, None = bez spotřeby na konec)
    result.sort(key=lambda r: (
        r['dnu_zasoby'] if r['dnu_zasoby'] is not None else 9999,
        r['kod']
    ))
    return jsonify({'items': result, 'dni': dni, 'okno': okno,
                    'celkem': len(result)})


# ─── FIFO & IMPORT ────────────────────────────────────────────────────────

@app.route('/api/sklad/fifo/<kod>')
def api_sklad_fifo(kod):
    """Vrátí FIFO dávky pro daný materiál (od nejstarší)."""
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT id, datum_prijmu, mnozstvi_orig, mnozstvi_zbyla, cena_jednotka,
               dodavatel, faktura, je_inventura, poznamka, zruseno
        FROM fifo_davky
        WHERE material_kod=? AND zruseno=0
        ORDER BY datum_prijmu ASC, id ASC
    """, (kod,))
    rows = db_rows_to_list(c.fetchall())
    celkem_zbyla = sum(r['mnozstvi_zbyla'] for r in rows)
    total_val = sum(r['mnozstvi_zbyla'] * r['cena_jednotka'] for r in rows)
    avg_cena = round(total_val / celkem_zbyla, 4) if celkem_zbyla > 0 else 0
    conn.close()
    return jsonify({'kod': kod, 'davky': rows, 'celkem_zbyla': round(celkem_zbyla, 4), 'prumerna_cena': avg_cena})


@app.route('/api/sklad/alarmy')
def api_sklad_alarmy():
    """Materiály kde stav < min_skladem."""
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT m.kod, m.nazev, m.jednotka, m.typ,
               COALESCE(s.skutecny_stav, s.naskladneno - s.pouzito, 0) as stav,
               COALESCE(s.min_skladem, 0) as min_skladem,
               COALESCE(s.naskladneno, 0) as naskladneno,
               (SELECT COALESCE(SUM(mnozstvi_zbyla),0) FROM fifo_davky
                WHERE material_kod=m.kod AND zruseno=0) as fifo_zbyla
        FROM materialy m
        JOIN sklad s ON s.material_kod = m.kod
        WHERE s.min_skladem > 0
          AND COALESCE(s.skutecny_stav, s.naskladneno - s.pouzito, 0) < s.min_skladem
        ORDER BY (COALESCE(s.skutecny_stav, s.naskladneno - s.pouzito, 0) / s.min_skladem) ASC
    """)
    rows = db_rows_to_list(c.fetchall())
    conn.close()
    return jsonify(rows)


@app.route('/api/statistiky/sklad')
def api_statistiky_sklad():
    """Statistiky skladu: celková hodnota, rozdělení dle typů, top položky, pohyby."""
    conn = get_db()
    c = conn.cursor()

    # Výpočtový stav = naskladneno - pouzito (stejná logika jako JS v modulu Sklad)
    _stav = "(COALESCE(s.naskladneno,0) - COALESCE(s.pouzito,0))"
    _kde  = """
        FROM materialy m
        LEFT JOIN sklad s ON s.material_kod = m.kod
        WHERE (
            m.zobrazovat = 1
            OR EXISTS (SELECT 1 FROM kusovniky k WHERE k.material_kod = m.kod)
            OR EXISTS (SELECT 1 FROM material_spojeniky ms WHERE ms.spojovaci_kod = m.kod)
        )
    """

    # ── Souhrn ───────────────────────────────────────────────────────────────
    c.execute(f"""
        SELECT
            COUNT(*)                                                            AS pocet_polozek,
            COALESCE(SUM({_stav} * m.nc_bez_dph), 0)                          AS hodnota_celkem,
            COUNT(CASE WHEN {_stav} > 0 THEN 1 END)                           AS polozek_na_sklade,
            COUNT(CASE WHEN COALESCE(s.min_skladem,0) > 0
                            AND {_stav} < s.min_skladem THEN 1 END)            AS pod_minimem
        {_kde}
    """)
    souhrn = dict(c.fetchone())

    # ── Rozdělení dle typů ───────────────────────────────────────────────────
    c.execute(f"""
        SELECT
            COALESCE(m.typ, 'Nezařazeno')            AS typ,
            COUNT(*)                                  AS pocet,
            COALESCE(SUM({_stav} * m.nc_bez_dph), 0) AS hodnota
        {_kde}
        GROUP BY COALESCE(m.typ, 'Nezařazeno')
        ORDER BY hodnota DESC
    """)
    typy = db_rows_to_list(c.fetchall())

    # ── Top 10 nejhodnotnějších položek ──────────────────────────────────────
    c.execute(f"""
        SELECT
            m.kod, m.nazev, m.typ,
            {_stav}                          AS stav,
            m.nc_bez_dph,
            {_stav} * m.nc_bez_dph           AS hodnota
        {_kde}
        AND {_stav} > 0
        ORDER BY hodnota DESC
        LIMIT 10
    """)
    top10 = db_rows_to_list(c.fetchall())

    # ── Pohyby skladu – posledních 30 dní ────────────────────────────────────
    c.execute("""
        SELECT
            ps.typ,
            COUNT(*)                                                    AS pocet_pohybu,
            COALESCE(SUM(ps.mnozstvi * COALESCE(m.nc_bez_dph,0)), 0)  AS hodnota
        FROM pohyby_skladu ps
        JOIN materialy m ON m.kod = ps.material_kod
        WHERE ps.datum >= date('now', '-30 days')
          AND ps.typ IN ('prijem','vydej')
        GROUP BY ps.typ
    """)
    pohyby30 = {r['typ']: dict(r) for r in c.fetchall()}

    # ── Mrtvý sklad – kladný stav, bez pohybu 90+ dní ────────────────────────
    c.execute(f"""
        SELECT
            COUNT(*)                                          AS pocet,
            COALESCE(SUM({_stav} * m.nc_bez_dph), 0)        AS hodnota
        {_kde}
        AND {_stav} > 0
        AND NOT EXISTS (
            SELECT 1 FROM pohyby_skladu ps
            WHERE ps.material_kod = m.kod
              AND ps.datum >= date('now', '-90 days')
        )
    """)
    mrtvy = dict(c.fetchone())

    conn.close()
    return jsonify({
        'souhrn': souhrn,
        'typy':   typy,
        'top10':  top10,
        'pohyby30':    pohyby30,
        'mrtvy_sklad': mrtvy,
    })


@app.route('/api/sklad/import-bulk', methods=['POST'])
def api_sklad_import_bulk():
    """Hromadný import naskladnění – vytváří příjemky, FIFO dávky i pohyby skladu.
    Body: { rows: [{datum,sku,mnozstvi,cena_j,dodavatel,faktura,inventura}],
            clear_existing: bool }
    """
    data = request.json
    rows = data.get('rows', [])
    clear_existing = data.get('clear_existing', False)

    conn = get_db()
    c = conn.cursor()
    imported = 0
    skipped = []
    errors = []
    prijemky_count = 0

    if clear_existing:
        c.execute("DELETE FROM prijemky_polozky")
        c.execute("DELETE FROM prijemky WHERE poznamka='import_nakupy_2025'")
        c.execute("DELETE FROM fifo_davky")
        c.execute("UPDATE sklad SET naskladneno=0, pouzito=0, skutecny_stav=0 WHERE 1=1")
        c.execute("DELETE FROM pohyby_skladu WHERE typ IN ('prijem','inventura','import')")

    # Načteme existující materiály
    c.execute("SELECT kod FROM materialy")
    known_kody = {r[0] for r in c.fetchall()}

    # Pomocná funkce: najdi nebo vytvoř dodavatele
    dodavatel_cache = {}
    def get_or_create_dodavatel(nazev):
        if not nazev:
            return None
        if nazev in dodavatel_cache:
            return dodavatel_cache[nazev]
        c.execute("SELECT id FROM dodavatele WHERE UPPER(nazev)=UPPER(?)", (nazev,))
        row = c.fetchone()
        if row:
            dodavatel_cache[nazev] = row[0]
            return row[0]
        c.execute("INSERT INTO dodavatele (nazev, aktivni) VALUES (?, 1)", (nazev,))
        did = c.lastrowid
        dodavatel_cache[nazev] = did
        return did

    # ── Filtruj a validuj řádky ──────────────────────────────────────────────
    valid_rows = []
    for row in rows:
        sku       = (row.get('sku') or '').strip()
        datum     = row.get('datum', '')
        mnozstvi  = float(row.get('mnozstvi', 0) or 0)
        cena_j    = float(row.get('cena_j', 0) or 0)
        dodavatel = (row.get('dodavatel') or '').strip()
        faktura   = (row.get('faktura') or '').strip()
        je_inv    = 1 if row.get('inventura') else 0

        if not sku or not datum:
            continue
        if sku not in known_kody:
            skipped.append(sku)
            continue
        if mnozstvi <= 0 and not je_inv:
            continue
        valid_rows.append({
            'sku': sku, 'datum': datum, 'mnozstvi': mnozstvi,
            'cena_j': cena_j, 'dodavatel': dodavatel,
            'faktura': faktura, 'je_inv': je_inv
        })

    # ── Seskup do příjemek podle (faktura + datum + dodavatel) ───────────────
    # Klíč: (faktura, datum, dodavatel) → seznam řádků
    prijemka_groups = {}
    for row in valid_rows:
        key = (row['faktura'], row['datum'], row['dodavatel'])
        prijemka_groups.setdefault(key, []).append(row)

    # ── Zpracuj každou příjemku ───────────────────────────────────────────────
    for (faktura, datum, dodavatel_nazev), group in sorted(prijemka_groups.items()):
        je_inv = group[0]['je_inv']  # celá skupina má stejný typ
        try:
            dod_id = get_or_create_dodavatel(dodavatel_nazev)
            stav_prijemky = 'zaúčtováno'
            typ_pohybu = 'inventura' if je_inv else 'prijem'

            # Vytvoř příjemku
            c.execute("""
                INSERT INTO prijemky (cislo, dodavatel_id, datum, stav, poznamka)
                VALUES (?, ?, ?, ?, 'import_nakupy_2025')
            """, (faktura, dod_id, datum, stav_prijemky))
            prijemka_id = c.lastrowid
            prijemky_count += 1

            # Zpracuj každou položku skupiny
            for row in group:
                sku      = row['sku']
                mnozstvi = row['mnozstvi']
                cena_j   = row['cena_j']

                # Položka příjemky
                c.execute("""
                    INSERT INTO prijemky_polozky
                      (prijemka_id, material_kod, mnozstvi, cena_jednotka, cena_celkem)
                    VALUES (?, ?, ?, ?, ?)
                """, (prijemka_id, sku, mnozstvi, cena_j,
                      round(mnozstvi * cena_j, 2)))

                # FIFO dávka
                c.execute("""
                    INSERT INTO fifo_davky
                      (material_kod, datum_prijmu, mnozstvi_orig, mnozstvi_zbyla,
                       cena_jednotka, dodavatel, faktura, je_inventura)
                    VALUES (?,?,?,?,?,?,?,?)
                """, (sku, datum, mnozstvi, mnozstvi, cena_j,
                      dodavatel_nazev, faktura, je_inv))

                # Pohyb skladu (navázaný na příjemku)
                c.execute("""
                    INSERT INTO pohyby_skladu
                      (material_kod, typ, mnozstvi, datum, poznamka, prijemka_id)
                    VALUES (?,?,?,?,?,?)
                """, (sku, typ_pohybu, mnozstvi, datum, faktura, prijemka_id))

                # Sklad
                c.execute("INSERT OR IGNORE INTO sklad (material_kod) VALUES (?)", (sku,))
                if je_inv:
                    c.execute("""
                        UPDATE sklad SET
                          naskladneno = naskladneno + ?,
                          skutecny_stav = naskladneno + ?,
                          updated_at = datetime('now')
                        WHERE material_kod=?
                    """, (mnozstvi, mnozstvi, sku))
                else:
                    c.execute("""
                        UPDATE sklad SET
                          naskladneno = naskladneno + ?,
                          updated_at = datetime('now')
                        WHERE material_kod=?
                    """, (mnozstvi, sku))

                imported += 1

        except Exception as e:
            errors.append(f'{faktura}/{dodavatel_nazev}: {str(e)}')

    conn.commit()
    conn.close()
    return jsonify({
        'ok': True,
        'imported': imported,
        'prijemky': prijemky_count,
        'skipped': list(set(skipped)),
        'skipped_count': len(skipped),
        'errors': errors
    })


@app.route('/import-nakupy')
def import_nakupy_page():
    """Stránka pro import naskladnění – upload CSV souboru."""
    SHEET_ID = '1Kih4OoB_qnETXhPQC_N3sQYXd_-m2mtKh0Wwg04IqYo'
    return render_template('import_nakupy.html', sheet_id=SHEET_ID)



# ─── DOCHÁZKA NA DÍLNĚ ────────────────────────────────────────────────────

def _doch_hodiny(cas_od, cas_do):
    """Vypočítá počet odpracovaných hodin z 'HH:MM' stringů. Vrátí 0 pokud neúplné."""
    try:
        h_od = int(cas_od.split(':')[0]) + int(cas_od.split(':')[1]) / 60
        h_do = int(cas_do.split(':')[0]) + int(cas_do.split(':')[1]) / 60
        return max(0.0, round(h_do - h_od, 2))
    except Exception:
        return 0.0


@app.route('/api/dochazka')
def api_dochazka_get():
    """Vrátí uživatele + záznamy docházky v zadaném rozsahu dat."""
    od = request.args.get('od') or date.today().isoformat()
    do = request.args.get('do') or (date.today() + timedelta(days=60)).isoformat()
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, jmeno, barva FROM uzivatele WHERE aktivni=1 ORDER BY jmeno")
    users = [dict(r) for r in c.fetchall()]
    c.execute("""
        SELECT d.*, u.jmeno
        FROM dochazka d
        JOIN uzivatele u ON u.id = d.uzivatel_id
        WHERE d.datum BETWEEN ? AND ?
        ORDER BY d.datum, u.jmeno
    """, (od, do))
    records = [dict(r) for r in c.fetchall()]
    # Přidej počet hodin ke každému záznamu
    for r in records:
        r['hodiny'] = _doch_hodiny(r.get('cas_od',''), r.get('cas_do','')) if r.get('cas_od') and r.get('cas_do') else 0.0
    conn.close()
    return jsonify({'users': users, 'records': records})


@app.route('/api/dochazka', methods=['POST'])
def api_dochazka_save():
    """Uloží (nebo smaže) záznam docházky. Při prázdném cas_od/cas_do smaže záznam."""
    d = request.json or {}
    uid  = d.get('uzivatel_id')
    datum = d.get('datum')
    cas_od = (d.get('cas_od') or '').strip() or None
    cas_do = (d.get('cas_do') or '').strip() or None
    if not uid or not datum:
        return jsonify({'error': 'Chybí uzivatel_id nebo datum'}), 400
    conn = get_db()
    c = conn.cursor()
    if not cas_od:
        # Smazat záznam (není přítomen)
        c.execute("DELETE FROM dochazka WHERE uzivatel_id=? AND datum=?", (uid, datum))
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'deleted': True})
    c.execute("""
        INSERT INTO dochazka (uzivatel_id, datum, cas_od, cas_do, updated_at)
        VALUES (?,?,?,?,datetime('now'))
        ON CONFLICT(uzivatel_id, datum) DO UPDATE SET
            cas_od=excluded.cas_od,
            cas_do=excluded.cas_do,
            updated_at=datetime('now')
    """, (uid, datum, cas_od, cas_do))
    conn.commit()
    new_id = c.lastrowid or 0
    conn.close()
    hodiny = _doch_hodiny(cas_od, cas_do or '') if cas_od and cas_do else 0.0
    return jsonify({'ok': True, 'id': new_id, 'hodiny': hodiny})


@app.route('/api/dochazka/tyden')
def api_dochazka_tyden():
    """Vrátí docházku pro dnes + 7 dní vpřed (pro dashboard)."""
    dnes = date.today().isoformat()
    do   = (date.today() + timedelta(days=7)).isoformat()
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT d.datum, d.cas_od, d.cas_do, u.jmeno, u.barva
        FROM dochazka d
        JOIN uzivatele u ON u.id = d.uzivatel_id
        WHERE d.datum BETWEEN ? AND ? AND d.cas_od IS NOT NULL
        ORDER BY d.datum, d.cas_od
    """, (dnes, do))
    rows = [dict(r) for r in c.fetchall()]
    for r in rows:
        r['hodiny'] = _doch_hodiny(r.get('cas_od',''), r.get('cas_do',''))
    conn.close()
    # Seskup po dnech
    by_day = {}
    for r in rows:
        by_day.setdefault(r['datum'], []).append(r)
    return jsonify({'by_day': by_day, 'dnes': dnes})


# ─── DOCHÁZKA LIVE ────────────────────────────────────────────────────────

@app.route('/api/dochazka-live/stav')
def api_dochazka_live_stav():
    """Vrátí všechny aktivní uživatele + jejich aktuální stav (přítomen/nepřítomen)."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, jmeno, barva FROM uzivatele WHERE aktivni=1 ORDER BY jmeno")
    users = [dict(r) for r in c.fetchall()]
    # Pro každého uživatele zjisti otevřený záznam (přišel, neodešel)
    for u in users:
        c.execute("""
            SELECT id, cas_prichod FROM dochazka_zaznamy
            WHERE uzivatel_id=? AND cas_odchod IS NULL
            ORDER BY cas_prichod DESC LIMIT 1
        """, (u['id'],))
        row = c.fetchone()
        u['pritomen']    = bool(row)
        u['prichod_od']  = row['cas_prichod'] if row else None
        u['otevreny_id'] = row['id'] if row else None
    conn.close()
    return jsonify({'users': users})


@app.route('/api/dochazka-live/prichod', methods=['POST'])
def api_dochazka_live_prichod():
    """Zapíše příchod uživatele (nový záznam). Pokud již má otevřený, vrátí chybu."""
    d = request.json or {}
    uid = d.get('uzivatel_id')
    if not uid:
        return jsonify({'error': 'Chybí uzivatel_id'}), 400
    conn = get_db()
    c = conn.cursor()
    # Zkontroluj, jestli nemá otevřený záznam
    c.execute("SELECT id FROM dochazka_zaznamy WHERE uzivatel_id=? AND cas_odchod IS NULL", (uid,))
    if c.fetchone():
        conn.close()
        return jsonify({'error': 'Uživatel je již přítomen — nejprve zapište odchod.'}), 409
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    datum = now[:10]
    c.execute("""
        INSERT INTO dochazka_zaznamy (uzivatel_id, datum, cas_prichod)
        VALUES (?,?,?)
    """, (uid, datum, now))
    conn.commit()
    rid = c.lastrowid
    conn.close()
    return jsonify({'ok': True, 'id': rid, 'cas_prichod': now})


@app.route('/api/dochazka-live/odchod', methods=['POST'])
def api_dochazka_live_odchod():
    """Zapíše odchod — uzavře otevřený záznam."""
    d = request.json or {}
    uid = d.get('uzivatel_id')
    if not uid:
        return jsonify({'error': 'Chybí uzivatel_id'}), 400
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT id, cas_prichod FROM dochazka_zaznamy
        WHERE uzivatel_id=? AND cas_odchod IS NULL
        ORDER BY cas_prichod DESC LIMIT 1
    """, (uid,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Nenalezen otevřený záznam příchodu.'}), 404
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    c.execute("""
        UPDATE dochazka_zaznamy SET cas_odchod=?, updated_at=datetime('now')
        WHERE id=?
    """, (now, row['id']))
    conn.commit()
    # Spočítej dobu přítomnosti
    from datetime import datetime as dt
    try:
        t_od = dt.strptime(row['cas_prichod'], '%Y-%m-%d %H:%M:%S')
        t_do = dt.strptime(now, '%Y-%m-%d %H:%M:%S')
        sek  = int((t_do - t_od).total_seconds())
    except Exception:
        sek = 0
    conn.close()
    return jsonify({'ok': True, 'cas_odchod': now, 'sekund': sek})


@app.route('/api/dochazka-live/mesic')
def api_dochazka_live_mesic():
    """Záznamy + součet hodin za daný měsíc (výchozí = aktuální)."""
    rok   = int(request.args.get('rok',   date.today().year))
    mesic = int(request.args.get('mesic', date.today().month))
    uid   = request.args.get('uzivatel_id')
    od = f'{rok:04d}-{mesic:02d}-01'
    # Poslední den měsíce
    import calendar
    posledni = calendar.monthrange(rok, mesic)[1]
    do = f'{rok:04d}-{mesic:02d}-{posledni:02d}'
    conn = get_db()
    c = conn.cursor()
    q = """
        SELECT dz.*, u.jmeno, u.barva
        FROM dochazka_zaznamy dz
        JOIN uzivatele u ON u.id = dz.uzivatel_id
        WHERE dz.datum BETWEEN ? AND ?
    """
    params = [od, do]
    if uid:
        q += " AND dz.uzivatel_id=?"
        params.append(uid)
    q += " ORDER BY dz.datum, dz.cas_prichod"
    c.execute(q, params)
    rows = db_rows_to_list(c.fetchall())
    conn.close()
    # Spočítej sekundy pro každý záznam
    from datetime import datetime as dt
    celkem_sek = {}
    for r in rows:
        if r.get('cas_prichod') and r.get('cas_odchod'):
            try:
                sek = int((dt.strptime(r['cas_odchod'], '%Y-%m-%d %H:%M:%S') -
                           dt.strptime(r['cas_prichod'], '%Y-%m-%d %H:%M:%S')).total_seconds())
            except Exception:
                sek = 0
            r['sekund'] = max(sek, 0)
        else:
            r['sekund'] = None  # otevřený (ještě přítomen)
        u = str(r['uzivatel_id'])
        celkem_sek[u] = celkem_sek.get(u, 0) + (r['sekund'] or 0)
    return jsonify({'zaznamy': rows, 'celkem_sek': celkem_sek, 'rok': rok, 'mesic': mesic})


@app.route('/api/dochazka-live/zaznam/<int:zid>', methods=['GET'])
def api_dochazka_live_get(zid):
    """Vrátí jeden záznam docházky."""
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT dz.id, dz.uzivatel_id, u.jmeno, dz.datum,
               dz.cas_prichod, dz.cas_odchod, dz.poznamka, dz.rucne_upraveno
        FROM dochazka_zaznamy dz
        JOIN uzivatele u ON u.id = dz.uzivatel_id
        WHERE dz.id = ?
    """, (zid,))
    row = c.fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'Záznam nenalezen'}), 404
    keys = ['id','uzivatel_id','jmeno','datum','cas_prichod','cas_odchod','poznamka','rucne_upraveno']
    return jsonify(dict(zip(keys, row)))


@app.route('/api/dochazka-live/zaznam/<int:zid>', methods=['PUT'])
def api_dochazka_live_edit(zid):
    """Ruční úprava záznamu (označí se příznakem rucne_upraveno)."""
    d = request.json or {}
    cas_p = (d.get('cas_prichod') or '').strip() or None
    cas_o = (d.get('cas_odchod')  or '').strip() or None
    pozn  = d.get('poznamka', '')
    if not cas_p:
        return jsonify({'error': 'cas_prichod je povinný'}), 400
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        UPDATE dochazka_zaznamy
        SET cas_prichod=?, cas_odchod=?, poznamka=?, rucne_upraveno=1, updated_at=datetime('now')
        WHERE id=?
    """, (cas_p, cas_o, pozn, zid))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/dochazka-live/zaznam/<int:zid>', methods=['DELETE'])
def api_dochazka_live_delete(zid):
    """Smaže záznam docházky."""
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM dochazka_zaznamy WHERE id=?", (zid,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/dochazka-live/zaznam', methods=['POST'])
def api_dochazka_live_new():
    """Ruční přidání záznamu (označí se rucne_upraveno=1)."""
    d = request.json or {}
    uid   = d.get('uzivatel_id')
    cas_p = (d.get('cas_prichod') or '').strip()
    cas_o = (d.get('cas_odchod')  or '').strip() or None
    pozn  = d.get('poznamka', '')
    if not uid or not cas_p:
        return jsonify({'error': 'Chybí uzivatel_id nebo cas_prichod'}), 400
    datum = cas_p[:10]
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO dochazka_zaznamy (uzivatel_id, datum, cas_prichod, cas_odchod, poznamka, rucne_upraveno)
        VALUES (?,?,?,?,?,1)
    """, (uid, datum, cas_p, cas_o, pozn))
    conn.commit()
    rid = c.lastrowid
    conn.close()
    return jsonify({'ok': True, 'id': rid})


# ─── CNC MODUL ────────────────────────────────────────────────────────────

def _cnc_norm_typ(t):
    """Normalizace typu materiálu pro kategorizaci (stejná logika jako u výpočtu časů)."""
    if not t:
        return ''
    t = t.upper()
    for f, r in [('Á','A'),('Č','C'),('Ď','D'),('É','E'),('Ě','E'),
                 ('Í','I'),('Ň','N'),('Ó','O'),('Ř','R'),('Š','S'),
                 ('Ť','T'),('Ú','U'),('Ů','U'),('Ý','Y'),('Ž','Z')]:
        t = t.replace(f, r)
    return t

def _cnc_je_deska(nt):
    return 'DESKA' in nt or 'PREKLIZK' in nt or 'PLAYWOOD' in nt or 'PLAST' in nt

def _cnc_je_podvozek(nt):
    return 'KOLECKO' in nt or 'KOLECK' in nt or 'PODVOZEK' in nt or 'WHEEL' in nt or 'CASTER' in nt

def _cnc_je_pena(nt):
    return 'PEN' in nt or 'FOAM' in nt or 'BALDACHIN' in nt or 'BALDACYN' in nt


@app.route('/api/cnc')
def api_cnc():
    """CNC přehled: aktivní zakázky s BOM materiály rozdělenými do kategorií.
    Checklist: Desky / Podvozky / Pěny (jen pokud jsou přítomny v BOM).
    Params: ?stav=Čeká,CNC hotovo
    """
    stavy_param = request.args.get('stav', 'Čeká,CNC hotovo,Výroba')
    stavy = [s.strip() for s in stavy_param.split(',') if s.strip()]
    placeholders = ','.join('?' * len(stavy))

    conn = get_db()
    c = conn.cursor()

    c.execute(f"""
        SELECT z.id, z.hn_cislo, z.nazev, z.stav, z.pocet_ks, z.termin,
               z.zakaznik, z.prioritni, z.poznamka_cnc, z.typ_casu_id,
               t.nazev AS case_nazev, t.vnitrni_sirka, t.vnitrni_vyska, t.vnitrni_hloubka
        FROM zakazky z
        LEFT JOIN typy_casu t ON t.id = z.typ_casu_id
        WHERE z.stav IN ({placeholders})
        ORDER BY z.prioritni DESC, z.termin ASC, z.created_at ASC
    """, stavy)
    zakazky_rows = c.fetchall()

    result = []
    for zak in zakazky_rows:
        zak_id = zak['id']
        typ_id = zak['typ_casu_id']

        # BOM materiály
        bom_mats = []
        if typ_id:
            c.execute("""
                SELECT k.material_kod, k.mnozstvi, m.nazev, m.typ, m.druh
                FROM kusovniky k
                JOIN materialy m ON m.kod = k.material_kod
                WHERE k.typ_casu_id = ?
                ORDER BY m.typ, m.nazev
            """, (typ_id,))
            bom_mats = [dict(r) for r in c.fetchall()]

        # Kategorizace materiálů
        desky_mats    = [m for m in bom_mats if _cnc_je_deska(_cnc_norm_typ(m.get('typ') or ''))]
        podvozky_mats = [m for m in bom_mats if _cnc_je_podvozek(_cnc_norm_typ(m.get('typ') or ''))]
        peny_mats     = [m for m in bom_mats if _cnc_je_pena(_cnc_norm_typ(m.get('typ') or ''))]

        has_desky    = len(desky_mats) > 0
        has_podvozky = len(podvozky_mats) > 0
        has_peny     = len(peny_mats) > 0

        # CNC checklist (kategorie jako klíče: _DESKY_, _PODVOZKY_, _PENY_)
        c.execute("SELECT material_kod, rezano FROM cnc_rezani WHERE zakazka_id=?", (zak_id,))
        rezani = {r['material_kod']: r['rezano'] for r in c.fetchall()}

        checklist = {
            'desky':    rezani.get('_DESKY_', 0) if has_desky else None,
            'podvozky': rezani.get('_PODVOZKY_', 0) if has_podvozky else None,
            'peny':     rezani.get('_PENY_', 0) if has_peny else None,
        }

        # Počet splněných/celkových checkboxů
        aktivni = [v for v in checklist.values() if v is not None]
        cnc_celkem = len(aktivni)
        cnc_hotovo = sum(1 for v in aktivni if v)

        result.append({
            **dict(zak),
            'desky_mats':    [m['nazev'] for m in desky_mats],
            'podvozky_mats': [m['nazev'] for m in podvozky_mats],
            'peny_mats':     [m['nazev'] for m in peny_mats],
            'has_desky':    has_desky,
            'has_podvozky': has_podvozky,
            'has_peny':     has_peny,
            'checklist':    checklist,
            'cnc_celkem':   cnc_celkem,
            'cnc_hotovo':   cnc_hotovo,
        })

    conn.close()
    return jsonify({'items': result})


@app.route('/api/cnc/<int:zak_id>/toggle', methods=['POST'])
def api_cnc_toggle(zak_id):
    """Přepnout stav vyřezání materiálu. Body: {material_kod, rezano}"""
    data = request.json
    mat_kod = data.get('material_kod')
    rezano = int(data.get('rezano', 1))
    if not mat_kod:
        return jsonify({'error': 'material_kod required'}), 400

    conn = get_db()
    c = conn.cursor()
    if rezano:
        c.execute("""
            INSERT OR REPLACE INTO cnc_rezani (zakazka_id, material_kod, rezano, updated_at)
            VALUES (?, ?, 1, datetime('now'))
        """, (zak_id, mat_kod))
    else:
        c.execute("DELETE FROM cnc_rezani WHERE zakazka_id=? AND material_kod=?", (zak_id, mat_kod))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/cnc/<int:zak_id>/toggle-all', methods=['POST'])
def api_cnc_toggle_all(zak_id):
    """Označit/odznačit všechny materiály zakázky. Body: {rezano, material_kody[]}"""
    data = request.json
    rezano = int(data.get('rezano', 1))
    kody = data.get('material_kody', [])

    conn = get_db()
    c = conn.cursor()
    if rezano:
        for kod in kody:
            c.execute("""
                INSERT OR REPLACE INTO cnc_rezani (zakazka_id, material_kod, rezano, updated_at)
                VALUES (?, ?, 1, datetime('now'))
            """, (zak_id, kod))
    else:
        c.execute("DELETE FROM cnc_rezani WHERE zakazka_id=?", (zak_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ─────────────────────────────────────────────────────────────────────────────
# NABÍDKY
# ─────────────────────────────────────────────────────────────────────────────

def _fifo_cena(c, material_kod, per_unit=False):
    """Vrátí vážený průměr FIFO ceny pro daný materiál (nebo 0).
    per_unit=True → přepočítá na správnou jednotku (m², m, ks) dle balenf a typu.
    Fallback 1: poslední zaúčtovaná nákupní cena z příjemek.
    Fallback 2: nc_bez_dph z číselníku materiálů."""
    # FIFO weighted average (sloupec mnozstvi_zbyla)
    c.execute("""
        SELECT SUM(mnozstvi_zbyla * cena_jednotka) / NULLIF(SUM(mnozstvi_zbyla), 0)
        FROM fifo_davky
        WHERE material_kod=? AND mnozstvi_zbyla > 0 AND zruseno = 0
    """, (material_kod,))
    row = c.fetchone()
    cena = None
    if row and row[0]:
        cena = float(row[0])
    if cena is None:
        # Fallback: poslední nákupní cena z příjemek
        c.execute("""
            SELECT pp.cena_jednotka
            FROM prijemky_polozky pp
            JOIN prijemky pr ON pr.id = pp.prijemka_id
            WHERE pp.material_kod = ? AND pr.stav = 'zaúčtováno'
            ORDER BY pr.datum DESC, pr.id DESC LIMIT 1
        """, (material_kod,))
        row2 = c.fetchone()
        if row2 and row2[0]:
            cena = float(row2[0])
    if cena is None:
        # Fallback: nc_bez_dph z číselníku
        c.execute("SELECT nc_bez_dph FROM materialy WHERE kod=?", (material_kod,))
        r = c.fetchone()
        cena = float(r[0]) if r and r[0] else 0.0
    if per_unit and cena:
        # Přepočet na jednotku (m², m nebo ks)
        c.execute("SELECT typ, balenf FROM materialy WHERE kod=?", (material_kod,))
        mr = c.fetchone()
        if mr:
            j = _mat_jednotka(mr['typ'])
            bf = float(mr['balenf'] or 1) or 1
            if j in ('m2', 'm'):
                cena = cena / bf
    return round(cena, 4)


# ── PŘEKLADAČ KÓDŮ ────────────────────────────────────────────────────────

@app.route('/api/nabidky/prekladac')
def api_prekladac_list():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM nabidky_prekladac ORDER BY externi_kod")
    items = db_rows_to_list(c.fetchall())
    conn.close()
    return jsonify({'items': items})

@app.route('/api/nabidky/prekladac', methods=['POST'])
def api_prekladac_add():
    d = request.json or {}
    if not d.get('externi_kod') or not d.get('interni_kod'):
        return jsonify({'error': 'Vyplňte oba kódy'}), 400
    conn = get_db()
    c = conn.cursor()
    ext = d['externi_kod'].strip()
    int_ = d['interni_kod'].strip()
    poz = d.get('poznamka', '') or ''
    # INSERT OR REPLACE — pokud externí kód již existuje, přepíše ho
    c.execute("INSERT OR REPLACE INTO nabidky_prekladac (externi_kod, interni_kod, poznamka) VALUES (?,?,?)",
              (ext, int_, poz))
    conn.commit()
    # Vrať id vloženého/aktualizovaného záznamu
    c.execute("SELECT id FROM nabidky_prekladac WHERE externi_kod=?", (ext,))
    row = c.fetchone()
    conn.close()
    return jsonify({'id': row['id'] if row else c.lastrowid})

@app.route('/api/nabidky/prekladac/<int:pid>', methods=['PUT'])
def api_prekladac_update(pid):
    d = request.json or {}
    fields = ['externi_kod', 'interni_kod', 'poznamka']
    sets = [f + '=?' for f in fields if f in d]
    vals = [d[f] for f in fields if f in d]
    if sets:
        conn = get_db()
        c = conn.cursor()
        c.execute(f"UPDATE nabidky_prekladac SET {','.join(sets)} WHERE id=?", vals + [pid])
        conn.commit()
        conn.close()
    return jsonify({'ok': True})

@app.route('/api/nabidky/prekladac/<int:pid>', methods=['DELETE'])
def api_prekladac_delete(pid):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM nabidky_prekladac WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/nabidky')
def api_nabidky_list():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM nabidky ORDER BY created_at DESC")
    items = db_rows_to_list(c.fetchall())
    conn.close()
    return jsonify({'items': items})


@app.route('/api/nabidky', methods=['POST'])
def api_nabidky_create():
    d = request.json or {}
    if not d.get('nazev') or not d.get('zakaznik'):
        return jsonify({'error': 'Vyplňte název a zákazníka'}), 400
    conn = get_db()
    c = conn.cursor()
    c.execute("""INSERT INTO nabidky (nazev, zakaznik, email, tel, pocet_ks,
                 hodiny_vyroba, hodiny_kresleni, hodiny_cnc, sazba_prace, koeficient, kurz_eur)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
              (d['nazev'], d['zakaznik'], d.get('email'), d.get('tel'),
               d.get('pocet_ks', 1), 0, 0, 0, 300, 2.2, d.get('kurz_eur', 25)))
    conn.commit()
    new_id = c.lastrowid
    conn.close()
    return jsonify({'id': new_id})


@app.route('/api/nabidky/<int:nid>')
def api_nabidka_detail(nid):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM nabidky WHERE id=?", (nid,))
    n = c.fetchone()
    if not n:
        conn.close()
        return jsonify({'error': 'Nenalezeno'}), 404

    # Import položky – prořez vždy aktualizujeme z aktuálního nastavení (ne z DB cache)
    c.execute("""
        SELECT ni.*, m.typ AS mat_typ
        FROM nabidky_import ni
        LEFT JOIN materialy m ON m.kod = ni.material_kod
        WHERE ni.nabidka_id=? ORDER BY ni.id
    """, (nid,))
    import_items = []
    for row in c.fetchall():
        item = dict(row)
        mat_typ = item.pop('mat_typ', None)
        item['prorez_procento'] = _get_prorez(c, mat_typ) if mat_typ else 0.0
        import_items.append(item)

    c.execute("""SELECT nm.*, m.nazev as mat_nazev, m.typ as mat_typ
                 FROM nabidky_materialy nm
                 LEFT JOIN materialy m ON m.kod = nm.material_kod
                 WHERE nm.nabidka_id=? ORDER BY nm.id""", (nid,))
    materialy = db_rows_to_list(c.fetchall())

    c.execute("SELECT * FROM nabidky_extra WHERE nabidka_id=? ORDER BY id", (nid,))
    extra = db_rows_to_list(c.fetchall())

    c.execute("""SELECT nh.*, m.nazev as mat_nazev, m.typ as mat_typ
                 FROM nabidky_hw nh
                 LEFT JOIN materialy m ON m.kod = nh.material_kod
                 WHERE nh.nabidka_id=? ORDER BY nh.id""", (nid,))
    hw_items = db_rows_to_list(c.fetchall())

    # Propojená kancelářská zakázka (Příprava zakázek)
    c.execute("SELECT id, nazev FROM kancelar_zakazky WHERE nabidka_id=? LIMIT 1", (nid,))
    kz = c.fetchone()

    conn.close()
    result = dict(n)
    result['import_items'] = import_items
    result['materialy'] = materialy
    result['extra'] = extra
    result['hw_items'] = hw_items
    result['kancelar_zakazka'] = dict(kz) if kz else None
    return jsonify(result)


@app.route('/api/nabidky/<int:nid>', methods=['PUT'])
def api_nabidka_update(nid):
    d = request.json or {}
    fields = ['nazev', 'zakaznik', 'email', 'tel', 'pocet_ks',
              'hodiny_vyroba', 'hodiny_kresleni', 'hodiny_cnc',
              'sazba_prace', 'koeficient', 'kurz_eur', 'poznamka', 'stav']
    sets = [f + '=?' for f in fields if f in d]
    vals = [d[f] for f in fields if f in d]
    if sets:
        conn = get_db()
        c = conn.cursor()
        c.execute(f"UPDATE nabidky SET {','.join(sets)} WHERE id=?", vals + [nid])
        conn.commit()
        conn.close()
    return jsonify({'ok': True})


@app.route('/api/nabidky/<int:nid>', methods=['DELETE'])
def api_nabidka_delete(nid):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM nabidky WHERE id=?", (nid,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/nabidky/<int:nid>/import', methods=['POST'])
def api_nabidka_import_save(nid):
    """Uloží parsovaný import z konfigurátoru (celý přepíše). Aplikuje překladač externích kódů."""
    items = request.json or []
    conn = get_db()
    c = conn.cursor()
    # Načti překladač kódů (externi_kod → interni_kod)
    c.execute("SELECT externi_kod, interni_kod FROM nabidky_prekladac")
    prekladac = {r['externi_kod'].strip(): r['interni_kod'].strip() for r in c.fetchall()}
    c.execute("DELETE FROM nabidky_import WHERE nabidka_id=?", (nid,))
    for it in items:
        kod_externi = (it.get('material_kod', '') or '').strip()
        # Přeložit kód pokud existuje v překladači
        kod = prekladac.get(kod_externi, kod_externi)
        fifo = _fifo_cena(c, kod)
        # Auto-doplnění názvu a typu z číselníku materiálů
        nazev = it.get('nazev_override') or ''
        mat_typ = None
        c.execute("SELECT nazev, typ FROM materialy WHERE kod=?", (kod,))
        mat_r = c.fetchone()
        if mat_r:
            if not nazev:
                nazev = mat_r[0] or ''
            mat_typ = mat_r[1]
        # Prořez platí pro libovolný typ s nenulovým prořezem (Deska, Pěna, Profil AL, …)
        prorez_pct = _get_prorez(c, mat_typ) if mat_typ else 0.0
        c.execute("""INSERT INTO nabidky_import (nabidka_id, material_kod, mnozstvi, cena_jednotka, nazev_override, prorez_procento)
                     VALUES (?,?,?,?,?,?)""",
                  (nid, kod, it.get('mnozstvi', 0), fifo, nazev, prorez_pct))
    # Auto-přepočítej HW spojovníky po změně importu
    _nabidka_hw_prepocitat_internal(c, nid)
    conn.commit()
    # Vrátíme uložené položky (s cenami)
    c.execute("SELECT * FROM nabidky_import WHERE nabidka_id=? ORDER BY id", (nid,))
    saved = db_rows_to_list(c.fetchall())
    conn.close()
    return jsonify({'ok': True, 'items': saved})


@app.route('/api/nabidky/<int:nid>/import/<int:iid>', methods=['PUT'])
def api_nabidka_import_update(nid, iid):
    d = request.json or {}
    fields = ['mnozstvi', 'cena_jednotka', 'nazev_override', 'prorez_procento']
    sets = [f + '=?' for f in fields if f in d]
    vals = [d[f] for f in fields if f in d]
    conn = get_db()
    c = conn.cursor()
    if sets:
        c.execute(f"UPDATE nabidky_import SET {','.join(sets)} WHERE id=? AND nabidka_id=?",
                  vals + [iid, nid])
    # Auto-přepočítej HW spojovníky po změně množství
    if 'mnozstvi' in d:
        _nabidka_hw_prepocitat_internal(c, nid)
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/nabidky/<int:nid>/materialy', methods=['POST'])
def api_nabidka_mat_add(nid):
    d = request.json or {}
    conn = get_db()
    c = conn.cursor()
    # Auto-fill FIFO price per jednotku (m², m nebo ks)
    mat_kod = d.get('material_kod', '')
    cena_m2 = d.get('cena_m2', 0)
    if not cena_m2 and mat_kod:
        cena_m2 = _fifo_cena(c, mat_kod, per_unit=True)
    c.execute("""INSERT INTO nabidky_materialy (nabidka_id, material_kod, sirka_mm, vyska_mm, pocet_ks, cena_m2)
                 VALUES (?,?,?,?,?,?)""",
              (nid, mat_kod or None, d.get('sirka_mm', 0), d.get('vyska_mm', 0),
               d.get('pocet_ks', 1), cena_m2))
    new_id = c.lastrowid
    # Auto-přepočítej HW spojovníky
    _nabidka_hw_prepocitat_internal(c, nid)
    conn.commit()
    conn.close()
    return jsonify({'id': new_id})


@app.route('/api/nabidky/<int:nid>/materialy/<int:mid>', methods=['PUT'])
def api_nabidka_mat_update(nid, mid):
    d = request.json or {}
    fields = ['material_kod', 'sirka_mm', 'vyska_mm', 'pocet_ks', 'cena_m2', 'prorez_procento']
    sets = [f + '=?' for f in fields if f in d]
    vals = [d[f] for f in fields if f in d]
    conn = get_db()
    c = conn.cursor()
    if sets:
        c.execute(f"UPDATE nabidky_materialy SET {','.join(sets)} WHERE id=? AND nabidka_id=?",
                  vals + [mid, nid])
    # Auto-přepočítej HW spojovníky při změně materiálu nebo množství
    if any(f in d for f in ('material_kod', 'pocet_ks')):
        _nabidka_hw_prepocitat_internal(c, nid)
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/nabidky/<int:nid>/materialy/<int:mid>', methods=['DELETE'])
def api_nabidka_mat_delete(nid, mid):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM nabidky_materialy WHERE id=? AND nabidka_id=?", (mid, nid))
    # Auto-přepočítej HW spojovníky po smazání
    _nabidka_hw_prepocitat_internal(c, nid)
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/nabidky/<int:nid>/extra', methods=['POST'])
def api_nabidka_extra_add(nid):
    d = request.json or {}
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT INTO nabidky_extra (nabidka_id, nazev, cena) VALUES (?,?,?)",
              (nid, d.get('nazev', 'Další náklad'), d.get('cena', 0)))
    conn.commit()
    new_id = c.lastrowid
    conn.close()
    return jsonify({'id': new_id})


@app.route('/api/nabidky/<int:nid>/extra/<int:eid>', methods=['PUT'])
def api_nabidka_extra_update(nid, eid):
    d = request.json or {}
    fields = ['nazev', 'cena']
    sets = [f + '=?' for f in fields if f in d]
    vals = [d[f] for f in fields if f in d]
    if sets:
        conn = get_db()
        c = conn.cursor()
        c.execute(f"UPDATE nabidky_extra SET {','.join(sets)} WHERE id=? AND nabidka_id=?",
                  vals + [eid, nid])
        conn.commit()
        conn.close()
    return jsonify({'ok': True})


@app.route('/api/nabidky/<int:nid>/extra/<int:eid>', methods=['DELETE'])
def api_nabidka_extra_delete(nid, eid):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM nabidky_extra WHERE id=? AND nabidka_id=?", (eid, nid))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ── NABÍDKY – HW / SPOJOVACÍ MATERIÁL ────────────────────────────────────────

@app.route('/api/nabidky/<int:nid>/hw', methods=['POST'])
def api_nabidka_hw_add(nid):
    """Přidá řádek spojovacího materiálu (ks) do nabídky."""
    d = request.json or {}
    conn = get_db()
    c = conn.cursor()
    mat_kod = d.get('material_kod') or None
    cena_ks = d.get('cena_ks', 0)
    nazev = d.get('nazev_override') or ''
    # Auto-doplnění ceny z FIFO
    if not cena_ks and mat_kod:
        cena_ks = _fifo_cena(c, mat_kod)
    # Auto-doplnění názvu z číselníku
    if not nazev and mat_kod:
        c.execute("SELECT nazev FROM materialy WHERE kod=?", (mat_kod,))
        mr = c.fetchone()
        nazev = mr[0] if mr else ''
    c.execute("""INSERT INTO nabidky_hw (nabidka_id, material_kod, nazev_override, mnozstvi, cena_ks)
                 VALUES (?,?,?,?,?)""",
              (nid, mat_kod, nazev, d.get('mnozstvi', 1), cena_ks))
    conn.commit()
    new_id = c.lastrowid
    conn.close()
    return jsonify({'id': new_id, 'cena_ks': cena_ks, 'nazev_override': nazev})


@app.route('/api/nabidky/<int:nid>/hw/<int:hwid>', methods=['PUT'])
def api_nabidka_hw_update(nid, hwid):
    d = request.json or {}
    fields = ['material_kod', 'nazev_override', 'mnozstvi', 'cena_ks', 'auto_generated']
    sets = [f + '=?' for f in fields if f in d]
    vals = [d[f] for f in fields if f in d]
    if sets:
        conn = get_db()
        c = conn.cursor()
        c.execute(f"UPDATE nabidky_hw SET {','.join(sets)} WHERE id=? AND nabidka_id=?",
                  vals + [hwid, nid])
        conn.commit()
        conn.close()
    return jsonify({'ok': True})


@app.route('/api/nabidky/<int:nid>/hw/<int:hwid>', methods=['DELETE'])
def api_nabidka_hw_delete(nid, hwid):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM nabidky_hw WHERE id=? AND nabidka_id=?", (hwid, nid))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


def _nabidka_hw_prepocitat_internal(c, nid):
    """Pomocná funkce: přepočítá auto-generované HW spojovníky pro nabídku.
    Pracuje na otevřeném kurzoru c (volající musí zavolat conn.commit()).
    Vrací slovník { spoj_agg, mat_qty }.
    """
    mat_qty = {}   # material_kod → celkové množství

    # Z nabidky_materialy (desky/pěny — pocet_ks kusů)
    c.execute("SELECT material_kod, pocet_ks FROM nabidky_materialy WHERE nabidka_id=? AND material_kod IS NOT NULL", (nid,))
    for row in c.fetchall():
        kod = row['material_kod']
        mat_qty[kod] = mat_qty.get(kod, 0) + (float(row['pocet_ks']) or 0)

    # Z nabidky_import (HW díly + desky z konfigurátoru — mnozstvi v ks)
    c.execute("SELECT material_kod, mnozstvi FROM nabidky_import WHERE nabidka_id=? AND material_kod IS NOT NULL", (nid,))
    for row in c.fetchall():
        kod = row['material_kod']
        mat_qty[kod] = mat_qty.get(kod, 0) + (float(row['mnozstvi']) or 0)

    # Pro každý materiál zjisti spojovníky
    spoj_agg = {}   # spojovaci_kod → { nazev, mnozstvi, nc_bez_dph }
    for mat_kod, qty in mat_qty.items():
        if qty <= 0:
            continue
        c.execute("""
            SELECT ms.spojovaci_kod, ms.mnozstvi_na_kus,
                   m.nazev, m.nc_bez_dph
            FROM material_spojeniky ms
            JOIN materialy m ON m.kod = ms.spojovaci_kod
            WHERE ms.material_kod = ?
        """, (mat_kod,))
        for sp in c.fetchall():
            kod = sp['spojovaci_kod']
            mnoz = round(qty * float(sp['mnozstvi_na_kus']), 3)
            if kod in spoj_agg:
                spoj_agg[kod]['mnozstvi'] += mnoz
            else:
                spoj_agg[kod] = {
                    'nazev': sp['nazev'],
                    'mnozstvi': mnoz,
                    'nc_bez_dph': sp['nc_bez_dph'],
                }

    # Vymaž stávající auto-generované HW řádky
    c.execute("DELETE FROM nabidky_hw WHERE nabidka_id=? AND auto_generated=1", (nid,))

    # Vlož nové auto-generované řádky s FIFO cenami
    for kod, sp in spoj_agg.items():
        mnozstvi = round(sp['mnozstvi'], 2)
        if mnozstvi <= 0:
            continue
        fifo = _fifo_cena(c, kod)
        cena_ks = fifo or float(sp['nc_bez_dph'] or 0)
        c.execute("""
            INSERT INTO nabidky_hw (nabidka_id, material_kod, nazev_override, mnozstvi, cena_ks, auto_generated)
            VALUES (?,?,?,?,?,1)
        """, (nid, kod, sp['nazev'], mnozstvi, cena_ks))

    return {'spoj_agg': spoj_agg, 'mat_qty': mat_qty}


@app.route('/api/nabidky/<int:nid>/hw/prepocitat', methods=['POST'])
def api_nabidka_hw_prepocitat(nid):
    """Přepočítá HW spojovníky z aktuálních materiálů + importu nabídky.
    Ruční řádky (auto_generated=0) zůstanou nedotčeny.
    """
    conn = get_db()
    c = conn.cursor()

    result = _nabidka_hw_prepocitat_internal(c, nid)
    conn.commit()

    c.execute("""SELECT nh.*, m.nazev as mat_nazev, m.typ as mat_typ
                 FROM nabidky_hw nh
                 LEFT JOIN materialy m ON m.kod = nh.material_kod
                 WHERE nh.nabidka_id=? ORDER BY nh.auto_generated DESC, nh.id""", (nid,))
    hw_items = db_rows_to_list(c.fetchall())
    conn.close()

    return jsonify({'ok': True, 'hw_items': hw_items,
                    'pocet_spojovniku': len(result['spoj_agg']),
                    'zdrojove_materialy': len(result['mat_qty'])})


# ─────────────────────────────────────────────────────────────────────────────
# SPA routes
# ─────────────────────────────────────────────────────────────────────────────
@app.route('/typy-casu/<int:typ_id>')
@app.route('/zakazky')
@app.route('/zakazky/<int:zak_id>')
# ─── STRÁNKY (SPA) ────────────────────────────────────────────────────────
@app.route('/materialy')
@app.route('/sklad')
@app.route('/typy-casu')
@app.route('/typy-casu/<int:typ_id>')
@app.route('/zakazky')
@app.route('/zakazky/<int:zak_id>')
@app.route('/inventura')
@app.route('/fakturace')
@app.route('/kancelar')
@app.route('/nastaveni')
@app.route('/nakupy')
@app.route('/cnc')
@app.route('/dochazka')
@app.route('/nabidky')
@app.route('/nabidky/<int:nid>')
def spa_routes(**kwargs):
    return render_template('app.html')


if __name__ == '__main__':
    init_db()
    auto_migrate()
    print("\n" + "="*60)
    print("  Flight Case výrobní systém")
    print("  Otevři v prohlížeči: http://localhost:5000")
    print("  Ze sítě:             http://<IP-tohoto-PC>:5000")
    print("="*60 + "\n")
    app.run(host='0.0.0.0', port=5000, debug=False)
