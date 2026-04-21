"""
Generátor PDF faktur — Razzor Cases (K-AUDIO Impex s.r.o.)
Používá reportlab + DejaVu TTF pro českou diakritiku.
"""

import os
import io
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Table, TableStyle

# ── Firemní konstanty ──────────────────────────────────────────────────────
DODAVATEL = {
    'nazev':  'K-AUDIO Impex s.r.o.',
    'ulice':  'Mezi vodami 2044/23',
    'mesto':  '143 00 Praha 4',
    'ic':     '27380793',
    'dic':    'CZ27380793',
    'tel':    '+420 244 090 448',
    'ucet':   '6935804001/5500',
}
ODBERATEL = {
    'nazev':  'AUDIO PARTNER s.r.o.',
    'ulice':  'Mezi vodami 2044/23',
    'mesto':  '143 00 Praha 4',
    'ic':     '27114147',
    'dic':    'CZ27114147',
}

BASE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(BASE, 'static', 'fonts')

# ── Registrace fontů ───────────────────────────────────────────────────────
_fonts_registered = False

def _register_fonts():
    global _fonts_registered
    if _fonts_registered:
        return 'DejaVu', 'DejaVu-Bold'
    candidates = [
        FONT_DIR,
        r'C:\Windows\Fonts',
        '/usr/share/fonts/truetype/dejavu',
        os.path.expanduser('~/Library/Fonts'),
    ]
    for d in candidates:
        reg  = os.path.join(d, 'DejaVuSans.ttf')
        bold = os.path.join(d, 'DejaVuSans-Bold.ttf')
        if os.path.exists(reg) and os.path.exists(bold):
            pdfmetrics.registerFont(TTFont('DejaVu', reg))
            pdfmetrics.registerFont(TTFont('DejaVu-Bold', bold))
            _fonts_registered = True
            return 'DejaVu', 'DejaVu-Bold'
    return 'Helvetica', 'Helvetica-Bold'


# ── Pomocné ────────────────────────────────────────────────────────────────

def _fmt_czk(v):
    if v is None:
        return '0,00'
    s = f"{float(v):,.2f}"                # "1,234.56"
    s = s.replace(',', '\u00a0').replace('.', ',')  # "1 234,56"
    return s

def _fmt_date(iso):
    if not iso:
        return ''
    try:
        return datetime.strptime(str(iso)[:10], '%Y-%m-%d').strftime('%d.%m.%Y')
    except Exception:
        return str(iso)


# ── Hlavní funkce ──────────────────────────────────────────────────────────

def vygeneruj_pdf(faktura: dict, polozky: list) -> bytes:
    """
    Vrátí bytes hotového PDF.

    Args:
        faktura: dict odpovídající řádku z tabulky 'faktury'
        polozky: list dict odpovídajících řádkům z 'faktury_polozky'
    """
    FONT, FONT_B = _register_fonts()

    buf = io.BytesIO()
    W, H = A4   # 595.27 × 841.89 pt

    cv = canvas.Canvas(buf, pagesize=A4)

    def txt(x, y, text, size=9, bold=False, color=(0, 0, 0)):
        cv.setFont(FONT_B if bold else FONT, size)
        cv.setFillColorRGB(*color)
        cv.drawString(x, y, str(text))
        cv.setFillColorRGB(0, 0, 0)

    def rtxt(x, y, text, size=9, bold=False, color=(0, 0, 0)):
        cv.setFont(FONT_B if bold else FONT, size)
        cv.setFillColorRGB(*color)
        cv.drawRightString(x, y, str(text))
        cv.setFillColorRGB(0, 0, 0)

    ML = 18 * mm          # margin left
    MR = W - 18 * mm      # margin right

    # ── Záhlaví – dodavatel vlevo ──────────────────────────────────────────
    y = H - 16 * mm
    txt(ML, y, DODAVATEL['nazev'], size=13, bold=True)
    y -= 5 * mm
    txt(ML, y, DODAVATEL['ulice'], size=8.5)
    y -= 4.2 * mm
    txt(ML, y, DODAVATEL['mesto'], size=8.5)
    y -= 4.2 * mm
    txt(ML, y, f"IČ: {DODAVATEL['ic']}   DIČ: {DODAVATEL['dic']}", size=8.5)
    y -= 4.2 * mm
    txt(ML, y, f"Tel.: {DODAVATEL['tel']}", size=8.5)

    # ── Záhlaví – faktura vpravo ───────────────────────────────────────────
    rtxt(MR, H - 16 * mm, 'FAKTURA', size=18, bold=True, color=(0.12, 0.27, 0.55))
    rtxt(MR, H - 23 * mm, 'DAŇOVÝ DOKLAD', size=10, color=(0.3, 0.3, 0.3))
    rtxt(MR, H - 34 * mm, faktura['cislo'], size=22, bold=True, color=(0.12, 0.27, 0.55))

    top_block_bottom = H - 55 * mm

    # ── Odběratel (box vlevo) — z faktura dict, fallback na AUDIO PARTNER ───
    odb_nazev = faktura.get('odberatel_nazev') or ODBERATEL['nazev']
    odb_ulice = faktura.get('odberatel_ulice') or ODBERATEL['ulice']
    odb_mesto = faktura.get('odberatel_mesto') or ODBERATEL['mesto']
    odb_ic    = faktura.get('odberatel_ic')    or ODBERATEL['ic']
    odb_dic   = faktura.get('odberatel_dic')   or ODBERATEL['dic']

    bx = ML
    bw = 80 * mm
    bh = 33 * mm
    by = top_block_bottom - bh

    cv.setStrokeColorRGB(0.75, 0.75, 0.75)
    cv.setLineWidth(0.5)
    cv.rect(bx, by, bw, bh)
    txt(bx + 2.5 * mm, by + bh - 5 * mm, 'ODBĚRATEL', size=7, color=(0.5, 0.5, 0.5))
    txt(bx + 2.5 * mm, by + bh - 10 * mm, odb_nazev, size=10, bold=True)
    txt(bx + 2.5 * mm, by + bh - 15 * mm, odb_ulice, size=8.5)
    txt(bx + 2.5 * mm, by + bh - 19.5 * mm, odb_mesto, size=8.5)
    txt(bx + 2.5 * mm, by + bh - 24 * mm, f"IČ: {odb_ic}", size=8.5)
    txt(bx + 2.5 * mm, by + bh - 28.5 * mm, f"DIČ: {odb_dic}", size=8.5)

    # ── Platební informace (tabulka vpravo) ────────────────────────────────
    cislo = faktura.get('cislo', '')
    var_sym = faktura.get('var_symbol') or cislo
    info_rows = [
        ('Datum vystavení:',  _fmt_date(faktura.get('datum_vystaveni'))),
        ('Datum splatnosti:', _fmt_date(faktura.get('datum_splatnosti'))),
        ('Datum plnění:',     _fmt_date(faktura.get('datum_plneni'))),
        ('Variabilní symbol:', var_sym),
        ('Způsob platby:',    'Převodem'),
        ('Bankovní účet:',    DODAVATEL['ucet']),
    ]
    ix = ML + 88 * mm
    iy = top_block_bottom - 5 * mm
    for lbl, val in info_rows:
        txt(ix, iy, lbl, size=8, bold=True)
        txt(ix + 40 * mm, iy, val, size=8)
        iy -= 4.8 * mm

    y = by - 8 * mm

    # ── Tabulka položek ────────────────────────────────────────────────────
    # Šířky sloupců: KÓD+NÁZEV | KS | CENA ZA MJ | SAZBA | ZÁKLAD | CELKEM S DPH
    # Součet = 174 mm (přesně šířka obsahu A4 při okrajích 18 mm)
    col_w = [70 * mm, 10 * mm, 24 * mm, 16 * mm, 26 * mm, 28 * mm]

    tbl_data = [['KÓD + NÁZEV', 'KS', 'CENA ZA MJ', 'SAZBA', 'ZÁKLAD', 'CELKEM S DPH']]
    for p in polozky:
        hn    = (p.get('hn_cislo') or '').strip()
        nazev = (p.get('nazev') or '').strip()
        label = f"{hn} – {nazev}" if hn and nazev else (hn or nazev)
        tbl_data.append([
            label,
            str(p.get('ks', 1)),
            _fmt_czk(p.get('cena_za_mj', 0)) + ' Kč',
            f"{int(p.get('sazba_dph', 21))} %",
            _fmt_czk(p.get('zaklad', 0)) + ' Kč',
            _fmt_czk(p.get('celkem_s_dph', 0)) + ' Kč',
        ])

    tbl = Table(tbl_data, colWidths=col_w, repeatRows=1)
    tbl.setStyle(TableStyle([
        # Hlavička
        ('FONT',        (0, 0), (-1, 0),  FONT_B, 8),
        ('BACKGROUND',  (0, 0), (-1, 0),  colors.HexColor('#1e3a6e')),
        ('TEXTCOLOR',   (0, 0), (-1, 0),  colors.white),
        # Data
        ('FONT',        (0, 1), (-1, -1), FONT,   8),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#eef1f9')]),
        # Zarovnání
        ('ALIGN',       (0, 0), (0,  -1), 'LEFT'),
        ('ALIGN',       (1, 0), (-1, -1), 'RIGHT'),
        # Mřížka
        ('GRID',        (0, 0), (-1, -1), 0.3,   colors.HexColor('#c8c8c8')),
        # Padding
        ('TOPPADDING',    (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('LEFTPADDING',   (0, 0), (-1, -1), 4),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 4),
    ]))

    _, tbl_h = tbl.wrapOn(cv, W - 2 * ML, H)
    tbl.drawOn(cv, ML, y - tbl_h)
    y = y - tbl_h - 6 * mm

    # ── Rekapitulace DPH ──────────────────────────────────────────────────
    rek_x = MR - 96 * mm
    rek_data = [
        ['Sazba DPH', 'Základ bez DPH', 'DPH', 'Celkem s DPH'],
        [
            '21 %',
            _fmt_czk(faktura.get('celkem_bez_dph', 0)) + ' Kč',
            _fmt_czk(faktura.get('celkem_dph', 0)) + ' Kč',
            _fmt_czk(faktura.get('celkem_s_dph', 0)) + ' Kč',
        ],
    ]
    # Součet = 96 mm (shodně s šířkou pruhu "CELKEM K ÚHRADĚ" i s rek_x = MR - 96 mm)
    rtbl = Table(rek_data, colWidths=[22 * mm, 26 * mm, 24 * mm, 24 * mm])
    rtbl.setStyle(TableStyle([
        ('FONT',       (0, 0), (-1, 0),  FONT_B, 8),
        ('FONT',       (0, 1), (-1, -1), FONT,   8),
        ('BACKGROUND', (0, 0), (-1, 0),  colors.HexColor('#dde3f0')),
        ('ALIGN',      (0, 0), (-1, -1), 'RIGHT'),
        ('GRID',       (0, 0), (-1, -1), 0.3,   colors.HexColor('#bbbbbb')),
        ('TOPPADDING',    (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('LEFTPADDING',   (0, 0), (-1, -1), 4),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 4),
    ]))
    _, rh = rtbl.wrapOn(cv, 92 * mm, 30 * mm)
    rtbl.drawOn(cv, rek_x, y - rh)

    # Celkem k úhradě – barevný pruh
    cy = y - rh - 4 * mm
    cv.setFillColorRGB(0.12, 0.27, 0.55)
    cv.rect(rek_x, cy - 8 * mm, 96 * mm, 10 * mm, fill=1, stroke=0)
    cv.setFont(FONT_B, 10)
    cv.setFillColorRGB(1, 1, 1)
    cv.drawString(rek_x + 3 * mm, cy - 5.5 * mm, 'CELKEM K ÚHRADĚ:')
    cv.drawRightString(rek_x + 93 * mm, cy - 5.5 * mm,
                       _fmt_czk(faktura.get('celkem_s_dph', 0)) + ' Kč')
    cv.setFillColorRGB(0, 0, 0)

    # ── Vystavil ──────────────────────────────────────────────────────────
    vy = cy - 16 * mm
    txt(ML, vy, 'Vystavil/a:', size=8, bold=True)
    txt(ML, vy - 4.5 * mm, faktura.get('vystavil') or 'Kateřina Otradovcová', size=9)

    # ── Linka a patička ───────────────────────────────────────────────────
    foot_y = 16 * mm
    cv.setStrokeColorRGB(0.7, 0.7, 0.7)
    cv.setLineWidth(0.5)
    cv.line(ML, foot_y + 5 * mm, MR, foot_y + 5 * mm)
    txt(ML, foot_y, 'K-AUDIO Impex s.r.o. · IČ 27380793 · DIČ CZ27380793 · Plátce DPH',
        size=7, color=(0.5, 0.5, 0.5))
    rtxt(MR, foot_y,
         'MS Praha · oddíl C · vložka 112347',
         size=7, color=(0.5, 0.5, 0.5))

    cv.save()
    return buf.getvalue()
