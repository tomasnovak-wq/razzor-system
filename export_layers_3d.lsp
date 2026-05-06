; ============================================================
; export_layers_3d.lsp  —  Razzor Cases  v15
;
; Zjednodušená verze — bez automatického EXPLODE.
; Před spuštěním ručně exploduj bloky (INSERT entity) které
; chceš exportovat. Skript pak projde každou vrstvu (včetně "0"),
; zmrazí ostatní a vyexportuje viditelné 3D objekty do STL.
;
; v14: Před každým STLOUT se vytvoří referenční box 1×1×1 mm
; na souřadnicích (0,0,0) světového souřadnicového systému.
; Server z polohy tohoto boxu v STL souboru přesně změří
; případný UCS offset a automaticky ho opraví.
; Box se ihned po exportu smaže (entdel), výkres zůstane čistý.
;
; v15: Přeskakuje vrstvy, které jsou v kresbě skryté nebo zmrazené
; (uživatel je schválně vypnul → nechce je exportovat).
; Po každém exportu se volá (gc) pro uvolnění paměti —
; předchází pádu AutoCADu při exportu mnoha vrstev najednou.
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
    (list " " "_") (list "/" "_") (list "\\" "_") (list ":" "_")
    (list "*" "_") (list "?" "_") (list "<" "_") (list ">" "_")
    (list "|" "_") (list "." ","))
    (setq s (_rz-replace (car pair) (cadr pair) s))
  )
  s
)

; Vrátí T pokud je vrstva viditelná (není zmrazená ani vypnutá)
(defun _rz-layer-visible-p (lname / ldata flags color)
  (setq ldata (tblsearch "layer" lname))
  (if ldata
    (progn
      (setq flags (cdr (assoc 70 ldata)))   ; bit 1 = zmrazená, bit 4 = nová VP frozen
      (setq color (cdr (assoc 62 ldata)))   ; záporná = vypnutá
      (and
        (= 0 (logand flags 1))              ; není zmrazená
        (> color 0)                         ; není vypnutá
      )
    )
    nil
  )
)

(defun c:ExportLayers3D ( / outdir orig_clayer orig_tilemode
                            layer_data layer_name safe_name stl_path
                            all_layers other_data other_name
                            exported_count skipped_count sel ref_ent)

  (setq orig_clayer    (getvar "CLAYER"))
  (setq orig_tilemode  (getvar "TILEMODE"))
  (setq outdir         (getvar "DWGPREFIX"))
  (setq exported_count 0)
  (setq skipped_count  0)

  ; Přepni do model space
  (if (= orig_tilemode 0) (setvar "TILEMODE" 1))

  (princ "\n=== Razzor 3D Export v15 ===")
  (princ (strcat "\nSložka: " outdir "\n"))

  ; Ulož seznam vrstev (bez "Defpoints"), pouze viditelné
  (setq all_layers '())
  (setq layer_data (tblnext "layer" T))
  (while layer_data
    (setq layer_name (cdr (assoc 2 layer_data)))
    (cond
      ; Přeskoč Defpoints vždy
      ((member layer_name (list "Defpoints"))
       nil)
      ; Přeskoč skryté/zmrazené vrstvy
      ((not (_rz-layer-visible-p layer_name))
       (princ (strcat "\n  [přeskočeno — skryté] " layer_name))
       (setq skipped_count (1+ skipped_count)))
      ; Viditelná vrstva — přidej do seznamu
      (T
       (setq all_layers (cons layer_name all_layers)))
    )
    (setq layer_data (tblnext "layer"))
  )

  (foreach layer_name all_layers

    ; Má vrstva vůbec nějaké 3D objekty?
    (setq sel (ssget "_X" (list
      (cons 0 "3DSOLID,MESH,SURFACE,REGION,BODY")
      (cons 8 layer_name))))

    (if (and sel (> (sslength sel) 0))
      (progn
        (princ (strcat "\nVrstva: " layer_name " ..."))

        ; Přepni CLAYER
        (setvar "CLAYER" layer_name)

        ; Zmraz všechny ostatní vrstvy
        (setq other_data (tblnext "layer" T))
        (while other_data
          (setq other_name (cdr (assoc 2 other_data)))
          (if (not (= other_name layer_name))
            (vl-catch-all-apply
              '(lambda () (command "-LAYER" "_Freeze" other_name "")))
          )
          (setq other_data (tblnext "layer"))
        )
        (command "-LAYER" "_Thaw" layer_name "")
        (command "-LAYER" "_On"   layer_name "")
        (command "._UCS" "_W")

        ; ── Referenční box 1×1×1 mm na souřadnicích (0,0,0) ──────────────
        ; Server z jeho polohy v STL zjistí přesný UCS offset a opraví ho.
        (setvar "CLAYER" layer_name)
        (command "._BOX" "0,0,0" "1,1,1")
        (setq ref_ent (entlast))

        ; Export STL (obsahuje geometrii vrstvy + referenční box)
        (setq safe_name (_rz-safename layer_name))
        (setq stl_path  (strcat outdir safe_name ".stl"))
        (setvar "FILEDIA" 0)
        (command "STLOUT" "all" "" "Y" stl_path)
        (setvar "FILEDIA" 1)

        ; Smaž referenční box — výkres zůstane čistý
        (if ref_ent (entdel ref_ent))
        (setq ref_ent nil)

        (setq exported_count (1+ exported_count))
        (princ (strcat " → " safe_name ".stl ✓"))

        ; Rozmraz všechny vrstvy
        (command "-LAYER" "_Thaw" "*" "")
        (command "-LAYER" "_On"   "*" "")

        ; Uvolni paměť po každém exportu (gc = standard AutoLISP, funguje na Mac i Win)
        (gc)
      )
    )
  )

  ; Obnov původní stav
  (setvar "CLAYER" orig_clayer)
  (setvar "TILEMODE" orig_tilemode)
  (setvar "FILEDIA" 1)

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

(princ "\nRazzor 3D Export v15b. Příkaz: ExportLayers3D\n")
(princ)
