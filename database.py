"""
Flight Case výrobní systém - Databázové schéma a pomocné funkce
"""
import sqlite3
import os

# Na Railway je persistent volume mountovaný na /data
# Lokálně používáme složku data/ vedle app.py
if os.path.isdir('/data'):
    DB_PATH = '/data/system.db'
else:
    DB_PATH = os.path.join(os.path.dirname(__file__), 'data', 'system.db')

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = DELETE")
    return conn

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    c = conn.cursor()

    # ── MATERIÁLY ─────────────────────────────────────────────────────────
    c.execute("""
    CREATE TABLE IF NOT EXISTS materialy (
        kod             TEXT PRIMARY KEY,
        nazev           TEXT NOT NULL,
        typ             TEXT,           -- DESKA, PROFIL AL, HW KOULE, PÉNA, ...
        druh            TEXT,           -- PŘEKLIŽKA, L PROFIL, KULATÁ, ...
        umisteni        TEXT,           -- umístění v regálu
        hmotnost        REAL DEFAULT 0,
        nity            REAL DEFAULT 0, -- počet nýtů na kus (z importu MATERIAL, sloupec I)
        balenf          REAL DEFAULT 1, -- balení m2/m/l
        nakup_baleni    REAL DEFAULT 0, -- nákupní cena za balení
        nakup_jednotka  REAL DEFAULT 0, -- nákupní cena za jednotku
        nc_bez_dph      REAL DEFAULT 0, -- NC bez DPH
        cas_s           REAL DEFAULT 0, -- čas zpracování v sekundách
        master_baleni   INTEGER DEFAULT 1,
        dodavatel       TEXT,
        dodaci_lhuta    INTEGER DEFAULT 14,
        sirka_hw        INTEGER,
        priorita        TEXT DEFAULT 'Střední',
        zobrazovat      INTEGER DEFAULT 1,
        oblibeny        INTEGER DEFAULT 0, -- 1 = oblíbený / prioritní materiál (hvězdička)
        poznamka        TEXT,
        web_url         TEXT,
        created_at      TEXT DEFAULT (datetime('now')),
        updated_at      TEXT DEFAULT (datetime('now'))
    )
    """)

    # ── TYPY CASŮ ─────────────────────────────────────────────────────────
    c.execute("""
    CREATE TABLE IF NOT EXISTS typy_casu (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        hn_cislo        TEXT UNIQUE NOT NULL,  -- HN221250 apod.
        nazev           TEXT NOT NULL,
        typ_korpusu     TEXT,           -- Rack, Klávesy, Hlava/kombo, Accessory case, ...
        vnitrni_sirka   INTEGER,        -- mm
        vnitrni_vyska   INTEGER,        -- mm
        vnitrni_hloubka INTEGER,        -- mm
        cena_dilu       REAL DEFAULT 0, -- automaticky z kusovníku
        cena_vyroby     REAL DEFAULT 0, -- manuálně nastavená cena
        cas_narocnost   REAL DEFAULT 0, -- v hodinách
        vyrobeno_ks     INTEGER DEFAULT 0,
        aktivni         INTEGER DEFAULT 1,  -- 0 = zhasnuto
        poznamka        TEXT,
        created_at      TEXT DEFAULT (datetime('now')),
        updated_at      TEXT DEFAULT (datetime('now'))
    )
    """)

    # ── KUSOVNÍK (BOM) ────────────────────────────────────────────────────
    c.execute("""
    CREATE TABLE IF NOT EXISTS kusovniky (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        typ_casu_id     INTEGER NOT NULL REFERENCES typy_casu(id) ON DELETE CASCADE,
        material_kod    TEXT NOT NULL REFERENCES materialy(kod),
        mnozstvi        REAL NOT NULL DEFAULT 0,
        UNIQUE(typ_casu_id, material_kod)
    )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_kus_typ ON kusovniky(typ_casu_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_kus_mat ON kusovniky(material_kod)")

    # ── MATERIÁL – ZÁVISLOSTI (spojovací materiál) ───────────────────────
    c.execute("""
    CREATE TABLE IF NOT EXISTS material_spojeniky (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        material_kod      TEXT NOT NULL REFERENCES materialy(kod) ON DELETE CASCADE,
        spojovaci_kod     TEXT NOT NULL REFERENCES materialy(kod) ON DELETE CASCADE,
        mnozstvi_na_kus   REAL NOT NULL DEFAULT 1,
        UNIQUE(material_kod, spojovaci_kod)
    )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_spoj_mat ON material_spojeniky(material_kod)")

    # ── SKLAD – AKTUÁLNÍ STAVY ────────────────────────────────────────────
    c.execute("""
    CREATE TABLE IF NOT EXISTS sklad (
        material_kod        TEXT PRIMARY KEY REFERENCES materialy(kod),
        naskladneno         REAL DEFAULT 0,
        pouzito             REAL DEFAULT 0,
        skutecny_stav       REAL DEFAULT 0,   -- fyzicky v regálu
        min_skladem         REAL DEFAULT 0,
        posledni_inventura  TEXT,
        updated_at          TEXT DEFAULT (datetime('now'))
    )
    """)

    # ── POHYBY SKLADU ─────────────────────────────────────────────────────
    c.execute("""
    CREATE TABLE IF NOT EXISTS pohyby_skladu (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        material_kod    TEXT NOT NULL REFERENCES materialy(kod),
        typ             TEXT NOT NULL,  -- prijem / vydej / inventura / korekce
        mnozstvi        REAL NOT NULL,
        datum           TEXT NOT NULL DEFAULT (date('now')),
        zakazka_id      INTEGER REFERENCES zakazky(id),
        poznamka        TEXT,
        uzivatel        TEXT,
        created_at      TEXT DEFAULT (datetime('now'))
    )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_poh_mat ON pohyby_skladu(material_kod)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_poh_dat ON pohyby_skladu(datum)")

    # ── VÝROBNÍ ZAKÁZKY ───────────────────────────────────────────────────
    c.execute("""
    CREATE TABLE IF NOT EXISTS zakazky (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        typ_casu_id     INTEGER REFERENCES typy_casu(id),
        hn_cislo        TEXT,           -- kopie z typy_casu nebo vlastní
        nazev           TEXT,
        stav            TEXT DEFAULT 'Čeká',  -- Čeká/Výroba/Hotovo/Expedováno/Zrušeno
        pocet_ks        INTEGER DEFAULT 1,
        termin          TEXT,
        zakaznik        TEXT,
        poznamka_dilna  TEXT,
        poznamka_cnc    TEXT,
        pracovnik       TEXT,
        sn_cislo        TEXT,           -- sériové číslo
        faktura_cislo   TEXT,
        faktura_datum   TEXT,
        datum_zapsani   TEXT DEFAULT (date('now')),
        datum_dokonceni TEXT,
        sklad_odepsano  INTEGER DEFAULT 0,  -- 1 = materiál odepsán ze skladu
        created_at      TEXT DEFAULT (datetime('now')),
        updated_at      TEXT DEFAULT (datetime('now'))
    )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_zak_stav ON zakazky(stav)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_zak_hn ON zakazky(hn_cislo)")

    # ── INVENTURY ─────────────────────────────────────────────────────────
    c.execute("""
    CREATE TABLE IF NOT EXISTS inventury (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        datum           TEXT NOT NULL DEFAULT (date('now')),
        nazev           TEXT,           -- např. "Inventura Q1 2026"
        stav            TEXT DEFAULT 'probíhá',  -- probíhá/dokončena
        uzivatel        TEXT,
        poznamka        TEXT,
        created_at      TEXT DEFAULT (datetime('now'))
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS inventura_polozky (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        inventura_id    INTEGER NOT NULL REFERENCES inventury(id) ON DELETE CASCADE,
        material_kod    TEXT NOT NULL REFERENCES materialy(kod),
        stav_pred       REAL,
        stav_fyzicky    REAL,
        rozdil          REAL GENERATED ALWAYS AS (stav_fyzicky - stav_pred) STORED,
        poznamka        TEXT
    )
    """)

    # ── DODAVATELÉ ────────────────────────────────────────────────────────────
    c.execute("""
    CREATE TABLE IF NOT EXISTS dodavatele (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        nazev           TEXT NOT NULL,
        zkratka         TEXT,
        kontakt_jmeno   TEXT,
        email           TEXT,
        telefon         TEXT,
        web             TEXT,
        adresa          TEXT,
        ic              TEXT,
        dic             TEXT,
        splatnost_dni   INTEGER DEFAULT 14,
        dodaci_lhuta_dni INTEGER DEFAULT 14,
        mena            TEXT DEFAULT 'CZK',
        poznamka        TEXT,
        aktivni         INTEGER DEFAULT 1,
        created_at      TEXT DEFAULT (datetime('now')),
        updated_at      TEXT DEFAULT (datetime('now'))
    )
    """)

    # ── PŘÍJEMKY (naskladnění dávkou) ─────────────────────────────────────────
    c.execute("""
    CREATE TABLE IF NOT EXISTS prijemky (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        cislo           TEXT,               -- číslo dodacího listu / faktury
        dodavatel_id    INTEGER REFERENCES dodavatele(id),
        datum           TEXT NOT NULL DEFAULT (date('now')),
        stav            TEXT DEFAULT 'rozpracováno',  -- rozpracováno/zaúčtováno
        poznamka        TEXT,
        uzivatel        TEXT,
        created_at      TEXT DEFAULT (datetime('now'))
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS prijemky_polozky (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        prijemka_id     INTEGER NOT NULL REFERENCES prijemky(id) ON DELETE CASCADE,
        material_kod    TEXT NOT NULL REFERENCES materialy(kod),
        mnozstvi        REAL NOT NULL,
        cena_jednotka   REAL DEFAULT 0,     -- nákupní cena za jednotku
        cena_celkem     REAL DEFAULT 0
    )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_prij_dod ON prijemky(dodavatel_id)")

    # ── ŘEZNÝ PLÁN PROFILŮ ────────────────────────────────────────────────────
    c.execute("""
    CREATE TABLE IF NOT EXISTS profily_plan (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        typ_casu_id  INTEGER NOT NULL REFERENCES typy_casu(id) ON DELETE CASCADE,
        typ_profilu  TEXT NOT NULL,    -- 'L' nebo 'H'
        poradi       INTEGER NOT NULL, -- pořadí řádku (1-15)
        ks           INTEGER DEFAULT 0,
        rozmer_mm    REAL,
        zakonceni    TEXT,             -- pro H: '/ |', '| \\', '| |' atd.
        zarázka1     REAL,             -- Fáze 2 – vzorec pily
        zarázka2     REAL,             -- Fáze 2
        UNIQUE(typ_casu_id, typ_profilu, poradi)
    )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_prof_typ ON profily_plan(typ_casu_id)")

    # ── PRACOVNÍ POSTUPY – ODKAZY ─────────────────────────────────────────────
    c.execute("""
    CREATE TABLE IF NOT EXISTS typy_casu_links (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        typ_casu_id INTEGER NOT NULL REFERENCES typy_casu(id) ON DELETE CASCADE,
        nazev       TEXT NOT NULL,
        url         TEXT NOT NULL,
        poradi      INTEGER DEFAULT 0
    )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_links_typ ON typy_casu_links(typ_casu_id)")

    # ── FAKTURY ───────────────────────────────────────────────────────────────
    c.execute("""
    CREATE TABLE IF NOT EXISTS faktury (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        cislo               TEXT UNIQUE NOT NULL,           -- "11526049"
        datum_vystaveni     TEXT NOT NULL,                  -- ISO date
        datum_splatnosti    TEXT NOT NULL,                  -- vystaveni + 14 dní
        datum_plneni        TEXT NOT NULL,                  -- = datum_vystaveni
        var_symbol          TEXT,
        vystavil            TEXT DEFAULT 'Kateřina Otradovcová',
        stav                TEXT DEFAULT 'vydána',          -- vydána/zaplacena/storno
        celkem_bez_dph      REAL DEFAULT 0,
        celkem_dph          REAL DEFAULT 0,
        celkem_s_dph        REAL DEFAULT 0,
        poznamka            TEXT,
        created_at          TEXT DEFAULT (datetime('now'))
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS faktury_polozky (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        faktura_id              INTEGER NOT NULL REFERENCES faktury(id) ON DELETE CASCADE,
        zakazka_id              INTEGER REFERENCES zakazky(id),
        hn_cislo                TEXT,
        nazev                   TEXT NOT NULL,
        ks                      INTEGER DEFAULT 1,
        cena_dilu_snapshot      REAL DEFAULT 0,
        cena_vyroby_snapshot    REAL DEFAULT 0,
        cena_za_mj              REAL NOT NULL,              -- (dilu+vyroby) × 1.047
        sazba_dph               REAL DEFAULT 21,
        zaklad                  REAL NOT NULL,              -- cena_za_mj × ks
        dph                     REAL NOT NULL,              -- zaklad × 0.21
        celkem_s_dph            REAL NOT NULL               -- zaklad + dph
    )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_fak_pol ON faktury_polozky(faktura_id)")

    # ── PROŘEZ – ZTRÁTY MATERIÁLU ─────────────────────────────────────────────
    c.execute("""
    CREATE TABLE IF NOT EXISTS prorez (
        typ         TEXT PRIMARY KEY,   -- odpovídá materialy.typ (DESKA, PROFIL AL, ...)
        procento    REAL DEFAULT 0      -- 0–100, přičítá se při odpisu ze skladu
    )
    """)

    # ── UŽIVATELÉ ─────────────────────────────────────────────────────────────
    c.execute("""
    CREATE TABLE IF NOT EXISTS uzivatele (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        jmeno       TEXT NOT NULL,
        role        TEXT NOT NULL DEFAULT 'Dílna',  -- Admin/Dílna/CNC/Kancelář/Projektant
        barva       TEXT DEFAULT '#3b82f6',          -- barva avataru
        aktivni     INTEGER DEFAULT 1,
        created_at  TEXT DEFAULT (datetime('now'))
    )
    """)

    # ── KANCELÁŘ – ŠTÍTKY ─────────────────────────────────────────────────────
    c.execute("""
    CREATE TABLE IF NOT EXISTS kancelar_stitky (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        nazev   TEXT NOT NULL UNIQUE,
        barva   TEXT DEFAULT '#e5e7eb',
        poradi  INTEGER DEFAULT 0,
        aktivni INTEGER DEFAULT 1
    )
    """)

    # ── KANCELÁŘ – ZAKÁZKY ────────────────────────────────────────────────────
    c.execute("""
    CREATE TABLE IF NOT EXISTS kancelar_zakazky (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        nazev               TEXT NOT NULL,
        zakaznik            TEXT,
        popis               TEXT,
        resitel_id          INTEGER REFERENCES uzivatele(id),
        vyrobni_zakazka_id  INTEGER REFERENCES zakazky(id),
        priorita            TEXT DEFAULT 'Střední',
        termin              TEXT,
        created_at          TEXT DEFAULT (datetime('now')),
        updated_at          TEXT DEFAULT (datetime('now'))
    )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_kan_res ON kancelar_zakazky(resitel_id)")

    # ── KANCELÁŘ – ZAKÁZKY ↔ ŠTÍTKY (M:N) ────────────────────────────────────
    c.execute("""
    CREATE TABLE IF NOT EXISTS kancelar_zakazky_stitky (
        zakazka_id  INTEGER NOT NULL REFERENCES kancelar_zakazky(id) ON DELETE CASCADE,
        stitek_id   INTEGER NOT NULL REFERENCES kancelar_stitky(id)  ON DELETE CASCADE,
        PRIMARY KEY (zakazka_id, stitek_id)
    )
    """)

    # ── KANCELÁŘ – ZÁKAZNÍCI (číselník) ───────────────────────────────────────
    c.execute("""
    CREATE TABLE IF NOT EXISTS kancelar_zakaznici (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        nazev      TEXT NOT NULL,
        tel        TEXT,
        mail       TEXT,
        poznamka   TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    )
    """)

    # ── KANCELÁŘ – STAV HOTOVO (číselník) ─────────────────────────────────────
    c.execute("""
    CREATE TABLE IF NOT EXISTS kancelar_stav_hotovo (
        id     INTEGER PRIMARY KEY AUTOINCREMENT,
        nazev  TEXT NOT NULL UNIQUE,
        poradi INTEGER DEFAULT 0
    )
    """)
    for i, nazev in enumerate(['Objednávka', 'Částečně BOM', 'Úplné BOM']):
        c.execute("INSERT OR IGNORE INTO kancelar_stav_hotovo (nazev, poradi) VALUES (?,?)", (nazev, i))

    # ── KANCELÁŘ – POZNÁMKY (nekonečný zápisník) ──────────────────────────────
    c.execute("""
    CREATE TABLE IF NOT EXISTS kancelar_poznamky (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        zakazka_id INTEGER NOT NULL REFERENCES kancelar_zakazky(id) ON DELETE CASCADE,
        obsah      TEXT,
        uzivatel   TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    )
    """)

    # ── KANCELÁŘ – PŘÍLOHY (soubory) ──────────────────────────────────────────
    c.execute("""
    CREATE TABLE IF NOT EXISTS kancelar_prilohy (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        zakazka_id INTEGER NOT NULL REFERENCES kancelar_zakazky(id) ON DELETE CASCADE,
        filename   TEXT NOT NULL,
        filepath   TEXT NOT NULL,
        mime_type  TEXT,
        velikost   INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now'))
    )
    """)

    # ── PARAMETRY VÝPOČTU ČASŮ VÝROBY ────────────────────────────────────────
    c.execute("""
    CREATE TABLE IF NOT EXISTS cas_parametry (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        sekce   TEXT NOT NULL,   -- 'CNC', 'Montaz', 'Peny'
        klic    TEXT NOT NULL,
        hodnota REAL NOT NULL DEFAULT 0,
        popis   TEXT,
        UNIQUE(sekce, klic)
    )
    """)
    # Výchozí parametry – vloženy jen pokud ještě neexistují
    _cas_defaults = [
        # CNC sekce
        ('CNC', 'setup',            900,  'Přinesení desek a příprava prostoru CNC (s)'),
        ('CNC', 'data_prep',        600,  'Příprava dat a programu CNC (s)'),
        ('CNC', 'fix_per_mat',       60,  'Pevný overhead na každý unikátní materiál DESKA (s)'),
        ('CNC', 'ref_sirka',        600,  'Referenční šířka case pro size-factor (mm)'),
        ('CNC', 'ref_hloubka',      500,  'Referenční hloubka case pro size-factor (mm)'),
        ('CNC', 'deska_default_s',  180,  'Záložní čas CNC na 1 ks DESKY, pokud není cas_s nastaven (s)'),
        # Montáž sekce
        ('Montaz', 'setup',         600,  'Sbírání dílů a příprava pracoviště montáže (s)'),
        ('Montaz', 'cleanup',       300,  'Úklid pracoviště po montáži (s)'),
        ('Montaz', 'kontrola',      180,  'Závěrečná kontrola kvality (s)'),
        ('Montaz', 'hw_default_s',   30,  'Záložní čas montáže na 1 ks HW, pokud není cas_s nastaven (s)'),
        ('Montaz', 'cas_na_nyt',     15,  'Čas na montáž 1 nýtu (s)'),
        ('Montaz', 'ref_vyska',     350,  'Referenční vnitřní výška case pro handling-factor (mm)'),
        # Pěny sekce
        ('Peny', 'pistole',         180,  'Nahřívání lepicí pistole (s)'),
        ('Peny', 'cleanup',         120,  'Úklid po práci s pěnami (s)'),
        ('Peny', 'fix_session',      60,  'Pevný čas na zahájení práce s pěnami (s)'),
        ('Peny', 'pena_default_s',  240,  'Záložní čas na 1 ks PĚNY, pokud není cas_s nastaven (s)'),
    ]
    for sekce, klic, hodnota, popis in _cas_defaults:
        c.execute(
            "INSERT OR IGNORE INTO cas_parametry (sekce, klic, hodnota, popis) VALUES (?,?,?,?)",
            (sekce, klic, hodnota, popis)
        )

    # ── OPRAVNÉ DOKLADY (Manko / Přebytek) ───────────────────────────────────
    c.execute("""
    CREATE TABLE IF NOT EXISTS opravne_doklady (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        datum         TEXT NOT NULL DEFAULT (date('now')),
        typ           TEXT NOT NULL,       -- 'manko' nebo 'prebytek'
        material_kod  TEXT NOT NULL REFERENCES materialy(kod),
        mnozstvi      REAL NOT NULL,       -- vždy kladné
        cena_bez_dph  REAL NOT NULL DEFAULT 0,
        poznamka      TEXT,
        uzivatel      TEXT,
        created_at    TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_od_mat ON opravne_doklady(material_kod)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_od_dat ON opravne_doklady(datum)")

    # ── FIFO DÁVKY ────────────────────────────────────────────────────────────
    c.execute("""
    CREATE TABLE IF NOT EXISTS fifo_davky (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        material_kod    TEXT NOT NULL REFERENCES materialy(kod),
        datum_prijmu    TEXT NOT NULL,
        mnozstvi_orig   REAL NOT NULL,
        mnozstvi_zbyla  REAL NOT NULL,
        cena_jednotka   REAL DEFAULT 0,
        dodavatel       TEXT,
        faktura         TEXT,
        je_inventura    INTEGER DEFAULT 0,
        poznamka        TEXT,
        zruseno         INTEGER DEFAULT 0,
        created_at      TEXT DEFAULT (datetime('now'))
    )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_fifo_mat ON fifo_davky(material_kod)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_fifo_dat ON fifo_davky(datum_prijmu)")

    conn.commit()
    conn.close()
    print(f"Databáze inicializována: {DB_PATH}")


# ── POMOCNÉ FUNKCE ────────────────────────────────────────────────────────

def aktualizuj_stav_skladu(conn, material_kod):
    """Přepočítá stav skladu z pohybů"""
    c = conn.cursor()
    c.execute("""
        UPDATE sklad SET
            naskladneno = COALESCE((SELECT SUM(mnozstvi) FROM pohyby_skladu WHERE material_kod=? AND typ='prijem'),0),
            pouzito     = COALESCE((SELECT SUM(mnozstvi) FROM pohyby_skladu WHERE material_kod=? AND typ='vydej'),0),
            updated_at  = datetime('now')
        WHERE material_kod = ?
    """, (material_kod, material_kod, material_kod))

def vypocti_cenu_dilu(conn, typ_casu_id):
    """Spočítá celkovou cenu dílů z kusovníku (včetně individuálního nebo globálního prořezu).
    Individuální prořez materiálu (materialy.prorez_procento) má přednost před globálním dle typu."""
    c = conn.cursor()
    c.execute("""
        SELECT COALESCE(SUM(
            k.mnozstvi
            * (1.0 + COALESCE(m.prorez_procento, p.procento, 0) / 100.0)
            * m.nc_bez_dph
        ), 0) as cena
        FROM kusovniky k
        JOIN materialy m ON m.kod = k.material_kod
        LEFT JOIN prorez p ON p.typ = m.typ
        WHERE k.typ_casu_id = ?
    """, (typ_casu_id,))
    row = c.fetchone()
    cena = row['cena'] if row else 0
    c.execute("UPDATE typy_casu SET cena_dilu=?, updated_at=datetime('now') WHERE id=?", (cena, typ_casu_id))
    return cena

def zkontroluj_dostupnost_materialu(conn, typ_casu_id, pocet_ks=1):
    """Zkontroluje, zda je dost materiálu pro výrobu X kusů"""
    c = conn.cursor()
    c.execute("""
        SELECT k.material_kod, m.nazev, k.mnozstvi * ? as potreba,
               COALESCE(s.skutecny_stav, s.naskladneno - s.pouzito, 0) as disponibilni
        FROM kusovniky k
        JOIN materialy m ON m.kod = k.material_kod
        LEFT JOIN sklad s ON s.material_kod = k.material_kod
        WHERE k.typ_casu_id = ?
    """, (pocet_ks, typ_casu_id))
    items = c.fetchall()
    chybi = [dict(i) for i in items if i['disponibilni'] < i['potreba']]
    return chybi

def odepis_material_ze_skladu(conn, zakazka_id):
    """Odepíše materiál z kusovníku zakázky ze skladu.
    Ke každé položce přičte prořez dle typu materiálu (tabulka prorez).
    V pohybu skladu se ukládá skutečně odepsané množství (včetně prořezu).
    BOM a výrobní listy zobrazují vždy přesné hodnoty z kusovníku.
    """
    c = conn.cursor()
    c.execute("SELECT typ_casu_id, pocet_ks FROM zakazky WHERE id=?", (zakazka_id,))
    zak = c.fetchone()
    if not zak or not zak['typ_casu_id']:
        return False

    # Načti kusovník + typ materiálu + prořez najednou
    # Individuální prořez materiálu (m.prorez_procento) má přednost před globálním dle typu (p.procento)
    c.execute("""
        SELECT k.material_kod,
               k.mnozstvi * ? AS mnozstvi_bom,
               m.typ,
               COALESCE(m.prorez_procento, p.procento, 0) AS prorez_pct
        FROM kusovniky k
        JOIN materialy m ON m.kod = k.material_kod
        LEFT JOIN prorez p ON p.typ = m.typ
        WHERE k.typ_casu_id = ?
    """, (zak['pocet_ks'], zak['typ_casu_id']))
    polozky = c.fetchall()

    for p in polozky:
        koeficient = 1.0 + (p['prorez_pct'] / 100.0)
        mnozstvi_odpis = round(p['mnozstvi_bom'] * koeficient, 6)
        poznamka = 'Automatický odpis ze zakázky'
        if p['prorez_pct'] > 0:
            poznamka += f' + prořez {p["prorez_pct"]} % ({p["typ"]})'
        c.execute("""
            INSERT INTO pohyby_skladu (material_kod, typ, mnozstvi, zakazka_id, poznamka)
            VALUES (?, 'vydej', ?, ?, ?)
        """, (p['material_kod'], mnozstvi_odpis, zakazka_id, poznamka))
        aktualizuj_stav_skladu(conn, p['material_kod'])

    c.execute("UPDATE zakazky SET sklad_odepsano=1 WHERE id=?", (zakazka_id,))
    conn.commit()
    return True


def auto_migrate():
    """Bezpečné migrace spouštěné automaticky při každém startu serveru.
    Přidává nové sloupce a tabulky — nikdy nemaže existující data.
    """
    conn = get_db()
    c = conn.cursor()
    log = []

    def add_column(table, col, definition):
        existing = {row[1] for row in c.execute(f"PRAGMA table_info({table})")}
        if col not in existing:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
            log.append(f"  [OK] {table}.{col}")

    # typy_casu – rozšíření
    add_column('typy_casu', 'orientace_lid',   'TEXT')
    add_column('typy_casu', 'pena_poznamka',   'TEXT')
    add_column('typy_casu', 'pena_odkaz',      'TEXT')
    add_column('typy_casu', 'prisl_1',         'TEXT')
    add_column('typy_casu', 'prisl_2',         'TEXT')
    add_column('typy_casu', 'prisl_3',         'TEXT')
    add_column('typy_casu', 'prisl_4',         'TEXT')
    add_column('typy_casu', 'typ_poznamka',    'TEXT')
    add_column('typy_casu', 'hmotnost',          'REAL DEFAULT 0')
    add_column('typy_casu', 'prodej_ap_bez_dph', 'REAL DEFAULT 0')
    add_column('typy_casu', 'cas_narocnost',     'REAL DEFAULT 0')
    add_column('typy_casu', 'cena_dilu',         'REAL DEFAULT 0')
    add_column('typy_casu', 'spravna_mc',        'REAL DEFAULT 0')  # Správná maloobchodní cena (z importu)
    add_column('typy_casu', 'viceprace_kompletace_s', 'INTEGER DEFAULT 0')  # Vícepráce – kompletace (empirická korekce)
    add_column('typy_casu', 'viceprace_peny_s',       'INTEGER DEFAULT 0')  # Vícepráce – pěny (empirická korekce)

    # materialy – web odkaz
    add_column('materialy', 'web_url', 'TEXT')

    # zakazky – fakturace + přiřazení + priorita
    add_column('zakazky', 'fakturovano',         'INTEGER DEFAULT 0')
    add_column('zakazky', 'prioritni',           'INTEGER DEFAULT 0')
    add_column('zakazky', 'foceni',              'INTEGER DEFAULT 0')
    add_column('zakazky', 'odeslano_do_vyroby',  'INTEGER DEFAULT 0')  # 1 = zobrazit v Dílně
    add_column('zakazky', 'destinace',           "TEXT DEFAULT 'Zákazník'")  # Zákazník / Sklad
    add_column('zakazky', 'poznamka_cnc_operator', 'TEXT')  # Editovatelná poznámka operátora CNC (co ještě chybí nařezat)

    # prijemky – dopravné + měna
    add_column('prijemky', 'dopravne',     'REAL DEFAULT 0')
    add_column('prijemky', 'mena',         "TEXT DEFAULT 'CZK'")
    add_column('prijemky', 'kurz',         'REAL DEFAULT 1.0')

    # pohyby_skladu – vazba na příjemku / opravný doklad
    add_column('pohyby_skladu', 'prijemka_id',       'INTEGER')
    add_column('pohyby_skladu', 'opravny_doklad_id',  'INTEGER')

    # materialy – individuální prořez materiálu (NULL = použij globální dle typu z tabulky prorez)
    add_column('materialy', 'prorez_procento', 'REAL DEFAULT NULL')

    # materialy – počet nýtů z importu
    add_column('materialy', 'nity', 'REAL DEFAULT 0')

    # materialy – oblíbený příznak (hvězdička)
    add_column('materialy', 'oblibeny', 'INTEGER DEFAULT 0')

    # kancelar_zakazky – nová pole
    add_column('kancelar_zakazky', 'tel',         'TEXT')
    add_column('kancelar_zakazky', 'mail',        'TEXT')
    add_column('kancelar_zakazky', 'hn_kod',      'TEXT')
    add_column('kancelar_zakazky', 'co_hotovo',   'TEXT')
    add_column('kancelar_zakazky', 'aktivni',     'INTEGER DEFAULT 1')
    add_column('kancelar_zakazky', 'zakaznik_id', 'INTEGER')
    add_column('kancelar_zakazky', 'nabidka_id',  'INTEGER REFERENCES nabidky(id) ON DELETE SET NULL')

    # odchylky_karty – hlášení odchylek z dílny
    c.execute("""
        CREATE TABLE IF NOT EXISTS odchylky_karty (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            zakazka_id  INTEGER REFERENCES zakazky(id) ON DELETE SET NULL,
            typ_casu_id INTEGER REFERENCES typy_casu(id) ON DELETE SET NULL,
            hn_cislo    TEXT,
            text        TEXT NOT NULL,
            stav        TEXT DEFAULT 'Nová',  -- Nová / Vyřešeno
            created_at  TEXT DEFAULT (datetime('now')),
            vyreseno_at TEXT
        )
    """)

    # cas_parametry – tabulka parametrů výpočtu časů
    c.execute("""
        CREATE TABLE IF NOT EXISTS cas_parametry (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            sekce   TEXT NOT NULL,
            klic    TEXT NOT NULL,
            hodnota REAL NOT NULL DEFAULT 0,
            popis   TEXT,
            UNIQUE(sekce, klic)
        )
    """)
    _cas_defaults = [
        ('CNC',    'setup',            900,  'Přinesení desek a příprava prostoru CNC (s)'),
        ('CNC',    'data_prep',        600,  'Příprava dat a programu CNC (s)'),
        ('CNC',    'fix_per_mat',       60,  'Pevný overhead na každý unikátní materiál DESKA (s)'),
        ('CNC',    'ref_sirka',        600,  'Referenční šířka case pro size-factor (mm)'),
        ('CNC',    'ref_hloubka',      500,  'Referenční hloubka case pro size-factor (mm)'),
        ('CNC',    'deska_default_s',  180,  'Záložní čas CNC na 1 ks DESKY, pokud není cas_s nastaven (s)'),
        ('Montaz', 'setup',            600,  'Sbírání dílů a příprava pracoviště montáže (s)'),
        ('Montaz', 'cleanup',          300,  'Úklid pracoviště po montáži (s)'),
        ('Montaz', 'kontrola',         180,  'Závěrečná kontrola kvality (s)'),
        ('Montaz', 'hw_default_s',      30,  'Záložní čas montáže na 1 ks HW, pokud není cas_s nastaven (s)'),
        ('Montaz', 'cas_na_nyt',        15,  'Čas na montáž 1 nýtu (s)'),
        ('Montaz', 'ref_vyska',        350,  'Referenční vnitřní výška case pro handling-factor (mm)'),
        ('Peny',   'pistole',          180,  'Nahřívání lepicí pistole (s)'),
        ('Peny',   'cleanup',          120,  'Úklid po práci s pěnami (s)'),
        ('Peny',   'fix_session',       60,  'Pevný čas na zahájení práce s pěnami (s)'),
        ('Peny',   'pena_default_s',   240,  'Záložní čas na 1 ks PĚNY, pokud není cas_s nastaven (s)'),
        ('Ceny',   'sazba_prace',      300,  'Hodinová sazba práce pro výpočet správné MC (Kč/h)'),
        ('Ceny',   'koeficient_mc',    2.2,  'Prodejní koeficient – náklady × koeficient = cena bez DPH'),
        ('Ceny',   'dph',               21,  'Sazba DPH (%)'),
        # ── Workflow montáže ─────────────────────────────────────────────────
        # Krok 1: Orientace v dokumentaci
        ('Priprava', 'orientace_jednoducha_s',  60, 'Orientace – jednoduchý case (≤ mez desek): čas v sekundách'),
        ('Priprava', 'orientace_slozita_s',    180, 'Orientace – složitý case (> mez desek): čas v sekundách'),
        ('Priprava', 'orientace_mez_desek',     10, 'Hranice počtu desek: ≤ tato hodnota = jednoduchý case'),
        # Krok 2: Přinesení profilů
        ('Priprava', 'noseni_profilu_s',       120, 'Přinesení a příprava profilů: čas v sekundách'),
        # Krok 3: Řezání profilů – FUSION/R1 (kódy Q* nebo NE*)
        ('Rezani', 'cas_fusion_ks_s',           25, 'FUSION/R1: čas na 1 řez (1 profil kolmý řez) v sekundách'),
        # Krok 3: Řezání profilů – standardní (ostatní kódy)
        ('Rezani', 'cas_L_uniq_s',              60, 'Standardní L profil: čas na 1 unikátní rozměr (4 ks naráz) v sekundách'),
        ('Rezani', 'cas_H_uniq_s',             110, 'Hybrid H profil: čas na 1 unikátní rozměr v sekundách (zastaralé)'),
        ('Rezani', 'cas_H_par_s',               60, 'Hybrid H profil: čas na 1 pár (2 ks naráz) v sekundách'),
        # Krok 3: Sražení hybridů u motýlových zámků
        ('Rezani', 'cas_motyl_zkos_s',          40, 'Sražení hybridu na pile: čas na 1 motýlový zámek v sekundách'),
        # Krok 3: FUSION Q6504 + motýlové zámky → vyřezání zámků do profilů na pile
        ('Rezani', 'cas_fusion_q6504_motyl_s', 240, 'Vyřezání motýlových zámků do FUSION Q6504 profilů na pile: čas v sekundách'),
        # Krok 4: Děrování L profilů (ne FUSION/R1, jen delší než min_delka_mm)
        ('Derovani', 'cas_nastaveni_pravitka_s', 10, 'Přenastavení pravítka děrovačky: čas na 1 unikátní rozměr v sekundách'),
        ('Derovani', 'cas_na_profil_s',          15, 'Děrování: čas na 1 kus L profilu v sekundách'),
        ('Derovani', 'min_delka_mm',            128, 'Minimální délka profilu pro děrování – kratší se přeskočí (mm)'),
        # Krok 5: Natírání otočných podvozků (typ=PODVOZEK, druh=OTOČNÉ)
        ('Podvozky', 'cas_na_kolo_s',           120, 'Natírání otočného kolečka: čas na 1 kus v sekundách'),
        # Krok 5: Broušení hybrid hliníků (jen pokud jsou pěny a není FUSION/R1)
        ('BrouseniHybrid', 'cas_na_hybrid_s',   10, 'Broušení H profilu: čas na 1 kus v sekundách'),
        # Krok 10b: Broušení desek před lepením pěn (fenol desky ≤10mm + pěny ≤20mm)
        ('BrouseniDesek', 'max_tloustka_desky_mm', 10, 'Broušení desek: max tloušťka fenol desky [mm]'),
        ('BrouseniDesek', 'max_tloustka_peny_mm',  20, 'Broušení desek: max tloušťka pěny zahrnuté do plochy [mm]'),
        ('BrouseniDesek', 'cas_per_m2_s',          45, 'Broušení desek: čas na 1 m² plochy pěny [s]'),
        # Krok 6: Můstky – frézování odpadků z desek (jen desky ≤ max_tloustka_mm)
        ('Mustky', 'max_tloustka_mm',            10, 'Maximální tloušťka desky s můstky (mm) – silnější překližky jsou bez můstků'),
        ('Mustky', 'cas_per_deska_s',            20, 'Čas na 1 desku z DXF (s můstky) v sekundách'),
        ('Mustky', 'cas_bom_fallback_s',        120, 'Záložní čas na 1 case (bez DXF, jen z BOM) v sekundách'),
        # Krok 7: Sesbírání HW (vše kromě desek, pěn, profilů AL, ostatní)
        ('SbiraniHW', 'cas_per_druh_s',          10, 'Čas na 1 druh HW položky (sesbírání ze skladu) v sekundách'),
        ('SbiraniHW', 'cas_per_ks_s',             2, 'Čas na 1 ks HW (příprava / odkládání) v sekundách'),
        # Krok 8: Sestřílení – základní kompletace pistolí (jen klasické casy)
        ('Sestrileni', 'cas_base_s',             10, 'Základní čas na 1 desku (fixní složka) v sekundách'),
        ('Sestrileni', 'cas_per_m2_s',           40, 'Přídavek za plochu desky: čas na 1 m² v sekundách'),
        # Krok 12: Polep tapetou
        ('Polep', 'cas_polep_s', 3000, 'Čas polepu case tapetou (materiál kód POLEP ART) v sekundách'),
        # Krok 11: Lepení pěn (DXF nebo BOM fallback)
        ('LepeniPen', 'cas_priprava_s',       300, 'Celková příprava před lepením pěn v sekundách'),
        ('LepeniPen', 'cas_priprava_peny_s',   60, 'Příprava na 1 kus pěny v sekundách'),
        ('LepeniPen', 'cas_per_m2_s',         120, 'Čas lepení na 1 m² pěny v sekundách'),
        ('LepeniPen', 'cas_min_s',             30, 'Minimální čas lepení 1 kusu pěny v sekundách'),
        # Koeficienty složitosti (koef_*)
        ('LepeniPen', 'koef_hlava',           1.0, 'Koeficient: Hlava / kombo'),
        ('LepeniPen', 'koef_mixpult',         1.2, 'Koeficient: Mixpult'),
        ('LepeniPen', 'koef_klavesy',         1.2, 'Koeficient: Klávesy'),
        ('LepeniPen', 'koef_rack',            1.0, 'Koeficient: Rack'),
        ('LepeniPen', 'koef_rack_slide',      1.0, 'Koeficient: Rack Sliding door'),
        ('LepeniPen', 'koef_access',          1.0, 'Koeficient: Accessory case'),
        ('LepeniPen', 'koef_pedal',           1.0, 'Koeficient: Pedalboard'),
        ('LepeniPen', 'koef_svetlo',          1.4, 'Koeficient: Case pro světelné hlavy'),
        ('LepeniPen', 'koef_tv',              1.2, 'Koeficient: Case pro TV'),
        ('LepeniPen', 'koef_satna',           1.0, 'Koeficient: Šatní skříň'),
        ('LepeniPen', 'koef_jiny',            1.0, 'Koeficient: Jiný typ'),
        ('LepeniPen', 'koef_inlay',           1.0, 'Koeficient: Inlay'),
        # Fallback počet ks pěn bez DXF (fks_*)
        ('LepeniPen', 'fks_hlava',            10, 'Fallback ks pěn bez DXF: Hlava / kombo'),
        ('LepeniPen', 'fks_mixpult',          16, 'Fallback ks pěn bez DXF: Mixpult'),
        ('LepeniPen', 'fks_klavesy',          16, 'Fallback ks pěn bez DXF: Klávesy'),
        ('LepeniPen', 'fks_rack',              2, 'Fallback ks pěn bez DXF: Rack'),
        ('LepeniPen', 'fks_rack_slide',        2, 'Fallback ks pěn bez DXF: Rack Sliding door'),
        ('LepeniPen', 'fks_access',           10, 'Fallback ks pěn bez DXF: Accessory case'),
        ('LepeniPen', 'fks_pedal',             9, 'Fallback ks pěn bez DXF: Pedalboard'),
        ('LepeniPen', 'fks_svetlo',           30, 'Fallback ks pěn bez DXF: Case pro světelné hlavy'),
        ('LepeniPen', 'fks_tv',               16, 'Fallback ks pěn bez DXF: Case pro TV'),
        ('LepeniPen', 'fks_satna',             2, 'Fallback ks pěn bez DXF: Šatní skříň'),
        ('LepeniPen', 'fks_jiny',             10, 'Fallback ks pěn bez DXF: Jiný typ'),
        ('LepeniPen', 'fks_inlay',             1, 'Fallback ks pěn bez DXF: Inlay'),
        # Krok 10: Rack lišty (PROFIL AL, druh=RACK)
        ('RackListy', 'cas_montaz_listy_s',   120, 'Čas montáže 1 rack lišty v sekundách'),
        ('RackListy', 'cas_guma_listy_s',       30, 'Čas montáže gumového těsnění / 1 lišta v sekundách'),
        ('RackListy', 'rack_unit_vyska_mm',    22.5,'Výška 1 rack unit v mm (standardně 22,5 mm = 1U)'),
        ('RackListy', 'cas_matice_ru_s',         6, 'Čas montáže matic do lišty / 1 rack unit v sekundách'),
        # Krok 9: Kompletace case
        # – Standard: nýtování L profilů + usazení + hybridy + HW
        ('Kompletace', 'std_nit_roztes_mm',      64, 'Dělitel délky L profilu pro počet nýtů (rozteč 128mm × 2 strany = 64)'),
        ('Kompletace', 'std_nitovani_s',          9, 'Čas nýtování / 1 nýt v sekundách'),
        ('Kompletace', 'std_L_min_mm',          128, 'Minimální délka L profilu pro nýtování (kratší se nenýtují)'),
        ('Kompletace', 'std_L_usazeni_s',        15, 'Čas usazení L profilu na case / 1 ks v sekundách'),
        ('Kompletace', 'std_H_usazeni_s',        10, 'Čas usazení H (hybrid) profilu / 1 ks v sekundách'),
        # – FUSION
        ('Kompletace', 'fus_profil_s',          180, 'FUSION: základní čas montáže / 1 kus profilu v sekundách'),
        ('Kompletace', 'fus_per_100mm_s',         3, 'FUSION: přídavek za každých 100 mm délky profilu v sekundách'),
        # – R1 system
        ('Kompletace', 'r1_hliniky_s',          600, 'R1 system: celkový čas kompletace hliníků v sekundách'),
        # – Speciální typy (paušál)
        ('Kompletace', 'panel_6060_s',          3600, 'Akustický panel 60x60 v rámu: celkový čas kompletace v sekundách'),
        ('Kompletace', 'panel_12060_s',         2700, 'Akustický panel 120x60 bez rámu: celkový čas kompletace v sekundách'),
        # Krok 13: Prostoje (zametání, ofukování, odnesení casu atd.)
        ('Prostoje', 'delitel',                  8, 'Dělitel pro výpočet prostojů (součet všech kroků / dělitel)'),
    ]
    for sekce, klic, hodnota, popis in _cas_defaults:
        c.execute(
            "INSERT OR IGNORE INTO cas_parametry (sekce, klic, hodnota, popis) VALUES (?,?,?,?)",
            (sekce, klic, hodnota, popis)
        )
    log.append("  [OK] cas_parametry – defaults seeded (INSERT OR IGNORE)")

    # ── FIFO DÁVKY ────────────────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS fifo_davky (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            material_kod    TEXT NOT NULL REFERENCES materialy(kod),
            datum_prijmu    TEXT NOT NULL,
            mnozstvi_orig   REAL NOT NULL,
            mnozstvi_zbyla  REAL NOT NULL,
            cena_jednotka   REAL DEFAULT 0,
            dodavatel       TEXT,
            faktura         TEXT,
            je_inventura    INTEGER DEFAULT 0,
            poznamka        TEXT,
            zruseno         INTEGER DEFAULT 0,
            created_at      TEXT DEFAULT (datetime('now'))
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_fifo_mat ON fifo_davky(material_kod)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_fifo_dat ON fifo_davky(datum_prijmu)")
    log.append("  [OK] fifo_davky")

    # ── CNC ŘEZÁNÍ – checklist operátora ─────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS cnc_rezani (
            zakazka_id   INTEGER NOT NULL REFERENCES zakazky(id) ON DELETE CASCADE,
            material_kod TEXT NOT NULL,
            rezano       INTEGER DEFAULT 1,
            updated_at   TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (zakazka_id, material_kod)
        )
    """)
    log.append("  [OK] cnc_rezani")

    # ── NABÍDKY ───────────────────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS nabidky (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            nazev           TEXT NOT NULL,
            zakaznik        TEXT NOT NULL,
            email           TEXT,
            tel             TEXT,
            pocet_ks        INTEGER DEFAULT 1,
            hodiny_vyroba   REAL DEFAULT 0,
            hodiny_kresleni REAL DEFAULT 0,
            hodiny_cnc      REAL DEFAULT 0,
            sazba_prace     REAL DEFAULT 300,
            koeficient      REAL DEFAULT 2.2,
            kurz_eur        REAL DEFAULT 25,
            poznamka        TEXT,
            stav            TEXT DEFAULT 'Rozpracovaná',
            created_at      TEXT DEFAULT (datetime('now'))
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS nabidky_import (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            nabidka_id      INTEGER NOT NULL REFERENCES nabidky(id) ON DELETE CASCADE,
            material_kod    TEXT NOT NULL,
            mnozstvi        REAL DEFAULT 0,
            cena_jednotka   REAL DEFAULT 0,
            nazev_override  TEXT
        )
    """)
    # nabidky_materialy – pokud existuje se starým sloupcem material_id, smaž a vytvoř znovu
    c.execute("PRAGMA table_info(nabidky_materialy)")
    nb_mat_cols = {r[1] for r in c.fetchall()}
    if 'material_id' in nb_mat_cols and 'material_kod' not in nb_mat_cols:
        c.execute("DROP TABLE IF EXISTS nabidky_materialy")
        log.append("  [MIG] nabidky_materialy přetvořena (material_id → material_kod)")
    c.execute("""
        CREATE TABLE IF NOT EXISTS nabidky_materialy (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            nabidka_id   INTEGER NOT NULL REFERENCES nabidky(id) ON DELETE CASCADE,
            material_kod TEXT REFERENCES materialy(kod),
            sirka_mm     REAL DEFAULT 0,
            vyska_mm     REAL DEFAULT 0,
            pocet_ks     INTEGER DEFAULT 1,
            cena_m2      REAL DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS nabidky_extra (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            nabidka_id  INTEGER NOT NULL REFERENCES nabidky(id) ON DELETE CASCADE,
            nazev       TEXT,
            cena        REAL DEFAULT 0
        )
    """)
    # Přidej prorez_procento do nabidky_materialy (pokud chybí)
    add_column('nabidky_materialy', 'prorez_procento', 'REAL DEFAULT 0')
    # Přidej prorez_procento do nabidky_import (prořez platí i pro desky/pěny/profily v importu)
    add_column('nabidky_import', 'prorez_procento', 'REAL DEFAULT 0')
    log.append("  [OK] nabidky, nabidky_import, nabidky_materialy, nabidky_extra")

    # ── PŘEKLADAČ KÓDŮ PRO NABÍDKY ───────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS nabidky_prekladac (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            externi_kod  TEXT NOT NULL UNIQUE,
            interni_kod  TEXT NOT NULL,
            poznamka     TEXT,
            created_at   TEXT DEFAULT (datetime('now'))
        )
    """)
    log.append("  [OK] nabidky_prekladac")

    # ── NABÍDKY – SPOJOVACÍ MATERIÁL (HW) ────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS nabidky_hw (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            nabidka_id      INTEGER NOT NULL REFERENCES nabidky(id) ON DELETE CASCADE,
            material_kod    TEXT REFERENCES materialy(kod),
            nazev_override  TEXT,
            mnozstvi        REAL DEFAULT 1,
            cena_ks         REAL DEFAULT 0,
            auto_generated  INTEGER DEFAULT 0
        )
    """)
    # Přidej auto_generated do nabidky_hw pokud chybí (migrace)
    add_column('nabidky_hw', 'auto_generated', 'INTEGER DEFAULT 0')
    log.append("  [OK] nabidky_hw")

    # Odběratel v faktuře (dynamicky — defaultně AUDIO PARTNER, ale změnitelný)
    add_column('faktury', 'odberatel_nazev', "TEXT DEFAULT 'AUDIO PARTNER s.r.o.'")
    add_column('faktury', 'odberatel_ulice', "TEXT DEFAULT 'Mezi vodami 2044/23'")
    add_column('faktury', 'odberatel_mesto', "TEXT DEFAULT '143 00 Praha 4'")
    add_column('faktury', 'odberatel_ic',    "TEXT DEFAULT '27114147'")
    add_column('faktury', 'odberatel_dic',   "TEXT DEFAULT 'CZ27114147'")
    log.append("  [OK] faktury.odberatel_*")

    # ── DOCHÁZKA NA DÍLNĚ ────────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS dochazka (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            uzivatel_id INTEGER NOT NULL REFERENCES uzivatele(id) ON DELETE CASCADE,
            datum       TEXT NOT NULL,   -- ISO date "2026-04-14"
            cas_od      TEXT,            -- "07:00" .. "19:00"
            cas_do      TEXT,
            poznamka    TEXT,
            updated_at  TEXT DEFAULT (datetime('now')),
            UNIQUE(uzivatel_id, datum)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_doch_datum ON dochazka(datum)")
    log.append("  [OK] dochazka")

    # ── DOCHÁZKA – záznamy příchodů / odchodů (live) ─────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS dochazka_zaznamy (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            uzivatel_id     INTEGER NOT NULL REFERENCES uzivatele(id) ON DELETE CASCADE,
            datum           TEXT NOT NULL,       -- ISO date  "2026-04-16"
            cas_prichod     TEXT NOT NULL,       -- ISO datetime "2026-04-16 07:30:00"
            cas_odchod      TEXT,                -- NULL = momentálně přítomen
            rucne_upraveno  INTEGER DEFAULT 0,   -- 1 = ručně editováno
            poznamka        TEXT,
            created_at      TEXT DEFAULT (datetime('now')),
            updated_at      TEXT DEFAULT (datetime('now'))
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_dzaz_uid   ON dochazka_zaznamy(uzivatel_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_dzaz_datum ON dochazka_zaznamy(datum)")
    log.append("  [OK] dochazka_zaznamy")

    # ── PRŮVODKA MONTÁŽE – profily per-zakázka ───────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS pruvodni_profily (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            zakazka_id  INTEGER NOT NULL REFERENCES zakazky(id) ON DELETE CASCADE,
            typ_profilu TEXT NOT NULL,    -- 'L' nebo 'H'
            poradi      INTEGER NOT NULL, -- pořadí řádku v rámci typu
            ks          INTEGER DEFAULT 0,
            rozmer_mm   REAL,
            zarazka     REAL,             -- pozice dorazu na pile = rozmer_mm by default, editovatelné
            rez         TEXT DEFAULT '| |', -- typ řezu L profilu: '| |', '/ |', '| \\', '/ \\'
            zakonceni   TEXT,             -- pro H profil: kód zakončení z číselníku
            poznamka    TEXT,
            UNIQUE(zakazka_id, typ_profilu, poradi)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_pruv_zak ON pruvodni_profily(zakazka_id)")
    add_column('pruvodni_profily', 'zarazka_2', 'REAL')   # druhá zarážka pily pro L profily
    log.append("  [OK] pruvodni_profily")

    # Přednastavení prořezu pro nové typy materiálů
    c.execute("SELECT DISTINCT typ FROM materialy WHERE typ IS NOT NULL AND typ != ''")
    for (typ,) in c.fetchall():
        c.execute("INSERT OR IGNORE INTO prorez (typ, procento) VALUES (?, 0)", (typ,))

    # ── MIGRACE pena_odkaz → typy_casu_links ─────────────────────────────────
    # Jednorázová migrace: stávající pena_odkaz + pena_poznamka překopírujeme
    # do nové tabulky typy_casu_links (pouze pokud pro daný typ ještě neexistuje žádný odkaz).
    try:
        c.execute("""
            SELECT id, pena_odkaz, pena_poznamka FROM typy_casu
            WHERE pena_odkaz IS NOT NULL AND trim(pena_odkaz) != ''
        """)
        for row in c.fetchall():
            typ_id = row[0]
            url    = (row[1] or '').strip()
            pozn   = (row[2] or '').strip()
            if not url:
                continue
            c.execute("SELECT COUNT(*) FROM typy_casu_links WHERE typ_casu_id=?", (typ_id,))
            if c.fetchone()[0] == 0:
                # Název: použij poznámku pokud je krátká a výstižná, jinak výchozí
                nazev = pozn if (pozn and len(pozn) <= 60) else 'Postup lepení pěn'
                c.execute(
                    "INSERT INTO typy_casu_links (typ_casu_id, nazev, url, poradi) VALUES (?, ?, ?, ?)",
                    (typ_id, nazev, url, 1)
                )
                log.append(f"  [OK] typy_casu_links: migrován pena_odkaz pro typ_casu_id={typ_id}")
    except Exception as e:
        log.append(f"  [WARN] migrace pena_odkaz selhala: {e}")

    # ── NASTAVENÍ SYSTÉMU (key-value store) ─────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS nastaveni (
            klic   TEXT PRIMARY KEY,
            hodnota TEXT
        )
    """)

    # ── JEDNORÁZOVÉ DATOVÉ MIGRACE ───────────────────────────────────────────
    # Tracking tabulka — zabrání opakovanému spouštění jednorázových migrací
    c.execute("""
        CREATE TABLE IF NOT EXISTS _migrations (
            name    TEXT PRIMARY KEY,
            ran_at  TEXT DEFAULT (datetime('now'))
        )
    """)

    # Migrace: existující zakázky → odeslano_do_vyroby = 1
    # Zakázky vzniklé před zavedením Příprava výroby mají DEFAULT 0 a "zmizely" z CNC/Dílna.
    # Tato migrace je nastaví na 1 — jsou to reálné výrobní zakázky.
    c.execute("SELECT 1 FROM _migrations WHERE name='odeslano_init_v1'")
    if not c.fetchone():
        c.execute("""
            UPDATE zakazky SET odeslano_do_vyroby = 1
            WHERE odeslano_do_vyroby = 0
              AND stav IN ('Čeká','CNC hotovo','Výroba','Hotovo','Zkontrolováno','Expedováno')
        """)
        c.execute("INSERT INTO _migrations (name) VALUES ('odeslano_init_v1')")
        log.append("  [OK] migrace odeslano_init_v1: existující zakázky → odeslano_do_vyroby=1")

    # Migrace: přejmenování priority 'Řešit okamžitě' → 'Okamžitě' v kancelar_zakazky
    c.execute("SELECT 1 FROM _migrations WHERE name='kan_prio_okamzite_v1'")
    if not c.fetchone():
        c.execute("""
            UPDATE kancelar_zakazky SET priorita = 'Okamžitě'
            WHERE priorita = 'Řešit okamžitě'
        """)
        c.execute("INSERT INTO _migrations (name) VALUES ('kan_prio_okamzite_v1')")
        log.append("  [OK] migrace kan_prio_okamzite_v1: priorita 'Řešit okamžitě' → 'Okamžitě'")

    # ── BOM IMPORT — ignorované kódy ────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS bom_import_ignore (
            kod     TEXT PRIMARY KEY,
            popis   TEXT DEFAULT ''
        )
    """)

    # ── TYPY KORPUSU — editovatelný číselník ─────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS typy_korpusu (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            nazev  TEXT NOT NULL,
            poradi INTEGER NOT NULL DEFAULT 0
        )
    """)
    c.execute("SELECT 1 FROM _migrations WHERE name='typy_korpusu_init_v1'")
    if not c.fetchone():
        default_typy = [
            'Hlava / kombo', 'Mixpult', 'Klávesy', 'Rack', 'Rack Sliding door',
            'Accessory case', 'Pedalboard', 'Case pro světelné hlavy', 'Case pro TV',
            'Šatní skříň', 'Jiný typ', 'Inlay', 'Akustický panel 60x60 v rámu',
            'Akustický panel 120x60 bez rámu', 'R1 system'
        ]
        for i, nazev in enumerate(default_typy):
            c.execute("INSERT INTO typy_korpusu (nazev, poradi) VALUES (?, ?)", (nazev, i))
        c.execute("INSERT INTO _migrations (name) VALUES ('typy_korpusu_init_v1')")
        log.append("[OK] typy_korpusu — výchozí typy vloženy")

    # ── Oprava Sestřílení: cas_base_s 5→10 (správná výchozí hodnota) ─────────
    c.execute("SELECT 1 FROM _migrations WHERE name='sestrileni_base_5to10_v1'")
    if not c.fetchone():
        c.execute("""
            UPDATE cas_parametry SET hodnota = 10
            WHERE sekce = 'Sestrileni' AND klic = 'cas_base_s' AND hodnota = 5
        """)
        c.execute("INSERT INTO _migrations (name) VALUES ('sestrileni_base_5to10_v1')")
        log.append("[OK] cas_parametry — Sestrileni.cas_base_s opraveno 5→10")

    # ── VÝCHOZÍ BOM (materiály automaticky vkládané do každého nového HN) ──
    c.execute("""
        CREATE TABLE IF NOT EXISTS vychozi_bom (
            material_kod  TEXT PRIMARY KEY,
            mnozstvi      REAL NOT NULL DEFAULT 1,
            poradi        INTEGER NOT NULL DEFAULT 0
        )
    """)
    log.append("[OK] vychozi_bom — tabulka připravena")

    # ── DXF DATA — uložené výsledky parsování DXF souborů pro typ casu ─────
    c.execute("""
        CREATE TABLE IF NOT EXISTS typy_casu_dxf (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            typ_casu_id    INTEGER NOT NULL REFERENCES typy_casu(id) ON DELETE CASCADE,
            nazev_souboru  TEXT,
            vrstvy_json    TEXT NOT NULL DEFAULT '[]',
            varovani_json  TEXT NOT NULL DEFAULT '[]',
            nahrano        TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_typy_casu_dxf ON typy_casu_dxf(typ_casu_id)")
    add_column('typy_casu_dxf', 'overrides_json', "TEXT NOT NULL DEFAULT '{}'")
    add_column('typy_casu_dxf', 'polygony_json',  "TEXT NOT NULL DEFAULT '{}'")
    log.append("  [OK] typy_casu_dxf")

    conn.commit()
    conn.close()
    if log:
        print("Auto-migrace:")
        for l in log:
            print(l)

if __name__ == '__main__':
    init_db()
