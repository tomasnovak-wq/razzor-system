"""
Flight Case výrobní systém — Flask server
Spuštění: python app.py
Přístup: http://localhost:5001  (nebo http://IP_PC:5001 z jiných strojů v síti)
"""
from flask import Flask, render_template, request, jsonify, redirect, url_for, send_file
import sqlite3
import os
import sys
import json
import re
import csv
import io
import subprocess
import threading
import time
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders
from datetime import date, datetime, timedelta
from database import get_db, init_db, auto_migrate, aktualizuj_stav_skladu, zkontroluj_dostupnost_materialu, odepis_material_ze_skladu, vypocti_cenu_dilu
from pdf_faktura import vygeneruj_pdf

app = Flask(__name__)
app.secret_key = 'flightcase-system-2026'

# ─── DATABÁZE – inicializace při startu (gunicorn i přímé spuštění) ──────
init_db()
auto_migrate()

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

def get_nastaveni(klic, default=None):
    """Načte hodnotu z tabulky nastaveni. Vrátí default pokud klíč neexistuje."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT hodnota FROM nastaveni WHERE klic=?", (klic,))
    row = c.fetchone()
    conn.close()
    return row['hodnota'] if row else default

def set_nastaveni(klic, hodnota):
    """Uloží nebo aktualizuje hodnotu v tabulce nastaveni."""
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO nastaveni (klic, hodnota) VALUES (?,?)", (klic, hodnota))
    conn.commit()
    conn.close()

def send_invoice_email(fak_id, pdf_bytes, cislo_faktury):
    """Odešle fakturu e-mailem na nakonfigurované adresy přes Gmail SMTP.
    Spouští se v samostatném vlákně — chyby loguje na stdout, neblokuje odpověď API."""
    try:
        gmail_user   = get_nastaveni('email_gmail_user', '')
        gmail_pass   = get_nastaveni('email_gmail_pass', '')
        prijemci_raw = get_nastaveni('email_prijemci', '')
        predmet      = get_nastaveni('email_predmet', 'Faktura {cislo}')
        telo         = get_nastaveni('email_telo', 'Dobrý den,\n\nv příloze zasíláme fakturu č. {cislo}.\n\nS pozdravem\nRazzor Cases')

        if not gmail_user or not gmail_pass or not prijemci_raw.strip():
            print(f"[Email] Přeskakuji — email není nakonfigurován (faktura {cislo_faktury})")
            return

        prijemci = [a.strip() for a in prijemci_raw.replace(';', ',').split(',') if a.strip()]
        if not prijemci:
            print(f"[Email] Přeskakuji — žádní příjemci (faktura {cislo_faktury})")
            return

        predmet_final = predmet.replace('{cislo}', cislo_faktury)
        telo_final    = telo.replace('{cislo}', cislo_faktury)

        msg = MIMEMultipart()
        msg['From']    = gmail_user
        msg['To']      = ', '.join(prijemci)
        msg['Subject'] = predmet_final
        msg.attach(MIMEText(telo_final, 'plain', 'utf-8'))

        # Příloha — PDF faktura
        part = MIMEBase('application', 'pdf')
        part.set_payload(pdf_bytes)
        encoders.encode_base64(part)
        part.add_header('Content-Disposition', f'attachment; filename="Faktura_{cislo_faktury}.pdf"')
        msg.attach(part)

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(gmail_user, gmail_pass)
            server.sendmail(gmail_user, prijemci, msg.as_bytes())

        print(f"[Email] Faktura {cislo_faktury} odeslána na: {', '.join(prijemci)}")

    except Exception as e:
        print(f"[Email] CHYBA při odesílání faktury {cislo_faktury}: {e}")

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
              'dodaci_lhuta','sirka_hw','priorita','zobrazovat','poznamka','nity',
              'prorez_procento']
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

# ─── BAREVNÉ PROFILY MATERIÁLŮ ───────────────────────────────────────────────

@app.route('/api/barvy-materialu', methods=['GET'])
def api_barvy_materialu_get():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT typ, barva FROM barvy_materialu ORDER BY typ")
    rows = {r['typ']: r['barva'] for r in c.fetchall()}
    conn.close()
    return jsonify({'barvy': rows})

@app.route('/api/barvy-materialu', methods=['POST'])
def api_barvy_materialu_post():
    data = request.json or {}
    barvy = data.get('barvy', {})   # {typ: barva}
    conn = get_db()
    c = conn.cursor()
    for typ, barva in barvy.items():
        if barva:
            c.execute("INSERT OR REPLACE INTO barvy_materialu (typ, barva) VALUES (?,?)", (typ, barva))
        else:
            c.execute("DELETE FROM barvy_materialu WHERE typ=?", (typ,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

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

    # BOM – přímé položky (individuální prořez materiálu má přednost před globálním dle typu)
    c.execute("""
        SELECT k.material_kod, k.mnozstvi,
               m.nazev, m.typ, m.druh, m.nc_bez_dph,
               m.hmotnost as hmotnost_j, m.nity, m.oblibeny,
               m.prorez_procento,
               COALESCE(s.naskladneno - s.pouzito, 0) as stav_skladu,
               COALESCE(m.prorez_procento, p.procento, 0) as prorez_efektivni,
               (k.mnozstvi * (1.0 + COALESCE(m.prorez_procento, p.procento, 0)/100.0) * m.nc_bez_dph) as cena_polozky,
               (k.mnozstvi * m.hmotnost) as hmotnost_polozky
        FROM kusovniky k
        JOIN materialy m ON m.kod = k.material_kod
        LEFT JOIN prorez p ON p.typ = m.typ
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

    c.execute("SELECT COUNT(*) FROM profily_plan WHERE typ_casu_id=?", (typ_id,))
    profily_count = c.fetchone()[0]

    conn.close()
    return jsonify({'typ': dict(typ), 'bom': bom, 'spojeniky': spojeniky,
                    'cena_dilu': cena_dilu, 'hmotnost_bom': hmotnost_bom, 'links': links,
                    'profily_count': profily_count})

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
    hn_cislo = (data.get('hn_cislo') or '').strip()
    nazev    = (data.get('nazev') or '').strip()
    if not hn_cislo or not nazev:
        return jsonify({'error': 'HN číslo a název jsou povinné.'}), 400
    conn = get_db()
    c = conn.cursor()
    # Kontrola duplicitního HN čísla
    c.execute("SELECT id, nazev FROM typy_casu WHERE hn_cislo=?", (hn_cislo,))
    existing = c.fetchone()
    if existing:
        conn.close()
        return jsonify({'error': f'HN číslo {hn_cislo} již existuje v BOM (typ: "{existing["nazev"]}")'}), 409
    try:
        c.execute("""
            INSERT INTO typy_casu (hn_cislo, nazev, typ_korpusu, vnitrni_sirka,
            vnitrni_vyska, vnitrni_hloubka, cena_vyroby, cas_narocnost,
            hmotnost, prodej_ap_bez_dph, poznamka)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (hn_cislo, nazev, data.get('typ_korpusu',''),
              data.get('vnitrni_sirka',0), data.get('vnitrni_vyska',0),
              data.get('vnitrni_hloubka',0), data.get('cena_vyroby',0),
              data.get('cas_narocnost',0), data.get('hmotnost',0),
              data.get('prodej_ap_bez_dph',0), data.get('poznamka','')))
        new_id = c.lastrowid
        # Automaticky vložit výchozí BOM materiály
        c.execute("SELECT material_kod, mnozstvi FROM vychozi_bom ORDER BY poradi")
        vychozi = c.fetchall()
        for v in vychozi:
            c.execute("INSERT OR IGNORE INTO kusovniky (typ_casu_id, material_kod, mnozstvi) VALUES (?,?,?)",
                      (new_id, v['material_kod'], v['mnozstvi']))
        if vychozi:
            vypocti_cenu_dilu(conn, new_id)
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'id': new_id})
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 400

@app.route('/api/typy-casu/<int:typ_id>', methods=['DELETE'])
def api_typ_casu_delete(typ_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM kusovniky WHERE typ_casu_id=?", (typ_id,))
    c.execute("DELETE FROM profily_plan WHERE typ_casu_id=?", (typ_id,))
    c.execute("DELETE FROM typy_casu_links WHERE typ_casu_id=?", (typ_id,))
    c.execute("DELETE FROM typy_casu WHERE id=?", (typ_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

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
        c.execute("SELECT id, nazev, url, typ_json, poradi FROM typy_casu_links WHERE typ_casu_id=? ORDER BY poradi, id", (typ_id,))
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return jsonify({'links': rows})
    # POST – nahradí všechny odkazy pro tento typ
    data = request.get_json() or {}
    links = data.get('links', [])
    c.execute("DELETE FROM typy_casu_links WHERE typ_casu_id=?", (typ_id,))
    for i, lnk in enumerate(links):
        nazev    = (lnk.get('nazev') or '').strip()
        url      = (lnk.get('url')   or '').strip()
        typy     = lnk.get('typy', ['ostatni'])
        if not isinstance(typy, list): typy = ['ostatni']
        typ_json = json.dumps(typy, ensure_ascii=False)
        if not url:
            continue
        c.execute("INSERT INTO typy_casu_links (typ_casu_id, nazev, url, typ_json, poradi) VALUES (?, ?, ?, ?, ?)",
                  (typ_id, nazev or 'ostatni', url, typ_json, i))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

## ── Přílohy typů casů (uploadované soubory) ────────────────────────────────

TC_PRILOHY_DIR = os.path.join('/data', 'tc_prilohy') if os.path.isdir('/data') else os.path.join(os.path.dirname(__file__), 'data', 'tc_prilohy')

@app.route('/api/typy-casu/<int:typ_id>/prilohy', methods=['GET'])
def api_tc_prilohy_list(typ_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, filename, mime_type, velikost, typ_json, created_at FROM typy_casu_prilohy WHERE typ_casu_id=? ORDER BY created_at, id", (typ_id,))
    items = db_rows_to_list(c.fetchall())
    conn.close()
    return jsonify({'prilohy': items})

@app.route('/api/typy-casu/<int:typ_id>/prilohy', methods=['POST'])
def api_tc_priloha_upload(typ_id):
    if 'file' not in request.files:
        return jsonify({'error': 'Žádný soubor'}), 400
    f = request.files['file']
    folder = os.path.join(TC_PRILOHY_DIR, str(typ_id))
    os.makedirs(folder, exist_ok=True)
    safe_name = f.filename.replace('/', '_').replace('\\', '_')
    filepath = os.path.join(folder, safe_name)
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
        INSERT INTO typy_casu_prilohy (typ_casu_id, filename, filepath, mime_type, velikost)
        VALUES (?,?,?,?,?)
    """, (typ_id, safe_name, filepath, mime, velikost))
    fid = c.lastrowid
    conn.commit()
    c.execute("SELECT id, filename, mime_type, velikost, typ_json, created_at FROM typy_casu_prilohy WHERE id=?", (fid,))
    row = dict(c.fetchone())
    conn.close()
    return jsonify({'ok': True, 'priloha': row})

@app.route('/api/typy-casu/<int:typ_id>/prilohy/<int:fid>', methods=['PATCH'])
def api_tc_priloha_patch(typ_id, fid):
    data = request.get_json(force=True) or {}
    typy = data.get('typy', ['ostatni'])
    if not isinstance(typy, list):
        typy = ['ostatni']
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE typy_casu_prilohy SET typ_json=? WHERE id=? AND typ_casu_id=?",
              (json.dumps(typy, ensure_ascii=False), fid, typ_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/typy-casu/<int:typ_id>/prilohy/<int:fid>/download')
def api_tc_priloha_download(typ_id, fid):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM typy_casu_prilohy WHERE id=? AND typ_casu_id=?", (fid, typ_id))
    row = c.fetchone()
    conn.close()
    if not row or not os.path.exists(row['filepath']):
        return jsonify({'error': 'Soubor nenalezen'}), 404
    return send_file(row['filepath'], as_attachment=True, download_name=row['filename'])

@app.route('/api/typy-casu/<int:typ_id>/prilohy/<int:fid>/view')
def api_tc_priloha_view(typ_id, fid):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM typy_casu_prilohy WHERE id=? AND typ_casu_id=?", (fid, typ_id))
    row = c.fetchone()
    conn.close()
    if not row or not os.path.exists(row['filepath']):
        return jsonify({'error': 'Soubor nenalezen'}), 404
    return send_file(row['filepath'], as_attachment=False, download_name=row['filename'])

@app.route('/api/typy-casu/<int:typ_id>/prilohy/<int:fid>', methods=['DELETE'])
def api_tc_priloha_delete(typ_id, fid):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT filepath FROM typy_casu_prilohy WHERE id=? AND typ_casu_id=?", (fid, typ_id))
    row = c.fetchone()
    if row:
        try:
            if os.path.exists(row['filepath']):
                os.remove(row['filepath'])
        except Exception:
            pass
        c.execute("DELETE FROM typy_casu_prilohy WHERE id=?", (fid,))
        conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/typy-casu/<int:typ_id>/dxf', methods=['GET'])
def api_dxf_get(typ_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT id, nazev_souboru, vrstvy_json, varovani_json, overrides_json, polygony_json, nahrano
        FROM typy_casu_dxf WHERE typ_casu_id=?
        ORDER BY id DESC LIMIT 1
    """, (typ_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return jsonify({'dxf': None})
    import json as _json
    return jsonify({
        'dxf': {
            'id':             row['id'],
            'nazev_souboru':  row['nazev_souboru'],
            'vrstvy':         _json.loads(row['vrstvy_json']    or '[]'),
            'varovani':       _json.loads(row['varovani_json']  or '[]'),
            'overrides':      _json.loads(row['overrides_json'] or '{}'),
            'polygony':       _json.loads(row['polygony_json']  or '{}'),
            'nahrano':        row['nahrano'],
        }
    })


@app.route('/api/typy-casu/<int:typ_id>/dxf', methods=['PATCH'])
def api_dxf_patch(typ_id):
    import json as _json
    data      = request.get_json() or {}
    overrides = data.get('overrides', {})
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE typy_casu_dxf SET overrides_json=? WHERE typ_casu_id=?",
              (_json.dumps(overrides, ensure_ascii=False), typ_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/typy-casu/<int:typ_id>/dxf', methods=['POST'])
def api_dxf_post(typ_id):
    import re as _re, math as _math, json as _json

    if 'dxf' not in request.files:
        return jsonify({'error': 'Žádný soubor'}), 400

    f = request.files['dxf']
    if not f.filename:
        return jsonify({'error': 'Prázdný název souboru'}), 400

    try:
        content = f.read().decode('utf-8', errors='replace')
    except Exception as e:
        return jsonify({'error': f'Nelze přečíst soubor: {e}'}), 400

    # ── DXF PARSER ───────────────────────────────────────────────────────────
    SNAP  = 5.0    # mm – tolerance pro propojení otevřených segmentů
    MIN_A = 500.0  # mm² – minimální plocha plochy (filtr šumu)

    def _shoelace(pts):
        n = len(pts)
        if n < 3:
            return 0.0
        s = sum(pts[i][0] * pts[(i+1) % n][1] - pts[(i+1) % n][0] * pts[i][1] for i in range(n))
        return abs(s) / 2.0

    def _dist(a, b):
        return _math.sqrt((a[0] - b[0])**2 + (a[1] - b[1])**2)

    def _pip(pt, poly):
        """Point in polygon – Ray casting algorithm."""
        x, y = pt
        n = len(poly)
        inside = False
        j = n - 1
        for i in range(n):
            xi, yi = poly[i]
            xj, yj = poly[j]
            if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-10) + xi):
                inside = not inside
            j = i
        return inside

    def _interior_point(poly):
        """Vrátí bod zaručeně uvnitř polygonu.

        Algoritmus:
        1. Zkus správný geometrický těžiště (vzorec přes plochu, ne průměr vrcholů).
           Průměr vrcholů leží mimo polygon u tvarů s nerovnoměrným rozložením vrcholů.
        2. Pokud těžiště leží mimo polygon → horizontální paprsky v několika výškách
           → střed prvního vnitřního úseku.
        3. Fallback: aritmetický průměr vrcholů.
        """
        n = len(poly)
        if n < 3:
            return (poly[0][0], poly[0][1])

        # Správný geometrický těžiště (area-weighted)
        A = 0.0
        cx = cy = 0.0
        for i in range(n):
            x0, y0 = poly[i]
            x1, y1 = poly[(i + 1) % n]
            cross = x0 * y1 - x1 * y0
            A += cross
            cx += (x0 + x1) * cross
            cy += (y0 + y1) * cross
        A *= 0.5
        if abs(A) > 1e-10:
            cx /= 6.0 * A
            cy /= 6.0 * A
        else:
            cx = sum(p[0] for p in poly) / n
            cy = sum(p[1] for p in poly) / n

        if _pip((cx, cy), poly):
            return (cx, cy)

        # Těžiště leží mimo polygon (silně konkávní tvar) → horizontální paprsek
        ys = [p[1] for p in poly]
        min_y, max_y = min(ys), max(ys)
        for frac in (0.5, 0.25, 0.75, 0.33, 0.67, 0.1, 0.9):
            test_y = min_y + (max_y - min_y) * frac
            xs_cross = []
            for i in range(n):
                x0, y0 = poly[i]
                x1, y1 = poly[(i + 1) % n]
                if (y0 < test_y <= y1) or (y1 < test_y <= y0):
                    t = (test_y - y0) / (y1 - y0 + 1e-15)
                    xs_cross.append(x0 + t * (x1 - x0))
            xs_cross.sort()
            for k in range(0, len(xs_cross) - 1, 2):
                mid_x = (xs_cross[k] + xs_cross[k + 1]) / 2.0
                if _pip((mid_x, test_y), poly):
                    return (mid_x, test_y)

        # Fallback
        return (cx, cy)

    def _chain(segments):
        """Spoj otevřené segmenty do uzavřených smyček."""
        segs = [{'pts': s, 'used': False} for s in segments]
        loops = []
        for start in segs:
            if start['used']:
                continue
            chain = list(start['pts'])
            start['used'] = True
            for _ in range(len(segs) * 2):
                tail = chain[-1]
                if len(chain) > 3 and _dist(tail, chain[0]) < SNAP:
                    loops.append(chain)
                    break
                found = False
                for seg in segs:
                    if seg['used']:
                        continue
                    pts = seg['pts']
                    if _dist(tail, pts[0]) < SNAP:
                        chain.extend(pts[1:])
                        seg['used'] = True
                        found = True
                        break
                    elif _dist(tail, pts[-1]) < SNAP:
                        chain.extend(list(reversed(pts[:-1])))
                        seg['used'] = True
                        found = True
                        break
                if not found:
                    break
        leftover = [s['pts'] for s in segs if not s['used']]
        return loops, leftover

    # Parsování entit
    layers   = {}  # layer_name -> {'closed': [...], 'open': [...]}
    warnings = []
    lines    = content.splitlines()
    idx      = 0

    def _next():
        nonlocal idx
        while idx + 1 < len(lines):
            cl = lines[idx].strip()
            vl = lines[idx + 1].strip()
            idx += 2
            try:
                return int(cl), vl
            except ValueError:
                continue
        return None, None

    # Přeskoč na sekci ENTITIES
    while idx < len(lines):
        c2, v2 = _next()
        if c2 == 0 and v2 == 'SECTION':
            c3, v3 = _next()
            if c3 == 2 and v3 == 'ENTITIES':
                break

    # Parsuj entity
    while idx < len(lines):
        code, val = _next()
        if code is None:
            break
        if code == 0 and val == 'ENDSEC':
            break
        if code not in (0,) or val not in ('LWPOLYLINE', 'CIRCLE', 'POLYLINE'):
            continue

        etype = val
        layer = None
        closed_flag = False
        pts = []
        radius = None

        if etype == 'POLYLINE':
            # Starý formát AC1009: POLYLINE + VERTEX + SEQEND
            # Nejprve načti hlavičku POLYLINE (layer, flag 70)
            while idx < len(lines):
                code, val = _next()
                if code is None:
                    break
                if code == 0:
                    idx -= 2
                    break
                if code == 8:
                    layer = val
                elif code == 70:
                    try:
                        closed_flag = bool(int(val) & 1)
                    except ValueError:
                        pass
            # Pak čti VERTEX entity až po SEQEND
            while idx < len(lines):
                code, val = _next()
                if code is None:
                    break
                if code == 0 and val == 'SEQEND':
                    break
                if code == 0 and val == 'VERTEX':
                    # Načti souřadnice tohoto vrcholu
                    vx = vy = None
                    while idx < len(lines):
                        code2, val2 = _next()
                        if code2 is None:
                            break
                        if code2 == 0:
                            idx -= 2
                            break
                        if code2 == 10:
                            try:
                                vx = float(val2)
                            except ValueError:
                                pass
                        elif code2 == 20:
                            try:
                                vy = float(val2)
                            except ValueError:
                                pass
                    if vx is not None and vy is not None:
                        pts.append([vx, vy])
        else:
            # Nový formát: LWPOLYLINE nebo CIRCLE
            while idx < len(lines):
                code, val = _next()
                if code is None:
                    break
                if code == 0:
                    idx -= 2
                    break
                if code == 8:
                    layer = val
                elif code == 70 and etype == 'LWPOLYLINE':
                    try:
                        closed_flag = bool(int(val) & 1)
                    except ValueError:
                        pass
                elif code == 40 and etype == 'CIRCLE':
                    try:
                        radius = float(val)
                    except ValueError:
                        pass
                elif code == 10:
                    try:
                        pts.append([float(val), None])
                    except ValueError:
                        pass
                elif code == 20:
                    try:
                        fv = float(val)
                        if pts and pts[-1][1] is None:
                            pts[-1][1] = fv
                        else:
                            pts.append([0.0, fv])
                    except ValueError:
                        pass

        if not layer:
            continue

        pts = [(p[0], p[1] if p[1] is not None else 0.0) for p in pts]

        ld = layers.setdefault(layer, {'closed': [], 'open': []})
        if etype == 'CIRCLE' and radius is not None:
            n = 32
            circle = [(_math.cos(2 * _math.pi * j / n) * radius,
                       _math.sin(2 * _math.pi * j / n) * radius) for j in range(n)]
            ld['closed'].append(circle)
        elif etype in ('LWPOLYLINE', 'POLYLINE') and len(pts) >= 2:
            eff_closed = _dist(pts[0], pts[-1]) < SNAP
            if closed_flag or eff_closed:
                ld['closed'].append(pts)
            else:
                ld['open'].append(pts)

    # Zpracuj každou vrstvu
    result_layers = []
    result_polygony = {}  # {layerName: [[pt, ...], ...]} — všechny valid polygony pro SVG náhled
    for lname, ldata in layers.items():
        chained, leftover = _chain(ldata['open'])
        if leftover:
            warnings.append(f'Vrstva „{lname}": {len(leftover)} neuzavřený segment(y) ignorován(y)')
        repaired = len(chained)

        all_polys = [(p, _shoelace(p)) for p in ldata['closed'] + chained]
        valid     = [(p, a) for p, a in all_polys if a >= MIN_A]
        if not valid:
            continue

        # Typ vrstvy z názvu — určit DÁL před nesting logikou
        lu = lname.upper()
        if lu.startswith('D ') or lu.startswith('D_') or lu.startswith('D\t'):
            typ = 'deska'
        elif lu.startswith('P ') or lu.startswith('P_') or lu.startswith('P\t'):
            typ = 'pena'
        else:
            typ = 'jine'

        # Nesting per-vrstva — určí hloubku každého polygonu.
        # Polygony na sudé hloubce (0, 2, …) jsou kusy; liché (1, 3, …) jsou díry/pockety.
        #
        # Klíčová oprava: místo průměru vrcholů (_centroid) používáme _interior_point,
        # který vrátí zaručeně vnitřní bod polygonu. Průměr vrcholů může ležet mimo polygon
        # u tvarů s nerovnoměrně rozmístěnými vrcholy → chybná klasifikace jako "uvnitř jiného".
        # Navíc: pokud nenajdeme bod uvnitř vlastního polygonu, tvar počítáme jako kus (buďme
        # konzervativní a raději kus připočítáme než vynecháme).
        valid.sort(key=lambda x: -x[1])
        pieces = []
        for poly, area in valid:
            pt = _interior_point(poly)
            if not _pip(pt, poly):
                # Nedaří se najít vnitřní bod → počítej jako kus (konzervativní přístup)
                pieces.append(area)
                continue
            # Klíč: polygony může obsahovat jen VĚTŠÍ polygon.
            # Malý otvor (např. otvor na šroub) nemůže "obsahovat" velký kus, přestože
            # geometrický těžiště kusu leží uvnitř otvoru (otvor je fyzicky v kusu).
            depth = sum(1 for op, oa in valid if op is not poly and oa > area and _pip(pt, op))
            if depth % 2 == 0:
                pieces.append(area)

        if not pieces:
            continue

        # Tloušťka z názvu vrstvy (např. "D 9mm Preklizka")
        m = _re.match(r'^[DP]\s+(\d+(?:[.,]\d+)?)mm', lname, _re.IGNORECASE)
        thickness = float(m.group(1).replace(',', '.')) if m else None

        result_layers.append({
            'nazev':         lname,
            'typ':           typ,
            'ks':            len(pieces),
            'plocha_m2':     round(sum(pieces) / 1_000_000, 4),
            'tloustka_mm':   thickness,
            'repaired':      repaired,
            # Vrstvy "jine" (gravíry, zahloubení) jsou v UI skryté — nesouvisí s materiálem
            'skryta':        typ == 'jine',
        })

        # Ulož všechny valid polygony pro SVG náhled (souřadnice zaokrouhleny na 1 desetinné místo)
        result_polygony[lname] = [
            [[round(x, 1), round(y, 1)] for x, y in poly]
            for poly, _ in valid
        ]

    typ_order = {'deska': 0, 'pena': 1, 'jine': 2}
    result_layers.sort(key=lambda x: (typ_order.get(x['typ'], 9), x['nazev']))

    # Ulož do DB
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM typy_casu_dxf WHERE typ_casu_id=?", (typ_id,))
    c.execute("""
        INSERT INTO typy_casu_dxf (typ_casu_id, nazev_souboru, vrstvy_json, varovani_json, polygony_json)
        VALUES (?, ?, ?, ?, ?)
    """, (typ_id, f.filename, _json.dumps(result_layers, ensure_ascii=False),
          _json.dumps(warnings, ensure_ascii=False),
          _json.dumps(result_polygony, ensure_ascii=False)))
    conn.commit()
    conn.close()

    return jsonify({
        'ok':       True,
        'vrstvy':   result_layers,
        'varovani': warnings,
        'polygony': result_polygony,
    })


# ── 3D MODELY (STL po vrstvách) ────────────────────────────────────────────────

STL_3D_DIR = os.path.join('/data', '3d_modely') if os.path.isdir('/data') else os.path.join(os.path.dirname(__file__), 'data', '3d_modely')


def _3d_stl_dir(typ_id, vid):
    """Adresář STL souborů pro danou verzi. Nové verze: <typ_id>/<vid>/. Legacy: <typ_id>/."""
    versioned = os.path.join(STL_3D_DIR, str(typ_id), str(vid))
    if os.path.isdir(versioned):
        return versioned
    # Zpětná kompatibilita — starší záznamy mají soubory přímo v <typ_id>/
    return os.path.join(STL_3D_DIR, str(typ_id))


@app.route('/api/typy-casu/<int:typ_id>/3d', methods=['GET'])
def api_3d_get(typ_id):
    import json as _json
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT id, nazev_souboru, vrstvy_json, nahrano, typ_sestavy
        FROM typy_casu_3d WHERE typ_casu_id=?
        ORDER BY id ASC
    """, (typ_id,))
    rows = c.fetchall()
    conn.close()
    versions = [{
        'id':            r['id'],
        'nazev_souboru': r['nazev_souboru'],
        'vrstvy':        _json.loads(r['vrstvy_json'] or '[]'),
        'nahrano':       r['nahrano'],
        'typ_sestavy':   r['typ_sestavy'] or 'sestava',
    } for r in rows]
    return jsonify({'versions': versions})


@app.route('/api/typy-casu/<int:typ_id>/3d/<int:vid>', methods=['PATCH'])
def api_3d_patch(typ_id, vid):
    """Uloží přiřazení typů vrstev nebo typ_sestavy pro konkrétní verzi."""
    import json as _json
    data = request.get_json(force=True) or {}

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, vrstvy_json FROM typy_casu_3d WHERE id=? AND typ_casu_id=?", (vid, typ_id))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Verze nenalezena'}), 404

    # Aktualizuj typ_sestavy
    if 'typ_sestavy' in data:
        c.execute("UPDATE typy_casu_3d SET typ_sestavy=? WHERE id=?", (data['typ_sestavy'], vid))

    # Aktualizuj typy vrstev
    updates = data.get('updates', [])
    if isinstance(updates, list) and updates:
        vrstvy = _json.loads(row['vrstvy_json'] or '[]')
        upd_map = {u['filename']: u['typ'] for u in updates if 'filename' in u and 'typ' in u}
        for v in vrstvy:
            if v.get('filename') in upd_map:
                v['typ'] = upd_map[v['filename']]
        c.execute("UPDATE typy_casu_3d SET vrstvy_json=? WHERE id=?",
                  (_json.dumps(vrstvy, ensure_ascii=False), vid))
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'vrstvy': vrstvy})

    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/typy-casu/<int:typ_id>/3d/<int:vid>', methods=['DELETE'])
def api_3d_delete(typ_id, vid):
    """Smaže verzi 3D modelu včetně STL souborů."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM typy_casu_3d WHERE id=? AND typ_casu_id=?", (vid, typ_id))
    if not c.fetchone():
        conn.close()
        return jsonify({'error': 'Verze nenalezena'}), 404
    c.execute("DELETE FROM typy_casu_3d WHERE id=?", (vid,))
    conn.commit()
    conn.close()
    # Smaž STL soubory (pouze verzovaný adresář, ne legacy)
    import shutil
    versioned_dir = os.path.join(STL_3D_DIR, str(typ_id), str(vid))
    if os.path.isdir(versioned_dir):
        shutil.rmtree(versioned_dir, ignore_errors=True)
    return jsonify({'ok': True})


@app.route('/api/typy-casu/<int:typ_id>/3d/<int:vid>/stl/<path:filename>', methods=['GET'])
def api_3d_stl_versioned(typ_id, vid, filename):
    """Vrátí STL soubor pro konkrétní verzi."""
    safe = os.path.basename(filename)
    if not safe.lower().endswith('.stl'):
        return jsonify({'error': 'Neplatný soubor'}), 400
    stl_path = os.path.join(_3d_stl_dir(typ_id, vid), safe)
    if not os.path.exists(stl_path):
        return jsonify({'error': 'Soubor nenalezen'}), 404
    return send_file(stl_path, mimetype='application/octet-stream',
                     as_attachment=False, download_name=safe)


@app.route('/api/typy-casu/<int:typ_id>/3d', methods=['POST'])
def api_3d_post(typ_id):
    """Přijme ZIP soubor se STL soubory, rozbalí, uloží jako novou verzi."""
    import zipfile, json as _json, re as _re

    if 'zip' not in request.files:
        return jsonify({'error': 'Žádný ZIP soubor'}), 400

    zf = request.files['zip']
    if not zf.filename.lower().endswith('.zip'):
        return jsonify({'error': 'Soubor musí být ZIP'}), 400

    # Extrahuj do temp adresáře
    import tempfile
    with tempfile.TemporaryDirectory() as tmp_dir:
        zip_path = os.path.join(tmp_dir, 'upload.zip')
        zf.save(zip_path)
        with zipfile.ZipFile(zip_path, 'r') as zzip:
            stl_names = [n for n in zzip.namelist()
                         if os.path.basename(n).lower().endswith('.stl')
                         and not os.path.basename(n).startswith('.')]
            if not stl_names:
                return jsonify({'error': 'ZIP neobsahuje žádné STL soubory'}), 400
            for stl_name in stl_names:
                zzip.extract(stl_name, tmp_dir)

        # ── pomocné funkce pro práci s binárním STL ──────────────────────────────
        import struct as _struct, statistics as _stats

        def _stl_read_triangles(path):
            tris = []
            with open(path, 'rb') as f:
                f.read(80)
                n = _struct.unpack('<I', f.read(4))[0]
                for _ in range(n):
                    data_b = f.read(50)
                    if len(data_b) == 50:
                        tris.append(bytearray(data_b))
            return tris

        def _tri_verts(tri):
            nx,ny,nz = _struct.unpack_from('<fff', tri, 0)
            v1 = _struct.unpack_from('<fff', tri, 12)
            v2 = _struct.unpack_from('<fff', tri, 24)
            v3 = _struct.unpack_from('<fff', tri, 36)
            return v1, v2, v3

        def _stl_write_triangles(path, tris):
            with open(path, 'wb') as f:
                f.write(b'\x00' * 80)
                f.write(_struct.pack('<I', len(tris)))
                for tri in tris:
                    f.write(bytes(tri))

        def _stl_shift_xyz(path, dx, dy, dz):
            tris = _stl_read_triangles(path)
            for tri in tris:
                for offset in (12, 24, 36):
                    x, y, z = _struct.unpack_from('<fff', tri, offset)
                    _struct.pack_into('<fff', tri, offset, x+dx, y+dy, z+dz)
            _stl_write_triangles(path, tris)

        def _stl_find_ref_box(path):
            """Najde referenční box 1×1×1 mm u (0,0,0) — vrátí (min_x,min_y,min_z) nebo None."""
            tris = _stl_read_triangles(path)
            box_verts = []
            for tri in tris:
                verts = _tri_verts(tri)
                if all(abs(c) < 20 for v in verts for c in v):
                    box_verts.extend(verts)
            if len(box_verts) < 3:
                return None
            xs = [v[0] for v in box_verts]
            ys = [v[1] for v in box_verts]
            zs = [v[2] for v in box_verts]
            if (max(xs)-min(xs) > 5 or max(ys)-min(ys) > 5 or max(zs)-min(zs) > 5):
                return None
            return (min(xs), min(ys), min(zs))

        # ── Zpracuj STL soubory ────────────────────────────────────────────────
        conn = get_db()
        c_db = conn.cursor()
        c_db.execute("SELECT kod, nazev, typ FROM materialy")
        mat_rows = c_db.fetchall()
        conn.close()
        mat_map = {r['kod'].lower().strip(): r for r in mat_rows if r['kod']}

        import unicodedata as _ud
        def _norm(s):
            return _ud.normalize('NFD', s.lower()).encode('ascii', 'ignore').decode()

        def _detect_typ(nazev):
            import re as _rer
            n = _norm(nazev)
            if _rer.match(r'^d[_\s]+(\d+)mm', n): return 'deska'
            if _rer.match(r'^p[_\s]+(\d+)mm', n): return 'pena'
            if 'nyty' in n or 'nyty' in n: return 'nyty'
            mat = mat_map.get(nazev.lower().strip())
            if mat:
                t = (mat['typ'] or '').upper()
                if t.startswith('HW'): return 'hw'
                if 'PROFIL' in t: return 'profily'
                if 'PENA' in t or 'PÉNA' in t: return 'pena'
                if 'DESKA' in t: return 'deska'
            return 'jine'

        # Najdi ref box ze 1. souboru
        ref_offset = None
        stl_paths = []
        for stl_name in stl_names:
            stl_base = os.path.basename(stl_name)
            stl_src  = os.path.join(tmp_dir, stl_name)
            stl_paths.append((stl_base, stl_src))

        for stl_base, stl_src in stl_paths:
            off = _stl_find_ref_box(stl_src)
            if off is not None:
                ref_offset = off
                break

        # Vytvoř dočasně ID=0 adresář, pak přejmenuje
        # Nejdřív vytvoř DB záznam, pak přesuň soubory
        conn = get_db()
        c_db = conn.cursor()
        c_db.execute("""
            INSERT INTO typy_casu_3d (typ_casu_id, nazev_souboru, vrstvy_json, typ_sestavy)
            VALUES (?, ?, '[]', 'sestava')
        """, (typ_id, zf.filename))
        new_vid = c_db.lastrowid
        conn.commit()
        conn.close()

        # Ulož STL soubory do verzovaného adresáře
        target_dir = os.path.join(STL_3D_DIR, str(typ_id), str(new_vid))
        os.makedirs(target_dir, exist_ok=True)

        import shutil as _shutil
        for stl_base, stl_src in stl_paths:
            dest = os.path.join(target_dir, stl_base)
            _shutil.copy2(stl_src, dest)

        # Zpracuj vrstvy + korekce
        vrstvy = []
        korektovano = []
        ref_box_name = None

        for stl_base, _ in stl_paths:
            stl_path = os.path.join(target_dir, stl_base)
            # Aplikuj ref box offset
            if ref_offset is not None:
                dx, dy, dz = -ref_offset[0], -ref_offset[1], -ref_offset[2]
                if abs(dx) > 0.001 or abs(dy) > 0.001 or abs(dz) > 0.001:
                    _stl_shift_xyz(stl_path, dx, dy, dz)

            # Detekuj ref box soubor (bude odebrán z výstupu)
            tris = _stl_read_triangles(stl_path)
            if len(tris) <= 12 and _stl_find_ref_box(stl_path) is not None:
                ref_box_name = stl_base
                continue

            nazev = stl_base[:-4].replace('_', ' ').replace(',', '.')
            typ   = _detect_typ(nazev)
            import re as _rer2
            th_m  = _rer2.match(r'^[dp][_\s]+(\d+(?:[.,]\d+)?)mm', _norm(nazev))
            tloustka = float(th_m.group(1).replace(',', '.')) if th_m else None

            vrstvy.append({
                'nazev':       nazev,
                'filename':    stl_base,
                'typ':         typ,
                'tloustka_mm': tloustka,
            })

        # Y-median fallback korekce (pouze pokud nebyl ref box)
        if ref_offset is None:
            y_meds = {}
            for v in vrstvy:
                tris = _stl_read_triangles(os.path.join(target_dir, v['filename']))
                if not tris: continue
                ys = [c for t in tris for vv in _tri_verts(t) for i, c in enumerate(vv) if i == 1]
                if ys: y_meds[v['filename']] = _stats.median(ys)
            if y_meds:
                meds = list(y_meds.values())
                global_med = _stats.median(meds)
                for fname, med in y_meds.items():
                    delta = global_med - med
                    if 0.5 < abs(delta) < 10:
                        _stl_shift_xyz(os.path.join(target_dir, fname), 0, delta, 0)
                        korektovano.append({'vrstva': fname, 'delta_mm': round(delta, 2)})

        # Ulož výsledné vrstvy
        conn = get_db()
        c_db = conn.cursor()
        c_db.execute("UPDATE typy_casu_3d SET vrstvy_json=? WHERE id=?",
                     (_json.dumps(vrstvy, ensure_ascii=False), new_vid))
        conn.commit()
        conn.close()

    resp = {'ok': True, 'vid': new_vid, 'vrstvy': vrstvy, 'pocet': len(vrstvy)}
    if korektovano:
        resp['korekce'] = korektovano
    return jsonify(resp)


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
    stav   = request.args.get('stav', '')
    search = request.args.get('q', '')
    limit  = int(request.args.get('limit', 100))
    dilna  = request.args.get('dilna', '')   # ?dilna=1 → jen zakázky odeslané do Dílny

    query = """
        SELECT z.*, t.nazev as case_nazev, t.typ_korpusu,
               CASE WHEN z.typ_casu_id IS NULL THEN NULL
                    WHEN (t.vnitrni_sirka  IS NOT NULL AND t.vnitrni_sirka  > 0 AND
                          t.vnitrni_vyska  IS NOT NULL AND t.vnitrni_vyska  > 0 AND
                          t.vnitrni_hloubka IS NOT NULL AND t.vnitrni_hloubka > 0) THEN 1
                    ELSE 0
               END as bom_spec_ok,
               CASE WHEN z.typ_casu_id IS NULL THEN NULL
                    WHEN EXISTS (
                        SELECT 1 FROM kusovniky k
                        JOIN materialy m ON m.kod = k.material_kod
                        WHERE k.typ_casu_id = z.typ_casu_id
                          AND m.typ NOT LIKE 'HW%' AND m.typ != 'PODVOZEK'
                    ) THEN 1
                    ELSE 0
               END as bom_mat_ok
        FROM zakazky z
        LEFT JOIN typy_casu t ON t.id = z.typ_casu_id
        WHERE 1=1
    """
    params = []
    if dilna:
        query += " AND z.odeslano_do_vyroby = 1"
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

    # Pro pohled Dílna: přidej skutečný stav CNC řezání (per kategorie)
    if dilna and items:
        zak_ids = [it['id'] for it in items]
        typ_ids = list({it['typ_casu_id'] for it in items if it.get('typ_casu_id')})

        # BOM typy materiálů pro každý typ casu
        bom_by_typ = {}
        if typ_ids:
            ph = ','.join('?' * len(typ_ids))
            c.execute(f"SELECT k.typ_casu_id, m.typ FROM kusovniky k "
                      f"JOIN materialy m ON m.kod=k.material_kod WHERE k.typ_casu_id IN ({ph})",
                      typ_ids)
            for row in c.fetchall():
                bom_by_typ.setdefault(row['typ_casu_id'], []).append(row['typ'] or '')

        # Zaškrtnuté kategorie z cnc_rezani
        cnc_by_zak = {}
        if zak_ids:
            ph = ','.join('?' * len(zak_ids))
            c.execute(f"SELECT zakazka_id, material_kod, rezano FROM cnc_rezani "
                      f"WHERE zakazka_id IN ({ph})", zak_ids)
            for row in c.fetchall():
                cnc_by_zak.setdefault(row['zakazka_id'], {})[row['material_kod']] = row['rezano']

        for it in items:
            typ_id = it.get('typ_casu_id')
            mats   = bom_by_typ.get(typ_id, []) if typ_id else []
            cnc    = cnc_by_zak.get(it['id'], {})
            has_d  = any(_cnc_je_deska(_cnc_norm_typ(t))    for t in mats)
            has_p  = any(_cnc_je_podvozek(_cnc_norm_typ(t)) for t in mats)
            has_n  = any(_cnc_je_pena(_cnc_norm_typ(t))     for t in mats)
            it['cnc_has_desky']    = has_d
            it['cnc_has_podvozky'] = has_p
            it['cnc_has_peny']     = has_n
            it['cnc_desky_ok']    = bool(cnc.get('_DESKY_',    0)) if has_d else None
            it['cnc_podvozky_ok'] = bool(cnc.get('_PODVOZKY_', 0)) if has_p else None
            it['cnc_peny_ok']     = bool(cnc.get('_PENY_',     0)) if has_n else None

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
            termin, zakaznik, poznamka_dilna, poznamka_cnc, pracovnik, sn_cislo, destinace)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (typ_id, hn_cislo, nazev, data.get('stav','Čeká'),
              data.get('pocet_ks',1), data.get('termin',''), data.get('zakaznik',''),
              data.get('poznamka_dilna',''), data.get('poznamka_cnc',''),
              data.get('pracovnik',''), data.get('sn_cislo',''),
              data.get('destinace','Zákazník')))
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
              'pracovnik','sn_cislo','faktura_cislo','faktura_datum','datum_dokonceni',
              'prioritni','foceni','odeslano_do_vyroby','destinace','poznamka_cnc_operator']
    updates = ', '.join(f"{f}=?" for f in fields if f in data)
    vals = [data[f] for f in fields if f in data]
    if 'stav' in data and data['stav'] == 'Hotovo' and 'datum_dokonceni' not in data:
        updates += ", datum_dokonceni=date('now')"
    if updates:
        c.execute(f"UPDATE zakazky SET {updates}, updated_at=datetime('now') WHERE id=?", vals + [zak_id])
        conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/zakazky/<int:zak_id>', methods=['DELETE'])
def api_zakazka_delete(zak_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT stav FROM zakazky WHERE id=?", (zak_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Zakázka nenalezena'}), 404
    POVOLENE_STAVY = {'Čeká', 'CNC hotovo'}
    if row['stav'] not in POVOLENE_STAVY:
        conn.close()
        return jsonify({'error': f'Zakázku ve stavu „{row["stav"]}" nelze smazat. Smazat lze pouze zakázky ve stavu Čeká nebo CNC hotovo.'}), 400
    c.execute("DELETE FROM zakazky WHERE id=?", (zak_id,))
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
                   COALESCE(s.naskladneno - s.pouzito, 0) as stav_skladu
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
                       COALESCE(s.naskladneno - s.pouzito, 0) as stav_skladu
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
    """Převede kancelářskou zakázku na výrobní zakázku (vždy 1 ks, HN z BOM)."""
    d = request.get_json() or {}
    typ_casu_id = d.get('typ_casu_id')
    if not typ_casu_id:
        return jsonify({'error': 'Vyberte typ casu ze seznamu — HN číslo musí pocházet z BOM.'}), 400
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM kancelar_zakazky WHERE id=?", (kid,))
    kz = c.fetchone()
    if not kz:
        conn.close()
        return jsonify({'error': 'Zakázka nenalezena'}), 404
    # Načti HN číslo z katalogu typů casů — ruční zadávání není povoleno
    c.execute("SELECT hn_cislo FROM typy_casu WHERE id=?", (typ_casu_id,))
    row_typ = c.fetchone()
    if not row_typ:
        conn.close()
        return jsonify({'error': 'Vybraný typ casu neexistuje.'}), 400
    hn_cislo = row_typ['hn_cislo']
    # Vždy vytvořit 1 zakázku (1 zakázka = 1 ks)
    c.execute("""
        INSERT INTO zakazky (nazev, zakaznik, hn_cislo, typ_casu_id, stav, pocet_ks, termin, poznamka_dilna)
        VALUES (?,?,?,?,?,?,?,?)
    """, (kz['nazev'], kz['zakaznik'] or '', hn_cislo,
          typ_casu_id, 'Čeká', 1, kz['termin'], kz['popis'] or ''))
    zak_id = c.lastrowid
    c.execute("UPDATE kancelar_zakazky SET vyrobni_zakazka_id=?, updated_at=? WHERE id=?",
              (zak_id, datetime.now().isoformat(), kid))
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

    # Blokace fakturace zakázek s otevřenými odchylkami
    zak_ids = [p.get('zakazka_id') for p in polozky_in if p.get('zakazka_id')]
    if zak_ids:
        placeholders = ','.join('?' * len(zak_ids))
        c.execute(f"""
            SELECT z.hn_cislo
            FROM odchylky_karty o
            JOIN zakazky z ON z.id = o.zakazka_id
            WHERE o.zakazka_id IN ({placeholders}) AND o.stav = 'Nová'
            GROUP BY o.zakazka_id
        """, zak_ids)
        blokujici = c.fetchall()
        if blokujici:
            cisla = ', '.join(r['hn_cislo'] or str(r[0]) for r in blokujici)
            conn.close()
            return jsonify({'error': f'Nelze vyfakturovat – zakázky mají otevřené odchylky: {cisla}. Nejprve odchylky uzavři.'}), 400

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

    # Odešli email s PDF v pozadí (neblokuje odpověď API)
    try:
        pdf_bytes = vygeneruj_pdf(fak, polozky_out)
        t = threading.Thread(target=send_invoice_email, args=(fak_id, pdf_bytes, fak['cislo']), daemon=True)
        t.start()
    except Exception as e:
        print(f"[Email] Nepodařilo se připravit PDF pro email: {e}")

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
               round((t.cena_dilu + t.cena_vyroby) * 1.047 * z.pocet_ks * 1.21, 2) as celkem_s_dph,
               (SELECT COUNT(*) FROM odchylky_karty o
                WHERE o.zakazka_id = z.id AND o.stav = 'Nová') as ma_odchylku
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

@app.route('/api/material-spojeniky/<int:spoj_id>', methods=['PUT'])
def api_material_spojeniky_update(spoj_id):
    data = request.json
    mnozstvi = float(data.get('mnozstvi_na_kus', 1))
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE material_spojeniky SET mnozstvi_na_kus=? WHERE id=?", (mnozstvi, spoj_id))
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
    """Výpočet odhadovaného výrobního času – workflow montáže krok po kroku.

    Kroky (zatím implementované):
      1. Orientace  – 1 min (≤ mez desek) nebo 3 min (> mez desek)
      2. Přinesení profilů – 2 min fixní (jen pokud existuje plán profilů)
      3. Řezání profilů:
         - FUSION/R1 (kód Q* nebo NE*): Σ ks × cas_fusion_ks_s
         - Standardní: unikátní rozměry L × cas_L_uniq_s + unikátní H × cas_H_uniq_s
         - Motýlové zámky: Σ ks × cas_motyl_zkos_s (sražení hybridů)

    Další kroky budou doplňovány postupně.
    """
    conn = get_db()
    c = conn.cursor()

    # Načti typ case + rozměry
    c.execute("SELECT * FROM typy_casu WHERE id=?", (typ_id,))
    _row = c.fetchone()
    if not _row:
        conn.close()
        return jsonify({'error': 'Typ nenalezen'}), 404
    typ = dict(_row)  # dict umožňuje .get() s defaultem (sqlite3.Row .get() nepodporuje)

    # Načti parametry
    c.execute("SELECT sekce, klic, hodnota FROM cas_parametry")
    par = {}
    for r in c.fetchall():
        par.setdefault(r['sekce'], {})[r['klic']] = r['hodnota']

    def p(sekce, klic, default=0):
        return par.get(sekce, {}).get(klic, default)

    # Načti BOM
    c.execute("""
        SELECT k.material_kod, k.mnozstvi, m.nazev, m.typ,
               COALESCE(m.nity, 0) as nity,
               COALESCE(m.druh, '') as druh,
               COALESCE(m.cas_s, 0) as cas_s
        FROM kusovniky k
        JOIN materialy m ON m.kod = k.material_kod
        WHERE k.typ_casu_id = ?
    """, (typ_id,))
    bom = db_rows_to_list(c.fetchall())

    # Načti plán profilů
    c.execute("""
        SELECT typ_profilu, poradi, ks, rozmer_mm
        FROM profily_plan WHERE typ_casu_id=? AND ks > 0
        ORDER BY typ_profilu, poradi
    """, (typ_id,))
    profily_rows = db_rows_to_list(c.fetchall())

    # Načti DXF data (pro výpočet můstků)
    c.execute("""
        SELECT vrstvy_json, overrides_json
        FROM typy_casu_dxf WHERE typ_casu_id=?
    """, (typ_id,))
    dxf_row = c.fetchone()

    conn.close()

    import math  # potřeba pro math.ceil v řezání hybridů

    # ── Pomocné funkce ────────────────────────────────────────────────────────
    def norm(t):
        """Normalizuje string na ASCII uppercase pro porovnání."""
        if not t: return ''
        t = t.upper()
        for f, r in [('Á','A'),('Č','C'),('Ď','D'),('É','E'),('Ě','E'),
                     ('Í','I'),('Ň','N'),('Ó','O'),('Ř','R'),('Š','S'),
                     ('Ť','T'),('Ú','U'),('Ů','U'),('Ý','Y'),('Ž','Z')]:
            t = t.replace(f, r)
        return t

    def is_deska(pol):
        nt = norm(pol.get('typ') or '')
        return 'DESKA' in nt or 'PREKLIZK' in nt or 'PLAYWOOD' in nt

    def is_fusion_profil(pol):
        """FUSION/R1 profil: kód začíná Q nebo NE a typ obsahuje PROFIL."""
        kod = pol.get('material_kod') or ''
        nt  = norm(pol.get('typ') or '')
        return (kod.startswith('Q') or kod.startswith('NE')) and 'PROFIL' in nt

    def is_motylovy_zamek(pol):
        """Motýlový zámek – detekce podle názvu."""
        return 'MOTYL' in norm(pol.get('nazev') or '')

    def is_otocne_kolecko(pol):
        """Otočné kolečko vyžadující natírání: typ=PODVOZEK, druh=OTOČNÉ."""
        return norm(pol.get('typ') or '') == 'PODVOZEK' and 'OTOCN' in norm(pol.get('druh') or '')

    def fmt_hm(s):
        s = int(round(s))
        if s < 60:
            return f"{s}s"
        h = s // 3600
        m = (s % 3600) // 60
        return f"{h}h {m:02d}min" if h else f"{m}min"

    kroky = []

    # ════════════════════════════════════════════════════════════════════════
    # KROK 1 – Orientace v dokumentaci
    # ════════════════════════════════════════════════════════════════════════
    pocet_desek   = int(sum(pol['mnozstvi'] for pol in bom if is_deska(pol)))
    mez_desek     = int(p('Priprava', 'orientace_mez_desek',     10))
    cas_jednod    = p('Priprava', 'orientace_jednoducha_s',      60)
    cas_slozity   = p('Priprava', 'orientace_slozita_s',        180)
    je_slozity    = pocet_desek > mez_desek
    orientace_s   = cas_slozity if je_slozity else cas_jednod

    kroky.append({
        'id':     'orientace',
        'label':  'Orientace v dokumentaci',
        'cas_s':  round(orientace_s),
        'cas_fmt': fmt_hm(orientace_s),
        'detail': {
            'pocet_desek': pocet_desek,
            'mez_desek':   mez_desek,
            'je_slozity':  je_slozity,
        },
    })

    # ════════════════════════════════════════════════════════════════════════
    # KROK 2 – Přinesení profilů
    # ════════════════════════════════════════════════════════════════════════
    ma_profily  = bool(profily_rows)
    noseni_s    = p('Priprava', 'noseni_profilu_s', 120) if ma_profily else 0

    kroky.append({
        'id':     'noseni_profilu',
        'label':  'Přinesení profilů',
        'cas_s':  round(noseni_s),
        'cas_fmt': fmt_hm(noseni_s),
        'detail': {
            'ma_profily': ma_profily,
        },
    })

    # ════════════════════════════════════════════════════════════════════════
    # KROK 3 – Řezání profilů
    # ════════════════════════════════════════════════════════════════════════
    # Detekce: je v BOM jakýkoliv FUSION/R1 profil (Q* nebo NE*)?
    je_fusion      = any(is_fusion_profil(pol) for pol in bom)

    rezani_s       = 0
    rezani_detail  = {'je_fusion': je_fusion}

    if je_fusion:
        # FUSION/R1: každý kus = 1 kolmý řez, čas cas_fusion_ks_s
        cas_fus   = p('Rezani', 'cas_fusion_ks_s', 25)
        fus_kusy  = int(sum(pr['ks'] for pr in profily_rows))
        rezani_s  = fus_kusy * cas_fus
        rezani_detail.update({
            'fusion_kusy':    fus_kusy,
            'cas_fusion_ks_s': cas_fus,
        })
    else:
        # Standardní: L profily seskupené 4× naráz → každý unikátní rozměr = 1 řez
        # H profily (hybridy) → každý unikátní rozměr = 1 řez (jiný čas)
        L_rozm = {}
        H_rozm = {}
        for pr in profily_rows:
            rozm = pr.get('rozmer_mm') or 0
            if pr['typ_profilu'] == 'L':
                L_rozm[rozm] = L_rozm.get(rozm, 0) + pr['ks']
            else:
                H_rozm[rozm] = H_rozm.get(rozm, 0) + pr['ks']

        cas_L      = p('Rezani', 'cas_L_uniq_s', 60)
        cas_H_par  = p('Rezani', 'cas_H_par_s',  60)
        L_uniq     = len(L_rozm)
        H_kusy_rez = int(sum(H_rozm.values()))
        H_paru     = math.ceil(H_kusy_rez / 2) if H_kusy_rez > 0 else 0
        rezani_s   = L_uniq * cas_L + H_paru * cas_H_par
        rezani_detail.update({
            'L_uniq':       L_uniq,
            'H_kusy':       H_kusy_rez,
            'H_paru':       H_paru,
            'L_rozmery':    sorted(L_rozm.keys()),
            'H_rozmery':    sorted(H_rozm.keys()),
            'cas_L_uniq_s': cas_L,
            'cas_H_par_s':  cas_H_par,
        })

    # Motýlové zámky → sražení hybridů na pile (ke každému zámku +40 s)
    motyl_kusy = int(sum(pol['mnozstvi'] for pol in bom if is_motylovy_zamek(pol)))
    if motyl_kusy > 0:
        cas_motyl  = p('Rezani', 'cas_motyl_zkos_s', 40)
        rezani_s  += motyl_kusy * cas_motyl
        rezani_detail.update({
            'motyl_kusy':      motyl_kusy,
            'cas_motyl_zkos_s': cas_motyl,
        })
    else:
        rezani_detail['motyl_kusy'] = 0

    # FUSION Q6504 + motýlové zámky → vyřezání zámků do profilů na pile
    je_q6504 = any((pol.get('material_kod') or '').upper() == 'Q6504' for pol in bom)
    if je_q6504 and motyl_kusy > 0:
        cas_q6504_motyl = p('Rezani', 'cas_fusion_q6504_motyl_s', 240)
        rezani_s += motyl_kusy * cas_q6504_motyl
        rezani_detail.update({
            'q6504_motyl':              True,
            'cas_fusion_q6504_motyl_s': cas_q6504_motyl,
        })
    else:
        rezani_detail['q6504_motyl'] = False

    kroky.append({
        'id':     'rezani_profilu',
        'label':  'Řezání profilů',
        'cas_s':  round(rezani_s) if ma_profily else 0,
        'cas_fmt': fmt_hm(rezani_s) if ma_profily else '0min',
        'detail': rezani_detail,
    })

    # ════════════════════════════════════════════════════════════════════════
    # KROK 4 – Děrování L profilů
    # Přeskočí se pokud: je FUSION/R1 nebo žádné profily v plánu.
    # Děruje se každý L profil zvlášť, jen s délkou > min_delka_mm.
    # Každý unikátní rozměr = přenastavení pravítka (cas_nastaveni_pravitka_s)
    # Každý kus = cas_na_profil_s
    # ════════════════════════════════════════════════════════════════════════
    derovani_s      = 0
    derovani_detail = {'je_fusion': je_fusion, 'ma_profily': ma_profily}

    if ma_profily and not je_fusion:
        min_delka    = p('Derovani', 'min_delka_mm',              128)
        cas_pravitko = p('Derovani', 'cas_nastaveni_pravitka_s',   10)
        cas_kus      = p('Derovani', 'cas_na_profil_s',            15)

        # Jen L profily delší než min_delka_mm
        D_rozm = {}
        for pr in profily_rows:
            if pr['typ_profilu'] != 'L':
                continue
            rozm = pr.get('rozmer_mm') or 0
            if rozm <= min_delka:
                continue
            D_rozm[rozm] = D_rozm.get(rozm, 0) + pr['ks']

        D_uniq     = len(D_rozm)
        D_kusy     = int(sum(D_rozm.values()))
        derovani_s = D_uniq * cas_pravitko + D_kusy * cas_kus

        derovani_detail.update({
            'D_uniq':                   D_uniq,
            'D_kusy':                   D_kusy,
            'min_delka_mm':             int(min_delka),
            'cas_nastaveni_pravitka_s': int(cas_pravitko),
            'cas_na_profil_s':          int(cas_kus),
            'D_rozmery':                sorted(D_rozm.keys()),
        })
    elif je_fusion:
        derovani_detail['duvod_skip'] = 'fusion'
    else:
        derovani_detail['duvod_skip'] = 'bez_profilu'

    kroky.append({
        'id':      'derovani',
        'label':   'Děrování L profilů',
        'cas_s':   round(derovani_s),
        'cas_fmt': fmt_hm(derovani_s),
        'detail':  derovani_detail,
    })

    # ════════════════════════════════════════════════════════════════════════
    # KROK 5 – Broušení hybrid hliníků
    # Podmínka: case má pěny v BOM (typ = PĚNA) + není FUSION/R1
    # Čas: počet ks H profilů (z plánu profilů) × cas_na_hybrid_s
    # ════════════════════════════════════════════════════════════════════════
    ma_peny   = any('PENA' in norm(pol.get('typ') or '') for pol in bom)
    H_kusy    = int(sum(r['ks'] for r in profily_rows if r['typ_profilu'] == 'H'))
    cas_hybrid = p('BrouseniHybrid', 'cas_na_hybrid_s', 10)

    if ma_peny and not je_fusion and H_kusy > 0:
        brouseni_s = H_kusy * cas_hybrid
        brouseni_detail = {
            'ma_peny':          True,
            'je_fusion':        je_fusion,
            'H_kusy':           H_kusy,
            'cas_na_hybrid_s':  int(cas_hybrid),
        }
    else:
        brouseni_s = 0
        brouseni_detail = {
            'ma_peny':   ma_peny,
            'je_fusion': je_fusion,
            'H_kusy':    H_kusy,
            'duvod_skip': (
                'fusion'    if je_fusion else
                'bez_pen'   if not ma_peny else
                'bez_H_profilu'
            ),
        }

    kroky.append({
        'id':      'brouseni_hybrid',
        'label':   'Broušení hybridů',
        'cas_s':   round(brouseni_s),
        'cas_fmt': fmt_hm(brouseni_s),
        'detail':  brouseni_detail,
    })

    # ════════════════════════════════════════════════════════════════════════
    # KROK 6 – Natírání otočných podvozků (kolečka typ=PODVOZEK, druh=OTOČNÉ)
    # ════════════════════════════════════════════════════════════════════════
    cas_kolo      = p('Podvozky', 'cas_na_kolo_s', 120)
    kolecka_kusy  = int(sum(pol['mnozstvi'] for pol in bom if is_otocne_kolecko(pol)))
    natireni_s    = kolecka_kusy * cas_kolo

    kroky.append({
        'id':      'natireni_podvozku',
        'label':   'Natírání podvozků',
        'cas_s':   round(natireni_s),
        'cas_fmt': fmt_hm(natireni_s),
        'detail': {
            'kolecka_kusy':  kolecka_kusy,
            'cas_na_kolo_s': int(cas_kolo),
        },
    })

    # ════════════════════════════════════════════════════════════════════════
    # KROK 6 – Můstky (odlamování odpadků po CNC řezání desek)
    # Primární zdroj: DXF import – počet desek s tloušťkou ≤ max_tloustka_mm
    # Záloha: BOM – pokud case obsahuje desky ≤ max_tloustka_mm → flat rate
    # ════════════════════════════════════════════════════════════════════════
    max_tl       = p('Mustky', 'max_tloustka_mm',   10)
    cas_deska    = p('Mustky', 'cas_per_deska_s',   20)
    cas_fallback = p('Mustky', 'cas_bom_fallback_s', 120)

    mustky_s      = 0
    mustky_detail = {}

    if dxf_row and dxf_row['vrstvy_json']:
        # ── Primární: z DXF ──────────────────────────────────────────────
        vrstvy    = json.loads(dxf_row['vrstvy_json'])
        overrides = json.loads(dxf_row['overrides_json'] or '{}')

        def dxf_eff(v):
            """Vrátí efektivní {typ, tloustka_mm} s ohledem na override."""
            ov = overrides.get(v['nazev'], 'auto')
            if ov == 'ignore':
                return None
            if ov.startswith('deska:'):
                return {'typ': 'deska', 'tloustka_mm': float(ov.split(':')[1])}
            if ov.startswith('pena:'):
                return {'typ': 'pena', 'tloustka_mm': float(ov.split(':')[1])}
            return {'typ': v.get('typ', 'jine'), 'tloustka_mm': v.get('tloustka_mm')}

        dxf_pocet = 0
        for v in vrstvy:
            eff = dxf_eff(v)
            if not eff or eff['typ'] != 'deska':
                continue
            tl = eff['tloustka_mm']
            if tl is not None and tl <= max_tl:
                dxf_pocet += int(v.get('ks', 0))

        mustky_s = dxf_pocet * cas_deska
        mustky_detail = {
            'zdroj':          'dxf',
            'pocet_desek':    dxf_pocet,
            'max_tloustka_mm': int(max_tl),
            'cas_per_deska_s': int(cas_deska),
        }
    else:
        # ── Záloha: z BOM ────────────────────────────────────────────────
        def tloustka_z_nazvu(nazev):
            m = re.search(r'(\d+[,.]?\d*)\s*mm', nazev or '')
            return float(m.group(1).replace(',', '.')) if m else None

        ma_tenkou_desku = any(
            norm(pol.get('typ') or '') in ('DESKA', 'PREKLIZKA', 'PLAYWOOD')
            and (tloustka_z_nazvu(pol.get('nazev')) or 999) <= max_tl
            for pol in bom
        )
        mustky_s = cas_fallback if ma_tenkou_desku else 0
        mustky_detail = {
            'zdroj':              'bom',
            'ma_tenkou_desku':    ma_tenkou_desku,
            'max_tloustka_mm':    int(max_tl),
            'cas_bom_fallback_s': int(cas_fallback),
        }

    kroky.append({
        'id':      'mustky',
        'label':   'Můstky',
        'cas_s':   round(mustky_s),
        'cas_fmt': fmt_hm(mustky_s),
        'detail':  mustky_detail,
    })

    # ════════════════════════════════════════════════════════════════════════
    # KROK 7 – Sestřílení (základní kompletace desek pistolí)
    # Přeskočí se pro: Jiný typ, Inlay, Akustický panel *, R1 system
    # Čas = Σ ks_hlavniho_materialu × (cas_base_s + cas_per_m2_s × plocha_m2/ks)
    # Hlavní materiál = deska ≤ 10 mm s nejvíce kusy v DXF
    # Záloha bez DXF: největší plocha odhadnuta z rozměrů casu
    # ════════════════════════════════════════════════════════════════════════
    SESTRILENI_VYLOUCENE = {
        norm('Jiný typ'), norm('Inlay'),
        norm('Akustický panel 60x60 v rámu'),
        norm('Akustický panel 120x60 bez rámu'),
        norm('R1 system'),
    }
    typ_korpusu_norm = norm(typ['typ_korpusu'] or '')
    je_klasicky_case  = typ_korpusu_norm not in SESTRILENI_VYLOUCENE

    cas_base   = p('Sestrileni', 'cas_base_s',    5)
    cas_per_m2 = p('Sestrileni', 'cas_per_m2_s', 40)

    sestril_s      = 0
    sestril_detail = {'je_klasicky_case': je_klasicky_case}

    if je_klasicky_case:
        max_tl_dest = p('Mustky', 'max_tloustka_mm', 10)  # stejná mez jako můstky

        if dxf_row and dxf_row['vrstvy_json']:
            vrstvy    = json.loads(dxf_row['vrstvy_json'])
            overrides = json.loads(dxf_row['overrides_json'] or '{}')

            # Najdi hlavní materiál: deska ≤ max_tl_dest s nejvíce kusy
            kandidati = []
            for v in vrstvy:
                ov = overrides.get(v['nazev'], 'auto')
                if ov == 'ignore':
                    continue
                if ov.startswith('deska:'):
                    tl = float(ov.split(':')[1])
                    vtyp = 'deska'
                elif ov.startswith('pena:'):
                    continue
                else:
                    tl   = v.get('tloustka_mm')
                    vtyp = v.get('typ', 'jine')
                if vtyp != 'deska' or tl is None or tl > max_tl_dest:
                    continue
                kandidati.append({
                    'nazev':    v['nazev'],
                    'ks':       int(v.get('ks', 0)),
                    'plocha_m2': v.get('plocha_m2', 0),
                    'tloustka_mm': tl,
                })

            if kandidati:
                hlavni = max(kandidati, key=lambda x: x['ks'])
                plocha_per_ks = (hlavni['plocha_m2'] / hlavni['ks']) if hlavni['ks'] > 0 else 0
                cas_per_desku = cas_base + cas_per_m2 * plocha_per_ks
                sestril_s = hlavni['ks'] * cas_per_desku
                sestril_detail.update({
                    'zdroj':         'dxf',
                    'hlavni_nazev':  hlavni['nazev'],
                    'hlavni_ks':     hlavni['ks'],
                    'plocha_per_ks_m2': round(plocha_per_ks, 3),
                    'cas_per_desku_s':  round(cas_per_desku, 1),
                    'cas_base_s':    int(cas_base),
                    'cas_per_m2_s':  int(cas_per_m2),
                })
            else:
                sestril_detail['zdroj'] = 'dxf_bez_desek'

        else:
            # Záloha: odhadni plochu z rozměrů casu (největší stěna)
            s = (typ['vnitrni_sirka'] or 0) / 1000   # mm → m
            v_dim = (typ['vnitrni_vyska'] or 0) / 1000
            h = (typ['vnitrni_hloubka'] or 0) / 1000
            # Největší strana (přibližná plocha nejpočetnější desky)
            plocha_odhad = max(s * h, s * v_dim, h * v_dim) if s and h else 0

            # Počet desek z BOM
            pocet_bom_desek = int(sum(
                pol['mnozstvi'] for pol in bom
                if norm(pol.get('typ') or '') in ('DESKA', 'PREKLIZKA', 'PLAYWOOD')
            ))
            if pocet_bom_desek > 0 and plocha_odhad > 0:
                cas_per_desku = cas_base + cas_per_m2 * plocha_odhad
                sestril_s = pocet_bom_desek * cas_per_desku
                sestril_detail.update({
                    'zdroj':           'bom_odhad',
                    'pocet_bom_desek': pocet_bom_desek,
                    'plocha_odhad_m2': round(plocha_odhad, 3),
                    'cas_per_desku_s': round(cas_per_desku, 1),
                    'cas_base_s':      int(cas_base),
                    'cas_per_m2_s':    int(cas_per_m2),
                })
            else:
                sestril_detail['zdroj'] = 'nelze_odhadnout'
    else:
        sestril_detail['duvod_skip'] = typ['typ_korpusu'] or 'neznámý typ'

    kroky.append({
        'id':      'sestrileni',
        'label':   'Sestřílení',
        'cas_s':   round(sestril_s),
        'cas_fmt': fmt_hm(sestril_s),
        'detail':  sestril_detail,
    })

    # ════════════════════════════════════════════════════════════════════════
    # KROK 8 – Sesbírání HW materiálu ze skladu
    # Počítají se všechny BOM položky KROMĚ: DESKA, PĚNA, PROFIL AL, OSTATNÍ
    # • počet různých druhů × cas_per_druh_s   (10 s/druh)
    # • celkové ks        × cas_per_ks_s       (2 s/ks)
    # ════════════════════════════════════════════════════════════════════════
    HW_VYLOUCENE_TYPY = {'DESKA', 'PĚNA', 'PROFIL AL', 'OSTATNÍ'}

    cas_druh = p('SbiraniHW', 'cas_per_druh_s', 10)
    cas_ks   = p('SbiraniHW', 'cas_per_ks_s',    2)

    hw_polozky = [
        pol for pol in bom
        if norm(pol.get('typ') or '') not in {norm(t) for t in HW_VYLOUCENE_TYPY}
    ]
    hw_druhu   = len(hw_polozky)                            # počet různých řádků BOM
    hw_kusu    = int(sum(pol['mnozstvi'] for pol in hw_polozky))  # celkem ks
    sbir_s     = hw_druhu * cas_druh + hw_kusu * cas_ks

    kroky.append({
        'id':      'sbir_hw',
        'label':   'Sesbírání HW materiálu',
        'cas_s':   round(sbir_s),
        'cas_fmt': fmt_hm(sbir_s),
        'detail': {
            'hw_druhu':        hw_druhu,
            'hw_kusu':         hw_kusu,
            'cas_per_druh_s':  int(cas_druh),
            'cas_per_ks_s':    int(cas_ks),
            'polozky':         [{'typ': p['typ'], 'nazev': p['nazev'], 'ks': int(p['mnozstvi'])}
                                for p in hw_polozky],
        },
    })

    # ════════════════════════════════════════════════════════════════════════
    # KROK 9 – Kompletace case
    # Větve: panel (paušál) → R1 → FUSION → Standard
    # HW čas (ks × cas_s) se přičítá u všech variant kromě panelů
    # ════════════════════════════════════════════════════════════════════════

    # ── Pomocná funkce: HW čas z BOM (ks × cas_s, vše kromě desek/pěn/profilů) ──
    HW_VYLOUCENE_NORM = {norm(t) for t in ('DESKA', 'PĚNA', 'PROFIL AL', 'OSTATNÍ')}

    def hw_cas_z_bom():
        """Vrátí (celkem_s, detail_list) z BOM položek (typ × cas_s)."""
        total = 0
        items = []
        for pol in bom:
            if norm(pol.get('typ') or '') in HW_VYLOUCENE_NORM:
                continue
            cs = float(pol.get('cas_s') or 0)
            if cs <= 0:
                continue
            ks  = int(pol['mnozstvi'])
            sub = ks * cs
            total += sub
            items.append({'nazev': pol['nazev'], 'typ': pol['typ'], 'ks': ks,
                          'cas_s': cs, 'sub_s': sub})
        return total, items

    # ── Parametry ──────────────────────────────────────────────────────────
    std_roztes  = p('Kompletace', 'std_nit_roztes_mm',  64)
    std_nyt_s   = p('Kompletace', 'std_nitovani_s',      9)
    std_L_min   = p('Kompletace', 'std_L_min_mm',      128)
    std_L_us    = p('Kompletace', 'std_L_usazeni_s',    15)
    std_H_us    = p('Kompletace', 'std_H_usazeni_s',    10)
    fus_s       = p('Kompletace', 'fus_profil_s',      180)
    fus_100mm   = p('Kompletace', 'fus_per_100mm_s',     3)
    r1_hl_s     = p('Kompletace', 'r1_hliniky_s',      600)
    pan6060_s   = p('Kompletace', 'panel_6060_s',     3600)
    pan12060_s  = p('Kompletace', 'panel_12060_s',    2700)

    komp_s      = 0
    komp_detail = {}

    # ── Detekce větve ──────────────────────────────────────────────────────
    je_r1 = (
        norm(typ['typ_korpusu'] or '') == norm('R1 system') or
        any((pol.get('material_kod') or '').startswith('NE') and
            'PROFIL' in norm(pol.get('typ') or '') for pol in bom)
    )

    if typ_korpusu_norm == norm('Akustický panel 60x60 v rámu'):
        komp_s = pan6060_s
        komp_detail = {'vetev': 'panel_6060', 'cas_s': int(pan6060_s)}

    elif typ_korpusu_norm == norm('Akustický panel 120x60 bez rámu'):
        komp_s = pan12060_s
        komp_detail = {'vetev': 'panel_12060', 'cas_s': int(pan12060_s)}

    elif je_r1:
        hw_total, hw_items = hw_cas_z_bom()
        komp_s = r1_hl_s + hw_total
        komp_detail = {
            'vetev':       'r1',
            'r1_hliniky_s': int(r1_hl_s),
            'hw_total_s':  round(hw_total),
            'hw_polozky':  hw_items,
        }

    elif je_fusion:
        # FUSION: každý kus profilu z profily_plan = 180s + délka/100*3s
        fus_profily_s = 0
        fus_profily_detail = []
        for pr in profily_rows:
            rozm = pr.get('rozmer_mm') or 0
            ks   = pr['ks']
            cas_kus = fus_s + (rozm / 100.0) * fus_100mm
            sub  = ks * cas_kus
            fus_profily_s += sub
            fus_profily_detail.append({
                'rozmer_mm': rozm, 'ks': ks,
                'cas_kus_s': round(cas_kus, 1), 'sub_s': round(sub, 1),
            })

        hw_total, hw_items = hw_cas_z_bom()
        komp_s = fus_profily_s + hw_total
        komp_detail = {
            'vetev':          'fusion',
            'fus_profily_s':  round(fus_profily_s),
            'fus_polozky':    fus_profily_detail,
            'fus_profil_s':   int(fus_s),
            'fus_per_100mm_s': int(fus_100mm),
            'hw_total_s':     round(hw_total),
            'hw_polozky':     hw_items,
        }

    else:
        # Standard: L nýtování + L usazení + H usazení + HW
        nyt_s     = 0
        L_us_s    = 0
        H_us_s    = 0
        nyt_detail = []
        L_us_detail = []
        H_us_detail = []

        for pr in profily_rows:
            rozm = pr.get('rozmer_mm') or 0
            ks   = pr['ks']
            if pr['typ_profilu'] == 'L':
                # Usazení: všechny L profily
                sub_us = ks * std_L_us
                L_us_s += sub_us
                # Nýtování: jen L profily > std_L_min
                if rozm > std_L_min:
                    nity_ks = int(rozm // std_roztes)
                    sub_nyt = ks * nity_ks * std_nyt_s
                    nyt_s  += sub_nyt
                    nyt_detail.append({
                        'rozmer_mm': rozm, 'ks': ks,
                        'nity_ks': nity_ks, 'sub_s': round(sub_nyt),
                    })
                L_us_detail.append({'rozmer_mm': rozm, 'ks': ks, 'sub_s': round(sub_us)})
            else:  # H profil
                sub_h = ks * std_H_us
                H_us_s += sub_h
                H_us_detail.append({'rozmer_mm': rozm, 'ks': ks, 'sub_s': round(sub_h)})

        hw_total, hw_items = hw_cas_z_bom()
        komp_s = nyt_s + L_us_s + H_us_s + hw_total
        komp_detail = {
            'vetev':        'standard',
            'nyt_s':        round(nyt_s),
            'L_us_s':       round(L_us_s),
            'H_us_s':       round(H_us_s),
            'hw_total_s':   round(hw_total),
            'nyt_detail':   nyt_detail,
            'L_us_detail':  L_us_detail,
            'H_us_detail':  H_us_detail,
            'hw_polozky':   hw_items,
            'std_nit_roztes_mm': int(std_roztes),
            'std_nitovani_s':    int(std_nyt_s),
            'std_L_usazeni_s':   int(std_L_us),
            'std_H_usazeni_s':   int(std_H_us),
        }

    kroky.append({
        'id':      'kompletace',
        'label':   'Kompletace',
        'cas_s':   round(komp_s),
        'cas_fmt': fmt_hm(komp_s),
        'detail':  komp_detail,
    })

    # ════════════════════════════════════════════════════════════════════════
    # KROK 10b – Broušení desek před lepením pěn
    # Podmínka: ma_peny AND BOM obsahuje fenol desku ≤ max_tloustka_desky_mm
    # Plocha: z DXF pěny s tloustka_mm ≤ max_tloustka_peny_mm, záloha: 0
    # Čas = Σ plocha_m2 (filtrovaných pěn) × cas_per_m2_s
    # ════════════════════════════════════════════════════════════════════════
    def _tloustka_z_nazvu(nazev):
        _m = re.search(r'(\d+[,.]?\d*)\s*mm', nazev or '')
        return float(_m.group(1).replace(',', '.')) if _m else None

    max_fenol_tl       = p('BrouseniDesek', 'max_tloustka_desky_mm', 10)
    max_pena_tl_bd     = p('BrouseniDesek', 'max_tloustka_peny_mm',  20)
    brouseni_d_per_m2  = p('BrouseniDesek', 'cas_per_m2_s',          45)

    # Množství fenol desek ≤ max_fenol_tl vs. ostatních desek
    ks_fenol   = sum(
        (pol.get('mnozstvi') or 0)
        for pol in bom
        if is_deska(pol)
        and 'FENOL' in norm(pol.get('nazev') or '')
        and (_tloustka_z_nazvu(pol.get('nazev')) or 999) <= max_fenol_tl
    )
    ks_ostatni = sum(
        (pol.get('mnozstvi') or 0)
        for pol in bom
        if is_deska(pol)
        and 'FENOL' not in norm(pol.get('nazev') or '')
    )
    # Broušení má smysl jen pokud fenol desky tvoří hlavní (nebo alespoň stejný) podíl
    ma_fenol_desku      = ks_fenol > 0
    fenol_je_hlavni     = ks_fenol >= ks_ostatni  # pokud ostatní převažují → přepážka, neprovádět

    brouseni_d_s      = 0.0
    brouseni_d_detail = {
        'ma_peny':                ma_peny,
        'ma_fenol_desku':         ma_fenol_desku,
        'fenol_je_hlavni':        fenol_je_hlavni,
        'ks_fenol':               round(ks_fenol, 2),
        'ks_ostatni':             round(ks_ostatni, 2),
        'max_tloustka_desky_mm':  int(max_fenol_tl),
        'max_tloustka_peny_mm':   int(max_pena_tl_bd),
        'cas_per_m2_s':           int(brouseni_d_per_m2),
    }

    if ma_peny and ma_fenol_desku and fenol_je_hlavni:
        if dxf_row and dxf_row['vrstvy_json']:
            _vrstvy_bd    = json.loads(dxf_row['vrstvy_json'])
            _overrides_bd = json.loads(dxf_row['overrides_json'] or '{}')
            plocha_sum    = 0.0
            peny_bd       = []
            for v in _vrstvy_bd:
                ov = _overrides_bd.get(v['nazev'], 'auto')
                if ov == 'ignore':
                    continue
                if ov.startswith('pena:'):
                    vtyp = 'pena'
                    vtl  = float(ov.split(':')[1])
                elif ov.startswith('deska:'):
                    continue
                else:
                    vtyp = v.get('typ', 'jine')
                    vtl  = v.get('tloustka_mm')
                if vtyp != 'pena':
                    continue
                if vtl is not None and vtl > max_pena_tl_bd:
                    continue
                plocha = float(v.get('plocha_m2') or 0)
                plocha_sum += plocha
                peny_bd.append({
                    'nazev':       v['nazev'],
                    'tloustka_mm': vtl,
                    'plocha_m2':   round(plocha, 3),
                })
            brouseni_d_s = plocha_sum * brouseni_d_per_m2
            brouseni_d_detail.update({
                'zdroj':      'dxf',
                'peny':       peny_bd,
                'plocha_m2':  round(plocha_sum, 3),
            })
        else:
            # Záloha: mnozstvi pěn z BOM je v m² — sečteme pěny ≤ max_pena_tl_bd
            peny_bom = []
            plocha_bom = 0.0
            for pol in bom:
                nt = norm(pol.get('typ') or '')
                if not ('PEN' in nt or 'FOAM' in nt or 'BALDACHIN' in nt or 'BALDACYN' in nt):
                    continue
                tl_bom = _tloustka_z_nazvu(pol.get('nazev'))
                if tl_bom is not None and tl_bom > max_pena_tl_bd:
                    continue
                plocha = float(pol.get('mnozstvi') or 0)
                plocha_bom += plocha
                peny_bom.append({
                    'nazev':       pol.get('nazev', ''),
                    'tloustka_mm': tl_bom,
                    'plocha_m2':   round(plocha, 3),
                })
            brouseni_d_s = plocha_bom * brouseni_d_per_m2
            brouseni_d_detail.update({
                'zdroj':     'bom',
                'peny':      peny_bom,
                'plocha_m2': round(plocha_bom, 3),
            })
    else:
        _duvod = []
        if not ma_peny:            _duvod.append('bez_pen')
        if not ma_fenol_desku:     _duvod.append('bez_fenol_desky')
        if not fenol_je_hlavni:    _duvod.append('fenol_pouze_prepazka')
        brouseni_d_detail['duvod_skip'] = '+'.join(_duvod)

    kroky.append({
        'id':      'brouseni_desek',
        'label':   'Broušení desek před lepením pěn',
        'cas_s':   round(brouseni_d_s),
        'cas_fmt': fmt_hm(brouseni_d_s),
        'detail':  brouseni_d_detail,
    })

    # ════════════════════════════════════════════════════════════════════════
    # KROK 10 – Rack lišty
    # Podmínka: BOM obsahuje PROFIL AL s druh=RACK
    # Počet lišt = zaokrouhlení celkové délky / výška casu → nejbližší sudé číslo
    # Čas / lišta = montáž + guma + (výška/22.5 RU) × čas_matice
    # ════════════════════════════════════════════════════════════════════════
    cas_montaz_l  = p('RackListy', 'cas_montaz_listy_s',  120)
    cas_guma_l    = p('RackListy', 'cas_guma_listy_s',     30)
    ru_vyska      = p('RackListy', 'rack_unit_vyska_mm',  22.5)
    cas_matice_ru = p('RackListy', 'cas_matice_ru_s',       6)

    rack_bom = [
        pol for pol in bom
        if norm(pol.get('typ') or '') == 'PROFIL AL'
        and norm(pol.get('druh') or '') == 'RACK'
    ]

    rack_s      = 0
    rack_detail = {'ma_rack': bool(rack_bom)}

    if rack_bom:
        vys_mm = typ['vnitrni_vyska'] or 0   # výška casu v mm

        # Celková délka rack profilů: mnozstvi v metrech → mm
        celk_mm = sum(pol['mnozstvi'] * 1000 for pol in rack_bom)

        if vys_mm > 0:
            # Počet lišt: zaokrouhli na nejbližší sudé číslo
            pocet_raw = celk_mm / vys_mm
            pocet_list = int(round(pocet_raw / 2)) * 2
            if pocet_list < 2:
                pocet_list = 2   # minimum 2 lišty (aspoň pár)

            # Rack units v casu
            rack_units = int(round(vys_mm / ru_vyska))

            # Čas na 1 lištu
            cas_per_lista = cas_montaz_l + cas_guma_l + rack_units * cas_matice_ru
            rack_s = pocet_list * cas_per_lista

            rack_detail.update({
                'celk_mm':          round(celk_mm),
                'vnitrni_vyska_mm': int(vys_mm),
                'pocet_raw':        round(pocet_raw, 2),
                'pocet_list':       pocet_list,
                'rack_units':       rack_units,
                'cas_per_lista_s':  round(cas_per_lista),
                'cas_montaz_s':     int(cas_montaz_l),
                'cas_guma_s':       int(cas_guma_l),
                'cas_matice_ru_s':  int(cas_matice_ru),
                'ru_vyska_mm':      ru_vyska,
            })
        else:
            # Chybí výška → použij jen počet BOM položek × 2 jako odhad
            pocet_list = len(rack_bom) * 2
            rack_units = 0
            cas_per_lista = cas_montaz_l + cas_guma_l
            rack_s = pocet_list * cas_per_lista
            rack_detail.update({
                'pocet_list':      pocet_list,
                'duvod':           'chybi_vyska',
                'cas_per_lista_s': round(cas_per_lista),
            })

    kroky.append({
        'id':      'rack_listy',
        'label':   'Rack lišty',
        'cas_s':   round(rack_s),
        'cas_fmt': fmt_hm(rack_s),
        'detail':  rack_detail,
    })

    # ════════════════════════════════════════════════════════════════════════
    # KROK 11 – Lepení pěn
    # DXF: každá pěna → 60s příprava + max(30s, plocha×120s), × koeficient
    # Záloha: fallback_ks dle typu casu × min čas, × koeficient
    # ════════════════════════════════════════════════════════════════════════

    # Mapování typ_korpusu → klíč parametrů
    _TYP_KLIC = {
        norm('Hlava / kombo'):              'hlava',
        norm('Mixpult'):                    'mixpult',
        norm('Klávesy'):                    'klavesy',
        norm('Rack'):                       'rack',
        norm('Rack Sliding door'):          'rack_slide',
        norm('Accessory case'):             'access',
        norm('Pedalboard'):                 'pedal',
        norm('Case pro světelné hlavy'):    'svetlo',
        norm('Case pro TV'):               'tv',
        norm('Šatní skříň'):               'satna',
        norm('Jiný typ'):                  'jiny',
        norm('Inlay'):                     'inlay',
    }
    tk = _TYP_KLIC.get(typ_korpusu_norm, 'jiny')

    pen_priprava  = p('LepeniPen', 'cas_priprava_s',      300)
    pen_prep_ks   = p('LepeniPen', 'cas_priprava_peny_s',  60)
    pen_per_m2    = p('LepeniPen', 'cas_per_m2_s',        120)
    pen_min       = p('LepeniPen', 'cas_min_s',            30)
    pen_koef      = p('LepeniPen', f'koef_{tk}',           1.0)
    pen_fks       = p('LepeniPen', f'fks_{tk}',            10)

    pen_s      = 0
    pen_detail = {'typ_klic': tk, 'koeficient': pen_koef}

    if dxf_row and dxf_row['vrstvy_json']:
        vrstvy    = json.loads(dxf_row['vrstvy_json'])
        overrides = json.loads(dxf_row['overrides_json'] or '{}')

        peny_dxf = []
        for v in vrstvy:
            ov = overrides.get(v['nazev'], 'auto')
            if ov == 'ignore':
                continue
            if ov.startswith('pena:'):
                vtyp = 'pena'
            elif ov.startswith('deska:'):
                continue
            elif ov == 'auto':
                vtyp = v.get('typ', 'jine')
            else:
                vtyp = v.get('typ', 'jine')
            if vtyp != 'pena':
                continue

            plocha = v.get('plocha_m2') or 0
            ks     = int(v.get('ks', 0))
            cas_obsah_ks = max(pen_min, plocha * pen_per_m2)
            cas_peny_ks  = pen_prep_ks + cas_obsah_ks
            sub = ks * cas_peny_ks
            peny_dxf.append({
                'nazev':       v['nazev'],
                'ks':          ks,
                'plocha_m2':   round(plocha, 3),
                'cas_ks_s':    round(cas_peny_ks, 1),
                'sub_s':       round(sub, 1),
            })

        brutto = pen_priprava + sum(x['sub_s'] for x in peny_dxf)
        pen_s  = brutto * pen_koef
        pen_detail.update({
            'zdroj':      'dxf',
            'peny':       peny_dxf,
            'brutto_s':   round(brutto),
            'cas_priprava_s':   int(pen_priprava),
            'cas_priprava_peny_s': int(pen_prep_ks),
            'cas_per_m2_s':    int(pen_per_m2),
            'cas_min_s':       int(pen_min),
        })
    else:
        # Záloha: fallback ks × (příprava + min čas), × koeficient
        brutto = pen_priprava + pen_fks * (pen_prep_ks + pen_min)
        pen_s  = brutto * pen_koef
        pen_detail.update({
            'zdroj':       'fallback',
            'fallback_ks': int(pen_fks),
            'brutto_s':    round(brutto),
        })

    # ════════════════════════════════════════════════════════════════════════
    # KROK 12 – Polep tapetou (materiál kód = 'POLEP ART')
    # ════════════════════════════════════════════════════════════════════════
    ma_polep   = any((pol.get('material_kod') or '').strip().upper() == 'POLEP ART' for pol in bom)
    polep_s    = p('Polep', 'cas_polep_s', 3000) if ma_polep else 0

    kroky.append({
        'id':      'polep',
        'label':   'Polep tapetou',
        'cas_s':   round(polep_s),
        'cas_fmt': fmt_hm(polep_s),
        'detail':  {'ma_polep': ma_polep, 'cas_polep_s': int(p('Polep', 'cas_polep_s', 3000))},
    })

    kroky.append({
        'id':      'lepeni_pen',
        'label':   'Lepení pěn',
        'cas_s':   round(pen_s),
        'cas_fmt': fmt_hm(pen_s),
        'detail':  pen_detail,
    })

    # KROK 13 – Prostoje (zametání, ofukování, odnesení casu atd.)
    # ════════════════════════════════════════════════════════════════════════
    prostoje_delitel = p('Prostoje', 'delitel', 8)
    prostoje_delitel = prostoje_delitel if prostoje_delitel else 8
    cas_pred_prostoji = sum(k['cas_s'] for k in kroky)
    prostoje_s = cas_pred_prostoji / prostoje_delitel

    kroky.append({
        'id':      'prostoje',
        'label':   'Prostoje',
        'cas_s':   round(prostoje_s),
        'cas_fmt': fmt_hm(prostoje_s),
        'detail':  {
            'cas_pred_prostoji_s': cas_pred_prostoji,
            'delitel':             prostoje_delitel,
        },
    })

    # ── Cenové parametry pro výpočet správné MC ───────────────────────────────
    ceny_par = {klic: val for klic, val in par.get('Ceny', {}).items()}

    # ── Vícepráce (empirické korekce uložené na typu casu) ────────────────────
    vp_komp_s = int(typ.get('viceprace_kompletace_s') or 0)
    vp_peny_s = int(typ.get('viceprace_peny_s') or 0)

    kroky_s   = sum(k['cas_s'] for k in kroky)
    celkem_s  = kroky_s + vp_komp_s + vp_peny_s

    return jsonify({
        'typ_id':     typ_id,
        'hn_cislo':   typ['hn_cislo'],
        'kroky':      kroky,
        'kroky_s':    round(kroky_s),
        'celkem_s':   round(celkem_s),
        'celkem_fmt': fmt_hm(celkem_s),
        'ceny_par':   ceny_par,
        'viceprace_kompletace_s': vp_komp_s,
        'viceprace_peny_s':       vp_peny_s,
        # Metadata pro kompatibilitu s MC výpočtem
        'pocet_desek': pocet_desek,
        'je_fusion':   je_fusion,
    })


@app.route('/api/typy-casu/<int:typ_id>/viceprace', methods=['PATCH'])
def api_viceprace_patch(typ_id):
    """Uloží vícepráce (empirické korekce) pro typ casu."""
    data = request.get_json(force=True)
    vp_komp = int(data.get('viceprace_kompletace_s', 0) or 0)
    vp_peny = int(data.get('viceprace_peny_s', 0) or 0)
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        UPDATE typy_casu
           SET viceprace_kompletace_s = ?,
               viceprace_peny_s       = ?
         WHERE id = ?
    """, (vp_komp, vp_peny, typ_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'viceprace_kompletace_s': vp_komp, 'viceprace_peny_s': vp_peny})

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
        SELECT d.datum, d.cas_od, d.cas_do, u.jmeno, u.barva, u.role
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
          AND z.odeslano_do_vyroby = 1
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


# ─── VERZE SYSTÉMU ───────────────────────────────────────────────────────────
@app.route('/api/verze')
def api_verze():
    import json as _json
    ver_path = os.path.join(os.path.dirname(__file__), 'version.json')
    try:
        with open(ver_path, encoding='utf-8') as f:
            return jsonify(_json.load(f))
    except:
        return jsonify({'verze': '?', 'popis': '', 'autor': '', 'datum': '', 'cas': '', 'git_commit': ''})


# ─── ADMIN: NAHRÁNÍ DATABÁZE ─────────────────────────────────────────────────
UPLOAD_SECRET = os.environ.get('UPLOAD_SECRET', 'razzor-upload-2026')

@app.route('/admin/download-db', methods=['GET'])
def admin_download_db():
    secret = request.args.get('secret', '')
    if secret != UPLOAD_SECRET:
        return jsonify({'error': 'Unauthorized'}), 403
    from database import DB_PATH
    return send_file(DB_PATH, as_attachment=True, download_name='system.db')


@app.route('/admin/upload-db', methods=['POST'])
def admin_upload_db():
    secret = request.headers.get('X-Upload-Secret', '')
    if secret != UPLOAD_SECRET:
        return jsonify({'error': 'Unauthorized'}), 403
    if 'file' not in request.files:
        return jsonify({'error': 'Chybí soubor'}), 400
    f = request.files['file']
    from database import DB_PATH
    import shutil
    backup_path = DB_PATH + '.backup'
    if os.path.exists(DB_PATH):
        shutil.copy2(DB_PATH, backup_path)
    f.save(DB_PATH)
    return jsonify({'ok': True, 'message': 'Databáze nahrána'})


# ─── FAKTURACE – REPORT VYFAKTUROVÁNO ────────────────────────────────────

@app.route('/api/fakturace/report')
def api_fakturace_report():
    """Report Vyfakturováno: jeden řádek na fakturu-položku, filtrováno dle data vystavení.
    Parametry: od=YYYY-MM-DD, do=YYYY-MM-DD (volitelné)
    """
    od = request.args.get('od', '')
    do = request.args.get('do', '')

    params = []
    where  = []
    if od:
        where.append("f.datum_vystaveni >= ?")
        params.append(od)
    if do:
        where.append("f.datum_vystaveni <= ?")
        params.append(do)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    conn = get_db()
    c = conn.cursor()
    c.execute(f"""
        SELECT
            f.id            AS faktura_id,
            f.cislo         AS faktura_cislo,
            f.datum_vystaveni,
            COALESCE(f.odberatel_nazev, '') AS odberatel_nazev,
            fp.nazev,
            fp.hn_cislo,
            fp.ks,
            ROUND(fp.cena_dilu_snapshot * fp.ks, 2)    AS dily,
            ROUND(fp.cena_vyroby_snapshot * fp.ks, 2)  AS prace,
            4.70                                        AS marze_sazba,
            ROUND(fp.zaklad, 2)                         AS celkem_bez_dph,
            ROUND(fp.celkem_s_dph, 2)                   AS celkem_s_dph,
            NULL                                        AS sn,
            NULL                                        AS marze_ap,
            z.typ_casu_id                               AS typ_casu_id
        FROM faktury f
        JOIN faktury_polozky fp ON fp.faktura_id = f.id
        LEFT JOIN zakazky z ON z.id = fp.zakazka_id
        {where_sql}
        ORDER BY f.datum_vystaveni DESC, f.id DESC, fp.id
    """, params)
    rows = db_rows_to_list(c.fetchall())
    conn.close()
    return jsonify({'ok': True, 'rows': rows})


# ─── NASTAVENÍ SYSTÉMU ────────────────────────────────────────────────────

@app.route('/api/nastaveni', methods=['GET'])
def api_nastaveni_get():
    """Vrátí všechna nastavení jako slovník {klic: hodnota}."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT klic, hodnota FROM nastaveni")
    rows = c.fetchall()
    conn.close()
    return jsonify({r['klic']: r['hodnota'] for r in rows})

@app.route('/api/nastaveni', methods=['POST'])
def api_nastaveni_post():
    """Uloží jedno nebo více nastavení. Body: {klic: hodnota, ...}"""
    data = request.get_json()
    if not data or not isinstance(data, dict):
        return jsonify({'error': 'Očekáván JSON objekt'}), 400
    conn = get_db()
    c = conn.cursor()
    for klic, hodnota in data.items():
        c.execute("INSERT OR REPLACE INTO nastaveni (klic, hodnota) VALUES (?,?)", (klic, str(hodnota) if hodnota is not None else ''))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/nastaveni/email-test', methods=['POST'])
def api_nastaveni_email_test():
    """Odešle testovací email na nakonfigurované adresy."""
    try:
        gmail_user   = get_nastaveni('email_gmail_user', '')
        gmail_pass   = get_nastaveni('email_gmail_pass', '')
        prijemci_raw = get_nastaveni('email_prijemci', '')

        if not gmail_user or not gmail_pass:
            return jsonify({'error': 'Gmail účet nebo heslo není nastaveno'}), 400
        if not prijemci_raw.strip():
            return jsonify({'error': 'Nejsou zadáni žádní příjemci'}), 400

        prijemci = [a.strip() for a in prijemci_raw.replace(';', ',').split(',') if a.strip()]

        msg = MIMEMultipart()
        msg['From']    = gmail_user
        msg['To']      = ', '.join(prijemci)
        msg['Subject'] = 'Test – Razzor Cases email'
        msg.attach(MIMEText('Testovací zpráva ze systému Razzor Cases. E-mail funguje správně.', 'plain', 'utf-8'))

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(gmail_user, gmail_pass)
            server.sendmail(gmail_user, prijemci, msg.as_bytes())

        return jsonify({'ok': True, 'message': f'Testovací email odeslán na: {", ".join(prijemci)}'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── BOM IMPORT — ignorované kódy ─────────────────────────────────────────────

@app.route('/api/bom-import-ignore', methods=['GET'])
def api_bom_import_ignore_get():
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT kod, popis FROM bom_import_ignore ORDER BY kod")
    rows = [{'kod': r[0], 'popis': r[1]} for r in c.fetchall()]
    conn.close()
    return jsonify(rows)

@app.route('/api/bom-import-ignore', methods=['POST'])
def api_bom_import_ignore_post():
    d = request.get_json() or {}
    kod = (d.get('kod') or '').strip()
    popis = (d.get('popis') or '').strip()
    if not kod:
        return jsonify({'error': 'Chybí kód'}), 400
    conn = get_db(); c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO bom_import_ignore (kod, popis) VALUES (?,?)", (kod, popis))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/bom-import-ignore/<kod>', methods=['DELETE'])
def api_bom_import_ignore_delete(kod):
    conn = get_db(); c = conn.cursor()
    c.execute("DELETE FROM bom_import_ignore WHERE kod=?", (kod,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


# ── TYPY KORPUSU ─────────────────────────────────────────────────────────────
@app.route('/api/typy-korpusu', methods=['GET'])
def api_typy_korpusu_get():
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT id, nazev, poradi FROM typy_korpusu ORDER BY poradi, id")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(rows)

@app.route('/api/typy-korpusu', methods=['POST'])
def api_typy_korpusu_post():
    d = request.json or {}
    nazev = (d.get('nazev') or '').strip()
    if not nazev:
        return jsonify({'error': 'Název nesmí být prázdný'}), 400
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT COALESCE(MAX(poradi),0)+1 FROM typy_korpusu")
    poradi = c.fetchone()[0]
    c.execute("INSERT INTO typy_korpusu (nazev, poradi) VALUES (?,?)", (nazev, poradi))
    new_id = c.lastrowid
    conn.commit(); conn.close()
    return jsonify({'id': new_id, 'nazev': nazev, 'poradi': poradi})

@app.route('/api/typy-korpusu/<int:tid>', methods=['PUT'])
def api_typy_korpusu_put(tid):
    d = request.json or {}
    conn = get_db(); c = conn.cursor()
    if 'nazev' in d:
        nazev = d['nazev'].strip()
        if not nazev:
            conn.close(); return jsonify({'error': 'Název nesmí být prázdný'}), 400
        c.execute("UPDATE typy_korpusu SET nazev=? WHERE id=?", (nazev, tid))
    if 'poradi' in d:
        c.execute("UPDATE typy_korpusu SET poradi=? WHERE id=?", (d['poradi'], tid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/typy-korpusu/<int:tid>', methods=['DELETE'])
def api_typy_korpusu_delete(tid):
    conn = get_db(); c = conn.cursor()
    c.execute("DELETE FROM typy_korpusu WHERE id=?", (tid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


# ── VÝCHOZÍ BOM ──────────────────────────────────────────────────────────────

@app.route('/api/vychozi-bom', methods=['GET'])
def api_vychozi_bom_get():
    conn = get_db(); c = conn.cursor()
    c.execute("""
        SELECT v.material_kod, v.mnozstvi, v.poradi,
               m.nazev, m.typ, m.nc_bez_dph
        FROM vychozi_bom v
        LEFT JOIN materialy m ON m.kod = v.material_kod
        ORDER BY v.poradi, v.material_kod
    """)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(rows)

@app.route('/api/vychozi-bom', methods=['POST'])
def api_vychozi_bom_post():
    data = request.json
    kod = (data.get('material_kod') or '').strip()
    mnozstvi = float(data.get('mnozstvi') or 1)
    if not kod:
        return jsonify({'error': 'material_kod je povinný'}), 400
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM materialy WHERE kod=?", (kod,))
    if not c.fetchone()[0]:
        conn.close()
        return jsonify({'error': f'Materiál {kod} neexistuje v katalogu'}), 404
    c.execute("SELECT COALESCE(MAX(poradi),0)+1 FROM vychozi_bom")
    poradi = c.fetchone()[0]
    c.execute("INSERT OR REPLACE INTO vychozi_bom (material_kod, mnozstvi, poradi) VALUES (?,?,?)",
              (kod, mnozstvi, poradi))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/vychozi-bom/<path:mat_kod>', methods=['PUT'])
def api_vychozi_bom_put(mat_kod):
    data = request.json
    conn = get_db(); c = conn.cursor()
    c.execute("UPDATE vychozi_bom SET mnozstvi=? WHERE material_kod=?",
              (float(data.get('mnozstvi', 1)), mat_kod))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/vychozi-bom/<path:mat_kod>', methods=['DELETE'])
def api_vychozi_bom_delete(mat_kod):
    conn = get_db(); c = conn.cursor()
    c.execute("DELETE FROM vychozi_bom WHERE material_kod=?", (mat_kod,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


if __name__ == '__main__':
    init_db()
    auto_migrate()
    print("\n" + "="*60)
    print("  Flight Case výrobní systém")
    print("  Otevři v prohlížeči: http://localhost:5001")
    print("  Ze sítě:             http://<IP-tohoto-PC>:5001")
    print("="*60 + "\n")
    app.run(host='0.0.0.0', port=5001, debug=True)
