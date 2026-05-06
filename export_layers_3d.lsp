; ============================================================
; export_layers_3d.lsp  —  Razzor Cases
; Mac-kompatibilní verze (bez vl-load-com, bez vlax/vla)
;
; Použití:
;   1. APPLOAD → načti tento soubor
;   2. Do příkazové řádky napiš:  ExportLayers3D
;
; Výsledek: STL soubory ve stejné složce jako výkres
;           pojmenované podle vrstvy.
;           Pak vyber všechny .stl soubory → pravý klik → Komprimovat.
; ============================================================

; Pomocná funkce: nahradí jeden znak/řetězec za jiný v řetězci
(defun _rz-replace (old new str / result i olen)
  (setq result "" i 1 olen (strlen old))
  (while (<= i (strlen str))
    (if (= (substr str i olen) old)
      (progn
        (setq result (strcat result new))
        (setq i (+ i olen))
      )
      (progn
        (setq result (strcat result (substr str i 1)))
        (setq i (1+ i))
      )
    )
  )
  result
)

; Pomocná funkce: sanitizuje název vrstvy pro název souboru
(defun _rz-safename (name / s)
  (setq s name)
  (setq s (_rz-replace " " "_" s))
  (setq s (_rz-replace "/" "_" s))
  (setq s (_rz-replace "\\" "_" s))
  (setq s (_rz-replace ":" "_" s))
  (setq s (_rz-replace "*" "_" s))
  (setq s (_rz-replace "?" "_" s))
  (setq s (_rz-replace "<" "_" s))
  (setq s (_rz-replace ">" "_" s))
  (setq s (_rz-replace "|" "_" s))
  (setq s (_rz-replace "." "," s))  ; tečka → čárka (6.5mm → 6,5mm)
  s
)

; Hlavní příkaz
(defun c:ExportLayers3D ( / outdir cur_layer layer_data layer_name
                            safe_name stl_path sel exported_count
                            all_layers other_data other_name)

  (setq cur_layer (getvar "CLAYER"))
  (setq outdir    (getvar "DWGPREFIX"))  ; STL soubory jdou do stejné složky jako výkres
  (setq exported_count 0)

  (princ "\n=== Razzor 3D Export ===")
  (princ (strcat "\nVýstupní složka: " outdir))
  (princ "\n")

  ; Nejdříve projdi vrstvy a ulož si jejich názvy
  (setq all_layers '())
  (setq layer_data (tblnext "layer" T))
  (while layer_data
    (setq layer_name (cdr (assoc 2 layer_data)))
    (if (not (member layer_name (list "0" "Defpoints")))
      (setq all_layers (cons layer_name all_layers))
    )
    (setq layer_data (tblnext "layer"))
  )

  ; Projdi každou vrstvu
  (foreach layer_name all_layers

    ; Zkontroluj jestli vrstva má 3D solid objekty
    (setq sel (ssget "_X" (list (cons 0 "3DSOLID") (cons 8 layer_name))))

    (if (and sel (> (sslength sel) 0))
      (progn
        (princ (strcat "\nZpracovávám: " layer_name "..."))

        ; Zmraz všechny ostatní vrstvy (kromě aktuální CLAYER a exportované)
        (setq other_data (tblnext "layer" T))
        (while other_data
          (setq other_name (cdr (assoc 2 other_data)))
          (if (and (not (= other_name layer_name))
                   (not (= other_name cur_layer)))
            (vl-catch-all-apply
              '(lambda () (command "-LAYER" "_Freeze" other_name "")))
          )
          (setq other_data (tblnext "layer"))
        )

        ; Rozmraz a zapni exportovanou vrstvu
        (command "-LAYER" "_Thaw" layer_name "")
        (command "-LAYER" "_On"   layer_name "")

        ; Sestav cestu k STL souboru
        (setq safe_name (_rz-safename layer_name))
        (setq stl_path (strcat outdir safe_name ".stl"))

        ; Znovu vyber objekty na vrstvě (po změně viditelnosti)
        (setq sel (ssget "_X" (list (cons 0 "3DSOLID") (cons 8 layer_name))))

        (if (and sel (> (sslength sel) 0))
          (progn
            (command "STLOUT" sel "" "Y" stl_path)
            (setq exported_count (1+ exported_count))
            (princ (strcat " → " safe_name ".stl ✓"))
          )
          (princ " → žádné viditelné objekty, přeskočeno")
        )

        ; Rozmraz všechny vrstvy zpět
        (setq other_data (tblnext "layer" T))
        (while other_data
          (setq other_name (cdr (assoc 2 other_data)))
          (vl-catch-all-apply
            '(lambda () (command "-LAYER" "_Thaw" other_name "")))
          (vl-catch-all-apply
            '(lambda () (command "-LAYER" "_On"   other_name "")))
          (setq other_data (tblnext "layer"))
        )
      )

      ; Vrstva nemá 3D solid objekty → přeskoč tiše
    )
  )

  ; Výsledná zpráva
  (princ (strcat "\n\n=== Hotovo! Exportováno " (itoa exported_count) " vrstev ==="))
  (princ (strcat "\nSložka: " outdir))

  (alert (strcat
    "Hotovo! Exportováno " (itoa exported_count) " vrstev.\n\n"
    "STL soubory jsou ve složce:\n" outdir "\n\n"
    "Postup:\n"
    "1. Otevři složku ve Finderu\n"
    "2. Seřaď dle data — vyber jen nové .stl soubory\n"
    "3. Pravý klik → Komprimovat\n"
    "4. Nahraj ZIP do Razzor → BOM editor → záložka 3D"
  ))

  (princ)
)

(princ "\nRazzor 3D Export načten. Spusť příkazem: ExportLayers3D\n")
(princ)
