; ============================================================
; export_layers_3d.lsp  —  Razzor Cases  v21
;
; Zjednodušená verze — bez automatického EXPLODE.
; Před spuštěním ručně exploduj bloky (INSERT entity) které
; chceš exportovat. Skript pak projde každou vrstvu (včetně "0"),
; zmrazí ostatní a vyexportuje viditelné 3D objekty do STL.
;
; v15: Přeskakuje skryté/zmrazené vrstvy. Po každém exportu (gc).
;
; v17: Zakázání UNDO záznamu po dobu exportu (prevence pádu AutoCADu).
; Freeze ostatních vrstev jedním "_Freeze *" — rychlejší.
;
; v19: Opravena syntaxe UNDO pro AutoCAD 2020+ na Macu.
; Prompt [All/None/One/Combine/Layer] — správně: "_None" / "_All".
;
; v20: Vrácen referenční box 1×1×1 mm na souřadnicích (0,0,0).
; Syntaxe z v15: (command "._BOX" "0,0,0" "1,1,1").
; Box se přidá před STLOUT, smaže se přes entdel po exportu.
; Server z jeho polohy v STL přesně zjistí UCS offset a opraví ho.
;
; v21: Odstraněno UNDO control — prompt se liší dle verze AutoCADu
; (starší: [Auto/Control/BEgin/End/Mark/Back], novější: [All/None/...])
; a způsobuje pád LISPu. S _Freeze * je undo buffer malý, pád nehrozí.
; ============================================================

(defun _rz-replace (old new str / result i olen)
  (setq result "" i 1 olen (strlen old))
  (while (<= i (strlen str))
    (if (= (substr str i olen) old)
      (progn (setq result (strcat result new)) (setq i (+ i olen)))
      (progn (setq result (strcat result (substr str i 1))) (setq i (1+ i)))
    )
  )
  result
)

(defun _rz-safename (name / s)
  (setq s name)
  (foreach pair (list
    ; Speciální znaky → podtržítko / čárka
    (list " " "_") (list "/" "_") (list "\\" "_") (list ":" "_")
    (list "*" "_") (list "?" "_") (list "<" "_") (list ">" "_")
    (list "|" "_") (list "." ",")
    ; Česká diakritika malá → ASCII
    (list "á" "a") (list "č" "c") (list "ď" "d") (list "é" "e")
    (list "ě" "e") (list "í" "i") (list "ň" "n") (list "ó" "o")
    (list "ř" "r") (list "š" "s") (list "ť" "t") (list "ú" "u")
    (list "ů" "u") (list "ý" "y") (list "ž" "z")
    ; Česká diakritika velká → ASCII
    (list "Á" "A") (list "Č" "C") (list "Ď" "D") (list "É" "E")
    (list "Ě" "E") (list "Í" "I") (list "Ň" "N") (list "Ó" "O")
    (list "Ř" "R") (list "Š" "S") (list "Ť" "T") (list "Ú" "U")
    (list "Ů" "U") (list "Ý" "Y") (list "Ž" "Z"))
    (setq s (_rz-replace (car pair) (cadr pair) s))
  )
  s
)

; Vrátí T pokud je vrstva viditelná (není zmrazená ani vypnutá)
(defun _rz-layer-visible-p (lname / ldata flags color)
  (setq ldata (tblsearch "layer" lname))
  (if ldata
    (progn
      (setq flags (cdr (assoc 70 ldata)))
      (setq color (cdr (assoc 62 ldata)))
      (and (= 0 (logand flags 1)) (> color 0))
    )
    nil
  )
)

(defun c:ExportLayers3D ( / outdir orig_clayer orig_tilemode orig_regenmode
                            layer_data layer_name safe_name stl_path
                            all_layers exported_count skipped_count sel
                            orig_frozen orig_off lflags lcolor rz_ref_box)

  (setq orig_clayer     (getvar "CLAYER"))
  (setq orig_tilemode   (getvar "TILEMODE"))
  (setq orig_regenmode  (getvar "REGENMODE"))
  (setq outdir          (getvar "DWGPREFIX"))
  (setq exported_count  0)
  (setq skipped_count   0)

  ; Přepni do model space
  (if (= orig_tilemode 0) (setvar "TILEMODE" 1))

  ; Zakáž automatický regen při přepínání vrstev
  (setvar "REGENMODE" 0)

  (princ "\n=== Razzor 3D Export v19 ===")
  (princ (strcat "\nSložka: " outdir "\n"))

  ; ── Ulož původní stav viditelnosti všech vrstev ───────────────────────────
  (setq orig_frozen '()  orig_off '())
  (setq layer_data (tblnext "layer" T))
  (while layer_data
    (setq layer_name (cdr (assoc 2 layer_data)))
    (setq lflags     (cdr (assoc 70 layer_data)))
    (setq lcolor     (cdr (assoc 62 layer_data)))
    (cond
      ((= 1 (logand lflags 1))
       (setq orig_frozen (cons layer_name orig_frozen)))
      ((< lcolor 0)
       (setq orig_off (cons layer_name orig_off)))
    )
    (setq layer_data (tblnext "layer"))
  )

  ; ── Sestav seznam vrstev k exportu (pouze viditelné, bez Defpoints) ──────
  (setq all_layers '())
  (setq layer_data (tblnext "layer" T))
  (while layer_data
    (setq layer_name (cdr (assoc 2 layer_data)))
    (cond
      ((member layer_name (list "Defpoints"))
       nil)
      ((not (_rz-layer-visible-p layer_name))
       (princ (strcat "\n  [přeskočeno — skryté] " layer_name))
       (setq skipped_count (1+ skipped_count)))
      (T
       (setq all_layers (cons layer_name all_layers)))
    )
    (setq layer_data (tblnext "layer"))
  )

  ; ── Export každé vrstvy ───────────────────────────────────────────────────
  (foreach layer_name all_layers

    (setq sel (ssget "_X" (list
      (cons 0 "3DSOLID,MESH,SURFACE,REGION,BODY")
      (cons 8 layer_name))))

    (if (and sel (> (sslength sel) 0))
      (progn
        (princ (strcat "\nVrstva: " layer_name " ..."))

        (setvar "CLAYER" layer_name)

        ; Zmraz VŠECHNY vrstvy najednou (AutoCAD přeskočí current layer automaticky)
        (vl-catch-all-apply '(lambda () (command "-LAYER" "_Freeze" "*" "")))
        (command "-LAYER" "_Thaw" layer_name "")
        (command "-LAYER" "_On"   layer_name "")
        (command "._UCS" "_W")

        ; Referenční box 1×1×1 mm na WCS (0,0,0) — server z jeho polohy
        ; v STL zjistí přesný UCS offset a automaticky ho opraví.
        ; Syntaxe ověřená v15: druhý argument "1,1,1" = protilehlý roh boxu.
        (command "._BOX" "0,0,0" "1,1,1")
        (setq rz_ref_box (entlast))

        ; Export STL
        (setq safe_name (_rz-safename layer_name))
        (setq stl_path  (strcat outdir safe_name ".stl"))
        (setvar "FILEDIA" 0)
        (command "STLOUT" "all" "" "Y" stl_path)
        (setvar "FILEDIA" 1)

        ; Smaž referenční box z výkresu (entdel místo UNDO — UNDO je vypnuté)
        (if rz_ref_box (entdel rz_ref_box))
        (setq rz_ref_box nil)

        (setq exported_count (1+ exported_count))
        (princ (strcat " → " safe_name ".stl ✓"))

        ; Rozmraz všechny vrstvy
        (command "-LAYER" "_Thaw" "*" "")
        (command "-LAYER" "_On"   "*" "")

        ; ── Obnov původní stav viditelnosti ──────────────────────────────
        (foreach lname orig_frozen
          (if (not (= lname layer_name))
            (vl-catch-all-apply
              '(lambda () (command "-LAYER" "_Freeze" lname "")))
          )
        )
        (foreach lname orig_off
          (vl-catch-all-apply
            '(lambda () (command "-LAYER" "_Off" lname "")))
        )

        ; Uvolni paměť
        (gc)
      )
    )
  )

  ; ── Obnov původní stav ────────────────────────────────────────────────────
  (setvar "CLAYER" orig_clayer)
  (setvar "TILEMODE" orig_tilemode)
  (setvar "FILEDIA" 1)
  (setvar "REGENMODE" orig_regenmode)

  ; Finální obnova viditelnosti (pro jistotu)
  (foreach lname orig_frozen
    (vl-catch-all-apply
      '(lambda () (command "-LAYER" "_Freeze" lname "")))
  )
  (foreach lname orig_off
    (vl-catch-all-apply
      '(lambda () (command "-LAYER" "_Off" lname "")))
  )

  (princ (strcat "\n\n=== Hotovo! Exportováno " (itoa exported_count) " vrstev"
    (if (> skipped_count 0)
      (strcat " (" (itoa skipped_count) " skrytých přeskočeno)")
      "")
    " ==="))

  (alert (strcat
    "Hotovo! Exportováno " (itoa exported_count) " vrstev.\n"
    (if (> skipped_count 0)
      (strcat "(" (itoa skipped_count) " skrytých vrstev bylo přeskočeno)\n")
      "")
    "\nSTL soubory jsou ve složce:\n" outdir "\n\n"
    "Postup:\n"
    "1. Otevři složku ve Finderu\n"
    "2. Vyber .stl soubory\n"
    "3. Pravý klik → Komprimovat\n"
    "4. Nahraj ZIP do Razzor → záložka 3D"
  ))
  (princ)
)

(princ "\nRazzor 3D Export v21. Příkaz: ExportLayers3D\n")
(princ)
