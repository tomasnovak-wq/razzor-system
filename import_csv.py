"""
Import dat z Google Sheets CSV exportů
- material.csv  → tabulka materialy + sklad
- vhw.csv       → tabulka typy_casu + kusovniky
"""
import csv
import sqlite3
import os
import sys
import re

DB_PATH = os.path.join(os.path.dirname(__file__), 'data', 'system.db')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def parse_number(s):
    """Bezpečný převod textu na číslo (ošetří české formátování, jednotky kg/Kč/%)"""
    if not s or str(s).strip() in ('', '-', 'N/A', '#N/A', '#REF!', '#VALUE!'):
        return 0.0
    s = str(s).strip()
    # Odstraň měnové symboly, jednotky a mezery
    s = re.sub(r'[Kč\s%]', '', s)
    s = re.sub(r'[a-zA-Z]+$', '', s)   # strip trailing units: kg, Kč, h, ...
    # České číslo: 1 234,56 → 1234.56
    s = s.replace('\xa0', '').replace(' ', '')
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.')
    elif ',' in s:
        s = s.replace(',', '.')
    try:
        return float(s)
    except:
        return 0.0


def import_material(csv_path, conn):
    """
    Import listu MATERIAL
    Struktura (řádek 3 = header):
    A: Č. produktu, B: Název, C: Typ, D: Druh, E: Zobrazovat,
    F: Umístění, G: Hmotnost, H: Průřez, I: Nýty,
    J: Balení m2/m/l, K: Nákup/balení, L: Nákup/jednotka,
    M: NC CZK bez DPH/jednotka, N: Časy(s), O: Master balení,
    P: Dodavatel, Q: Dodací lhůta, R: Šířka HW, S: Priorita
    """
    print(f"\n--- Import MATERIAL z: {csv_path} ---")

    with open(csv_path, encoding='utf-8-sig', errors='replace') as f:
        reader = csv.reader(f)
        rows = list(reader)

    print(f"Celkem řádků v CSV: {len(rows)}")

    # Najdi řádek s hlavičkou (obsahuje "Č. produktu" nebo "produktu")
    header_row = None
    for i, row in enumerate(rows):
        if any('produkt' in str(cell).lower() for cell in row):
            header_row = i
            print(f"Hlavička nalezena na řádku {i+1}: {row[:6]}")
            break

    if header_row is None:
        # Zkus řádek 3 (index 2)
        header_row = 2
        print(f"Hlavička automaticky nastavena na řádek {header_row+1}")

    c = conn.cursor()
    imported = 0
    skipped = 0

    for row_idx in range(header_row + 1, len(rows)):
        row = rows[row_idx]
        if len(row) < 2:
            continue

        kod = str(row[0]).strip() if len(row) > 0 else ''
        nazev = str(row[1]).strip() if len(row) > 1 else ''

        # Přeskoč prázdné řádky a řádky bez kódu nebo názvu
        if not kod or not nazev or kod in ('0', '#N/A', '#REF!'):
            skipped += 1
            continue
        # Přeskoč řádky kde kód je jen 0
        if all(c == '0' for c in kod):
            skipped += 1
            continue

        typ = str(row[2]).strip() if len(row) > 2 else ''
        druh = str(row[3]).strip() if len(row) > 3 else ''
        zobrazovat = 1
        # Sloupec E - zobrazovat (checkbox - TRUE/FALSE nebo ✓)
        if len(row) > 4:
            zob_val = str(row[4]).strip().upper()
            zobrazovat = 0 if zob_val in ('FALSE', '0', '') else 1

        umisteni = str(row[5]).strip() if len(row) > 5 else ''
        hmotnost = parse_number(row[6]) if len(row) > 6 else 0
        nity     = parse_number(row[8]) if len(row) > 8 else 0   # sloupec I = Nýty
        balenf = parse_number(row[9]) if len(row) > 9 else 1
        if balenf == 0:
            balenf = 1
        nakup_baleni = parse_number(row[10]) if len(row) > 10 else 0
        nakup_jednotka = parse_number(row[11]) if len(row) > 11 else 0
        nc_bez_dph = parse_number(row[12]) if len(row) > 12 else 0
        cas_s = parse_number(row[13]) if len(row) > 13 else 0
        master_baleni = int(parse_number(row[14])) if len(row) > 14 else 1
        dodavatel = str(row[15]).strip() if len(row) > 15 else ''
        dodaci_lhuta = int(parse_number(row[16])) if len(row) > 16 else 14
        sirka_hw = int(parse_number(row[17])) if len(row) > 17 else 0
        priorita = str(row[18]).strip() if len(row) > 18 else 'Střední'
        if not priorita:
            priorita = 'Střední'

        try:
            # INSERT OR IGNORE zachová existující řádek (oblíbený, web_url, poznamka atd.)
            c.execute("""
                INSERT OR IGNORE INTO materialy
                (kod, nazev, typ, druh, zobrazovat, umisteni, hmotnost, nity, balenf,
                 nakup_baleni, nakup_jednotka, nc_bez_dph, cas_s, master_baleni,
                 dodavatel, dodaci_lhuta, sirka_hw, priorita)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (kod, nazev, typ, druh, zobrazovat, umisteni, hmotnost, nity, balenf,
                  nakup_baleni, nakup_jednotka, nc_bez_dph, cas_s, master_baleni,
                  dodavatel, dodaci_lhuta, sirka_hw, priorita))

            # UPDATE aktualizuje pouze CSV pole — oblibeny, web_url, poznamka se NEpřepisují
            c.execute("""
                UPDATE materialy SET
                    nazev=?, typ=?, druh=?, zobrazovat=?, umisteni=?, hmotnost=?,
                    nity=?, balenf=?, nakup_baleni=?, nakup_jednotka=?, nc_bez_dph=?,
                    cas_s=?, master_baleni=?, dodavatel=?, dodaci_lhuta=?, sirka_hw=?,
                    priorita=?, updated_at=datetime('now')
                WHERE kod=?
            """, (nazev, typ, druh, zobrazovat, umisteni, hmotnost, nity, balenf,
                  nakup_baleni, nakup_jednotka, nc_bez_dph, cas_s, master_baleni,
                  dodavatel, dodaci_lhuta, sirka_hw, priorita, kod))

            # Inicializuj sklad pro tuto položku (pokud ještě neexistuje)
            c.execute("INSERT OR IGNORE INTO sklad (material_kod) VALUES (?)", (kod,))

            imported += 1
        except Exception as e:
            print(f"  Chyba řádek {row_idx+1} [{kod}]: {e}")
            skipped += 1

    conn.commit()
    print(f"MATERIAL: importováno {imported}, přeskočeno {skipped}")

    # ── Auto-NYTY: napáruj závislosti nýtů automaticky ────────────────────────
    # Najdi v databázi materiál, který reprezentuje NYTY – prioritní jsou přesné shody kódu
    c.execute("""
        SELECT kod, nazev FROM materialy
        WHERE LOWER(kod)   LIKE '%nyt%'
           OR LOWER(nazev) LIKE '%nyt%'
           OR LOWER(nazev) LIKE '%nýt%'
        ORDER BY
            CASE WHEN UPPER(kod) IN ('NYTY','NÝTY') THEN 0 ELSE 1 END,
            CASE WHEN LOWER(kod) LIKE '%nyt%'       THEN 0 ELSE 1 END
        LIMIT 5
    """)
    kandidati = c.fetchall()
    nyty_kod = kandidati[0]['kod'] if kandidati else None

    if nyty_kod:
        c.execute("SELECT kod, nity FROM materialy WHERE nity > 0")
        nity_mats = c.fetchall()
        nyty_count = 0
        for mat in nity_mats:
            c.execute("""
                INSERT OR REPLACE INTO material_spojeniky
                    (material_kod, spojovaci_kod, mnozstvi_na_kus)
                VALUES (?, ?, ?)
            """, (mat['kod'], nyty_kod, mat['nity']))
            nyty_count += 1
        conn.commit()
        print(f"  Auto-NYTY [{nyty_kod}]: {nyty_count} závislostí napárováno")
    else:
        print("  Auto-NYTY: materiál NYTY nenalezen v databázi – závislosti nebyly nastaveny")
        print("  (Až bude materiál NYTY v databázi, použij Nastavení → ⚙ Spojeniky → Hromadné přiřazení)")

    return imported


def import_vhw(csv_path, conn):
    """
    Import listu VHW - kusovníková matice

    Struktura VHW:
    Řádek 1: HN čísla case typů (od sloupce E dál)
    Řádek 2: Názvy case typů
    Řádek 3: Vyplněno?
    Řádek 4: Vyrobeno ks
    Řádek 5: Cena dílů
    Řádek 6: Header materiálů (Č.produktu, Název, Typ, Čas, ...)
    Řádky 7+: Materiály s množstvími pro každý case
    Řádek 451: Typ korpusu
    Řádek 452: Vnitřní rozměry Š (width)
    Řádek 453: Vnitřní rozměry V (height)
    Řádek 454: Vnitřní rozměry H (depth)
    Řádek 455: Cena výroby
    """
    print(f"\n--- Import VHW z: {csv_path} ---")

    with open(csv_path, encoding='utf-8-sig', errors='replace') as f:
        reader = csv.reader(f)
        rows = list(reader)

    print(f"Celkem řádků v CSV: {len(rows)}, sloupců přibližně: {len(rows[0]) if rows else 0}")

    if len(rows) < 10:
        print("CHYBA: CSV je příliš krátký!")
        return 0

    # Najdi řádek s HN čísly (řádek 1, index 0)
    # HN čísla jsou ve formátu HNxxxxxx nebo xXXXXXX
    hn_row_idx = None
    nazev_row_idx = None
    mat_header_idx = None

    for i, row in enumerate(rows[:15]):
        # HN row - obsahuje buňky začínající HN nebo x
        hn_count = sum(1 for cell in row if re.match(r'^(HN|x)\d+', str(cell).strip()))
        if hn_count > 3:
            hn_row_idx = i
            print(f"HN řádek nalezen: {i+1} (počet HN: {hn_count})")
            break

    if hn_row_idx is None:
        hn_row_idx = 0
        print(f"HN řádek nastaven na 0")

    nazev_row_idx = hn_row_idx + 1

    # Najdi řádek s hlavičkou materiálů – kontroluj jen prvních 6 sloupců (ne celý řádek,
    # protože v názvech casů může být slovo "reproduktor" → obsahuje "produkt")
    for i in range(hn_row_idx, min(hn_row_idx + 10, len(rows))):
        row = rows[i]
        row_text = ' '.join(str(c) for c in row[:6]).lower()
        if 'produkt' in row_text or ('název' in row_text and 'case' not in row_text):
            mat_header_idx = i
            print(f"Header materiálů nalezen: řádek {i+1}: {row[:6]}")
            break

    if mat_header_idx is None:
        mat_header_idx = hn_row_idx + 5
        print(f"Header materiálů nastaven na řádek {mat_header_idx+1}")

    # Načti HN čísla a najdi první sloupec s case daty
    hn_row = rows[hn_row_idx] if hn_row_idx < len(rows) else []
    nazev_row = rows[nazev_row_idx] if nazev_row_idx < len(rows) else []

    # Najdi index prvního sloupce s HN číslem
    case_col_start = None
    for col_idx, cell in enumerate(hn_row):
        if re.match(r'^(HN|x)\d+', str(cell).strip()):
            case_col_start = col_idx
            break

    if case_col_start is None:
        # Zkus najít v řádku s hlavičkou - case sloupce jsou od sloupce D (index 3) nebo E (4)
        case_col_start = 4
        print(f"case_col_start nastaven na {case_col_start}")

    print(f"Case sloupce začínají na sloupci: {case_col_start} (0-indexed)")

    # Najdi řádky s rozměry a cenou (jsou na konci - hledej "rozměr" nebo "vnitřní")
    sirka_row_idx = typ_row_idx = vyska_row_idx = hloubka_row_idx = cena_row_idx = None
    hmotnost_case_row_idx = prodej_ap_row_idx = cena_dilu_row_idx = spravna_mc_row_idx = None

    for i in range(len(rows)-1, -1, -1):
        row = rows[i]
        row_text = ' '.join(str(c) for c in row[:5]).lower()
        if 'šířk' in row_text or 'sirk' in row_text or 'with' in row_text or 'width' in row_text:
            if sirka_row_idx is None:
                sirka_row_idx = i
        elif 'výšk' in row_text or 'vysk' in row_text or 'height' in row_text:
            if vyska_row_idx is None:
                vyska_row_idx = i
        elif 'hloub' in row_text or 'depth' in row_text or 'lenght' in row_text or 'length' in row_text:
            if hloubka_row_idx is None:
                hloubka_row_idx = i
        elif 'cena výroby' in row_text or 'cena vyrob' in row_text:
            if cena_row_idx is None:
                cena_row_idx = i
        elif 'typ korpusu' in row_text or 'typ komp' in row_text:
            if typ_row_idx is None:
                typ_row_idx = i
        if 'hmotnost' in row_text and hmotnost_case_row_idx is None:
            hmotnost_case_row_idx = i
        if ('prodej ap' in row_text) and prodej_ap_row_idx is None:
            prodej_ap_row_idx = i
        if ('cena díl' in row_text or 'cena dil' in row_text) and cena_dilu_row_idx is None:
            cena_dilu_row_idx = i
        if ('správná mc' in row_text or 'spravna mc' in row_text or 'maloobchod' in row_text
                or (' mc' in row_text and ('cena' in row_text or 'správn' in row_text or 'spravna' in row_text))):
            if spravna_mc_row_idx is None:
                spravna_mc_row_idx = i

    # cas_narocnost je uložen v mat_header_idx řádku (sloupce materiálů, col 7+ = hodnoty per case)
    cas_narocnost_row_idx = mat_header_idx

    print(f"Rozměrové řádky: šířka={sirka_row_idx}, výška={vyska_row_idx}, hloubka={hloubka_row_idx}, cena={cena_row_idx}, typ={typ_row_idx}")
    print(f"Doplňkové řádky: hmotnost={hmotnost_case_row_idx}, prodej_ap={prodej_ap_row_idx}, cena_dilu={cena_dilu_row_idx}, spravna_mc={spravna_mc_row_idx}, cas_narocnost={cas_narocnost_row_idx}")

    # Zpracuj každý case typ (každý sloupec od case_col_start)
    c = conn.cursor()
    imported_cases = 0
    imported_bom = 0
    skipped_cases = 0

    total_cols = len(hn_row)
    print(f"Celkem sloupců: {total_cols}, case sloupce: {case_col_start} až {total_cols-1}")

    for col_idx in range(case_col_start, total_cols):
        hn_cislo = str(hn_row[col_idx]).strip() if col_idx < len(hn_row) else ''

        # Přeskoč prázdné nebo nevalidní HN čísla
        if not hn_cislo or not re.match(r'^(HN|x)\d+', hn_cislo):
            continue

        nazev = str(nazev_row[col_idx]).strip() if col_idx < len(nazev_row) else ''
        if not nazev:
            nazev = hn_cislo

        # Přeskoč "ZHASNOUT" (deaktivované case typy)
        aktivni = 0 if 'ZHASNOUT' in nazev.upper() or 'ZHASNOUT' in hn_cislo.upper() else 1

        # Rozměry
        typ_korpusu = ''
        sirka = vyska = hloubka = cena_vyroby = 0
        cas_narocnost = hmotnost_case = prodej_ap = cena_dilu = spravna_mc = 0.0

        if typ_row_idx and col_idx < len(rows[typ_row_idx]):
            typ_korpusu = str(rows[typ_row_idx][col_idx]).strip()
        if sirka_row_idx and col_idx < len(rows[sirka_row_idx]):
            sirka = int(parse_number(rows[sirka_row_idx][col_idx]))
        if vyska_row_idx and col_idx < len(rows[vyska_row_idx]):
            vyska = int(parse_number(rows[vyska_row_idx][col_idx]))
        if hloubka_row_idx and col_idx < len(rows[hloubka_row_idx]):
            hloubka = int(parse_number(rows[hloubka_row_idx][col_idx]))
        if cena_row_idx and col_idx < len(rows[cena_row_idx]):
            cena_vyroby = parse_number(rows[cena_row_idx][col_idx])
        if cas_narocnost_row_idx is not None and col_idx < len(rows[cas_narocnost_row_idx]):
            cas_narocnost = parse_number(rows[cas_narocnost_row_idx][col_idx])
        if hmotnost_case_row_idx is not None and col_idx < len(rows[hmotnost_case_row_idx]):
            hmotnost_case = parse_number(rows[hmotnost_case_row_idx][col_idx])
        if prodej_ap_row_idx is not None and col_idx < len(rows[prodej_ap_row_idx]):
            prodej_ap = parse_number(rows[prodej_ap_row_idx][col_idx])
        if cena_dilu_row_idx is not None and col_idx < len(rows[cena_dilu_row_idx]):
            cena_dilu = parse_number(rows[cena_dilu_row_idx][col_idx])
        if spravna_mc_row_idx is not None and col_idx < len(rows[spravna_mc_row_idx]):
            spravna_mc = parse_number(rows[spravna_mc_row_idx][col_idx])

        try:
            # Nejdřív vlož nový záznam (pokud ještě neexistuje)
            c.execute("""
                INSERT OR IGNORE INTO typy_casu (hn_cislo, nazev)
                VALUES (?, ?)
            """, (hn_cislo, nazev))

            # Získej ID (existující nebo právě vložený)
            c.execute("SELECT id FROM typy_casu WHERE hn_cislo=?", (hn_cislo,))
            row_db = c.fetchone()
            typ_id = row_db[0] if row_db else None

            # Aktualizuj pouze hodnoty z CSV – ručně zadaná pole (pena, prisl, ...) se zachovají
            c.execute("""
                UPDATE typy_casu SET
                    nazev=?, typ_korpusu=?, vnitrni_sirka=?, vnitrni_vyska=?,
                    vnitrni_hloubka=?, cena_vyroby=?, aktivni=?,
                    cas_narocnost=?, hmotnost=?, prodej_ap_bez_dph=?, cena_dilu=?,
                    spravna_mc=?,
                    updated_at=datetime('now')
                WHERE hn_cislo=?
            """, (nazev, typ_korpusu, sirka, vyska, hloubka, cena_vyroby, aktivni,
                  cas_narocnost, hmotnost_case, prodej_ap, cena_dilu, spravna_mc, hn_cislo))

            imported_cases += 1

            # Zpracuj BOM pro tento case - projdi řádky s materiály
            bom_count = 0
            # Sada řádků, které jsou metadata (rozměry, ceny, atd.) — ne BOM položky
            metadata_radky = {r for r in [
                typ_row_idx, sirka_row_idx, vyska_row_idx, hloubka_row_idx,
                cena_row_idx, hmotnost_case_row_idx, prodej_ap_row_idx,
                cena_dilu_row_idx, spravna_mc_row_idx, mat_header_idx,
            ] if r is not None}
            for mat_row_idx in range(mat_header_idx + 1, len(rows)):
                mat_row = rows[mat_row_idx]

                # Přeskoč metadata řádky (rozměry, ceny, hmotnost, MC, atd.)
                if mat_row_idx in metadata_radky:
                    continue

                mat_kod = str(mat_row[0]).strip() if len(mat_row) > 0 else ''
                if not mat_kod or mat_kod in ('', '0', '#N/A', '#REF!'):
                    continue

                # Přeskoč nadpisové řádky a booleovské hodnoty z Google Sheets
                if len(mat_kod) > 30 or mat_kod.lower() in ('č. produktu', 'č.produktu', 'kod', 'kód', 'true', 'false', 'ano', 'ne'):
                    continue

                # Množství v tomto sloupci
                mnozstvi_str = str(mat_row[col_idx]).strip() if col_idx < len(mat_row) else ''
                mnozstvi = parse_number(mnozstvi_str)

                if mnozstvi == 0:
                    continue

                # Ověř, že materiál existuje v databázi
                c.execute("SELECT kod FROM materialy WHERE kod=?", (mat_kod,))
                if not c.fetchone():
                    # Materiál neexistuje - přidej ho jako neznámý
                    mat_nazev = str(mat_row[1]).strip() if len(mat_row) > 1 else mat_kod
                    mat_typ = str(mat_row[2]).strip() if len(mat_row) > 2 else ''
                    c.execute("""
                        INSERT OR IGNORE INTO materialy (kod, nazev, typ)
                        VALUES (?,?,?)
                    """, (mat_kod, mat_nazev, mat_typ))
                    c.execute("INSERT OR IGNORE INTO sklad (material_kod) VALUES (?)", (mat_kod,))

                try:
                    c.execute("""
                        INSERT OR REPLACE INTO kusovniky (typ_casu_id, material_kod, mnozstvi)
                        VALUES (?,?,?)
                    """, (typ_id, mat_kod, mnozstvi))
                    bom_count += 1
                    imported_bom += 1
                except Exception as e:
                    pass

        except Exception as e:
            print(f"  Chyba case {hn_cislo}: {e}")
            skipped_cases += 1
            continue

        if imported_cases % 100 == 0:
            print(f"  Progress: {imported_cases} case typů...")
            conn.commit()

    conn.commit()
    print(f"\nVHW import dokončen:")
    print(f"  Case typy: importováno {imported_cases}, přeskočeno {skipped_cases}")
    print(f"  BOM položky: {imported_bom}")
    return imported_cases


def run_import(material_csv=None, vhw_csv=None):
    """Spustí import z CSV souborů"""
    from database import init_db
    init_db()

    conn = get_db()
    total = 0

    if material_csv and os.path.exists(material_csv):
        n = import_material(material_csv, conn)
        total += n
    else:
        if material_csv:
            print(f"SOUBOR NENALEZEN: {material_csv}")

    if vhw_csv and os.path.exists(vhw_csv):
        n = import_vhw(vhw_csv, conn)
        total += n
    else:
        if vhw_csv:
            print(f"SOUBOR NENALEZEN: {vhw_csv}")

    conn.close()
    print(f"\nImport dokončen. Celkem importováno: {total} záznamů.")
    return total


if __name__ == '__main__':
    # Výchozí cesty - lze přepsat argumenty
    base = os.path.dirname(__file__)
    mat_csv = sys.argv[1] if len(sys.argv) > 1 else os.path.join(base, 'data', 'material.csv')
    vhw_csv = sys.argv[2] if len(sys.argv) > 2 else os.path.join(base, 'data', 'vhw.csv')
    run_import(mat_csv, vhw_csv)
