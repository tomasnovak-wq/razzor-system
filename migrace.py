"""
Databázová migrace – přidá nové tabulky a sloupce.
Bezpečné spuštění – existující data zůstanou nedotčena.
"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'system.db')

print(f"Migrace databáze: {DB_PATH}")
conn = sqlite3.connect(DB_PATH, timeout=30)
conn.execute("PRAGMA journal_mode = DELETE")
conn.execute("PRAGMA foreign_keys = ON")
c = conn.cursor()

# ── DODAVATELÉ ────────────────────────────────────────────────────────────────
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
print("  [OK] Tabulka 'dodavatele'")

# ── PŘÍJEMKY ──────────────────────────────────────────────────────────────────
c.execute("""
CREATE TABLE IF NOT EXISTS prijemky (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cislo           TEXT,
    dodavatel_id    INTEGER REFERENCES dodavatele(id),
    datum           TEXT NOT NULL DEFAULT (date('now')),
    stav            TEXT DEFAULT 'rozpracováno',
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
    cena_jednotka   REAL DEFAULT 0,
    cena_celkem     REAL DEFAULT 0,
    UNIQUE(prijemka_id, material_kod)
)
""")
c.execute("CREATE INDEX IF NOT EXISTS idx_prij_dod ON prijemky(dodavatel_id)")
print("  [OK] Tabulky 'prijemky' + 'prijemky_polozky'")

# ── ŘEZNÝ PLÁN PROFILŮ ────────────────────────────────────────────────────────
c.execute("""
CREATE TABLE IF NOT EXISTS profily_plan (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    typ_casu_id  INTEGER NOT NULL REFERENCES typy_casu(id) ON DELETE CASCADE,
    typ_profilu  TEXT NOT NULL,
    poradi       INTEGER NOT NULL,
    ks           INTEGER DEFAULT 0,
    rozmer_mm    REAL,
    zakonceni    TEXT,
    zarázka1     REAL,
    zarázka2     REAL,
    UNIQUE(typ_casu_id, typ_profilu, poradi)
)
""")
c.execute("CREATE INDEX IF NOT EXISTS idx_prof_typ ON profily_plan(typ_casu_id)")
print("  [OK] Tabulka 'profily_plan'")

# ── NOVÉ SLOUPCE V typy_casu ──────────────────────────────────────────────────
new_cols_typy = [
    ("orientace_lid", "TEXT"),
    ("pena_poznamka", "TEXT"),
    ("pena_odkaz",    "TEXT"),
    ("prisl_1",       "TEXT"),
    ("prisl_2",       "TEXT"),
    ("prisl_3",       "TEXT"),
    ("prisl_4",       "TEXT"),
]
existing = {row[1] for row in c.execute("PRAGMA table_info(typy_casu)")}
for col, typ in new_cols_typy:
    if col not in existing:
        c.execute(f"ALTER TABLE typy_casu ADD COLUMN {col} {typ}")
        print(f"  [OK] typy_casu.{col}")

# ── NOVÉ SLOUPCE V materialy ───────────────────────────────────────────────────
new_cols_mat = [
    ("web_url", "TEXT"),
]
existing_mat = {row[1] for row in c.execute("PRAGMA table_info(materialy)")}
for col, typ in new_cols_mat:
    if col not in existing_mat:
        c.execute(f"ALTER TABLE materialy ADD COLUMN {col} {typ}")
        print(f"  [OK] materialy.{col}")

# ── FAKTURY ───────────────────────────────────────────────────────────────────
c.execute("""
CREATE TABLE IF NOT EXISTS faktury (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    cislo               TEXT UNIQUE NOT NULL,
    datum_vystaveni     TEXT NOT NULL,
    datum_splatnosti    TEXT NOT NULL,
    datum_plneni        TEXT NOT NULL,
    var_symbol          TEXT,
    vystavil            TEXT DEFAULT 'Kateřina Otradovcová',
    stav                TEXT DEFAULT 'vydána',
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
    cena_za_mj              REAL NOT NULL,
    sazba_dph               REAL DEFAULT 21,
    zaklad                  REAL NOT NULL,
    dph                     REAL NOT NULL,
    celkem_s_dph            REAL NOT NULL
)
""")
c.execute("CREATE INDEX IF NOT EXISTS idx_fak_pol ON faktury_polozky(faktura_id)")
print("  [OK] Tabulky 'faktury' + 'faktury_polozky'")

# ── NOVÝ SLOUPEC V zakazky ─────────────────────────────────────────────────────
existing_zak = {row[1] for row in c.execute("PRAGMA table_info(zakazky)")}
if 'fakturovano' not in existing_zak:
    c.execute("ALTER TABLE zakazky ADD COLUMN fakturovano INTEGER DEFAULT 0")
    print("  [OK] zakazky.fakturovano")

# ── DOPRAVNÉ + MĚNA V PŘÍJEMKÁCH ──────────────────────────────────────────────
existing_prij = {row[1] for row in c.execute("PRAGMA table_info(prijemky)")}
if 'dopravne' not in existing_prij:
    c.execute("ALTER TABLE prijemky ADD COLUMN dopravne REAL DEFAULT 0")
    print("  [OK] prijemky.dopravne")
if 'mena' not in existing_prij:
    c.execute("ALTER TABLE prijemky ADD COLUMN mena TEXT DEFAULT 'CZK'")
    print("  [OK] prijemky.mena")
if 'kurz' not in existing_prij:
    c.execute("ALTER TABLE prijemky ADD COLUMN kurz REAL DEFAULT 1.0")
    print("  [OK] prijemky.kurz")

# ── PROŘEZ ───────────────────────────────────────────────────────────────────
c.execute("""
CREATE TABLE IF NOT EXISTS prorez (
    typ         TEXT PRIMARY KEY,
    procento    REAL DEFAULT 0
)
""")
# Přednastavit 0 % pro všechny stávající typy materiálů
c.execute("SELECT DISTINCT typ FROM materialy WHERE typ IS NOT NULL AND typ != ''")
for (typ,) in c.fetchall():
    c.execute("INSERT OR IGNORE INTO prorez (typ, procento) VALUES (?, 0)", (typ,))
print("  [OK] Tabulka 'prorez'")

# ── UŽIVATELÉ ────────────────────────────────────────────────────────────────
c.execute("""
CREATE TABLE IF NOT EXISTS uzivatele (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    jmeno       TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'Dílna',
    barva       TEXT DEFAULT '#3b82f6',
    aktivni     INTEGER DEFAULT 1,
    created_at  TEXT DEFAULT (datetime('now'))
)
""")
print("  [OK] Tabulka 'uzivatele'")

# ── KANCELÁŘ – ŠTÍTKY ─────────────────────────────────────────────────────────
c.execute("""
CREATE TABLE IF NOT EXISTS kancelar_stitky (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    nazev   TEXT NOT NULL UNIQUE,
    barva   TEXT DEFAULT '#e5e7eb',
    poradi  INTEGER DEFAULT 0,
    aktivni INTEGER DEFAULT 1
)
""")

# Přednastavená sada štítků (přidá jen pokud tabulka dosud prázdná)
c.execute("SELECT COUNT(*) FROM kancelar_stitky")
if c.fetchone()[0] == 0:
    stitky = [
        ('NEŘEŠENO',              '#fca5a5',  1),
        ('Nacenit',               '#f9a8d4',  2),
        ('Nakreslit',             '#fdba74',  3),
        ('Nutno_změřit',          '#fcd34d',  4),
        ('Vytvořit_náhled',       '#bfdbfe',  5),
        ('Vytvořit_objednávku',   '#f9a8d4',  6),
        ('Vyplnit_VHW_a_CNC_data','#fcd34d',  7),
        ('Kontaktovat_zákazníka', '#fca5a5',  8),
        ('Odeslána_nabídka',      '#bfdbfe',  9),
        ('Čekáme_na_odpověď',     '#d1d5db', 10),
        ('Přiveze_na_omeření',    '#d1d5db', 11),
        ('Nakresleno',            '#bfdbfe', 12),
        ('Potvrzeno_zákazníkem',  '#86efac', 13),
        ('Michal_vše_hotovo',     '#fde68a', 14),
        ('Hotovo_uzavřeno',       '#6ee7b7', 15),
        ('Uzavřeno',              '#6ee7b7', 16),
        ('Zrušeno',               '#fca5a5', 17),
    ]
    c.executemany(
        "INSERT INTO kancelar_stitky (nazev, barva, poradi) VALUES (?,?,?)",
        stitky
    )
    print(f"  [OK] Tabulka 'kancelar_stitky' + {len(stitky)} štítků")
else:
    print("  [OK] Tabulka 'kancelar_stitky' (již existuje)")

# ── KANCELÁŘ – ZAKÁZKY ────────────────────────────────────────────────────────
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

c.execute("""
CREATE TABLE IF NOT EXISTS kancelar_zakazky_stitky (
    zakazka_id  INTEGER NOT NULL REFERENCES kancelar_zakazky(id) ON DELETE CASCADE,
    stitek_id   INTEGER NOT NULL REFERENCES kancelar_stitky(id)  ON DELETE CASCADE,
    PRIMARY KEY (zakazka_id, stitek_id)
)
""")
print("  [OK] Tabulky 'kancelar_zakazky' + 'kancelar_zakazky_stitky'")

conn.commit()
conn.close()
print("\nMigrace dokončena! Všechna data zachována.")
input("Stiskni Enter pro zavření...")
