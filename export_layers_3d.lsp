; ============================================================
; export_layers_3d.lsp  —  Razzor Cases
; Exportuje každou vrstvu se 3D solid objekty jako samostatný
; STL soubor do složky <výkres>_3D_STL/ vedle výkresu.
; Použití: napište do příkazového řádku AutoCADu:  ExportLayers3D
; ============================================================

(defun c:ExportLayers3D ( / acad doc layers outdir lname safename stl_path sel
                            orig_freeze_states curr_layer exported skipped)
  (vl-load-com)

  (setq acad (vlax-get-acad-object))
  (setq doc  (vla-get-ActiveDocument acad))
  (setq layers (vla-get-Layers doc))
  (setq curr_layer (getvar "CLAYER"))

  ; ── Výstupní složka ──────────────────────────────────────
  (setq outdir (strcat
    (getvar "DWGPREFIX")
    (vl-filename-base (getvar "DWGNAME"))
    "_3D_STL"))
  (vl-mkdir outdir)

  ; ── Ulož původní stav zmrazení všech vrstev ───────────────
  (setq orig_freeze_states '())
  (vlax-for lay layers
    (setq orig_freeze_states
      (cons (list (vla-get-Name lay) (vla-get-Freeze lay))
            orig_freeze_states)))

  (setq exported '())
  (setq skipped  '())

  ; ── Procházej vrstvy ─────────────────────────────────────
  (vlax-for lay layers
    (setq lname (vla-get-Name lay))

    ; Přeskoč systémové vrstvy
    (if (not (member lname (list "0" "Defpoints")))
      (progn
        ; Zkontroluj jestli vrstva má 3D solid objekty
        (setq sel (ssget "_X" (list (cons 0 "3DSOLID") (cons 8 lname))))

        (if (and sel (> (sslength sel) 0))
          (progn
            ; Zmraz všechny vrstvy kromě aktuální a exportované
            (vlax-for other_lay layers
              (setq other_name (vla-get-Name other_lay))
              (if (not (equal other_name lname))
                ; Nelze zmrazit aktuální vrstvu (CLAYER) — jen vypneme
                (if (equal other_name curr_layer)
                  (vl-catch-all-apply
                    '(lambda (l) (vla-put-LayerOn l :vlax-false))
                    (list other_lay))
                  (vl-catch-all-apply
                    '(lambda (l) (vla-put-Freeze l :vlax-true))
                    (list other_lay))
                )
              )
            )

            ; Rozmraz a zapni exportovanou vrstvu
            (vl-catch-all-apply
              '(lambda (l) (vla-put-Freeze   l :vlax-false)) (list lay))
            (vl-catch-all-apply
              '(lambda (l) (vla-put-LayerOn  l :vlax-true))  (list lay))

            ; Sanitizuj název vrstvy — nahraď nevhodné znaky podtržítkem
            ; mezera, /, \, :, *, ?, ", <, >, | → _
            ; tečka → , (zachováme čárku pro čísla jako "6,5mm")
            (setq safename lname)
            (foreach bad_ch
              (list " " "/" "\\" ":" "*" "?" "\"" "<" ">" "|")
              (setq safename (vl-string-subst "_" bad_ch safename)))
            (setq safename (vl-string-subst "," "." safename))

            ; Cesta k STL souboru
            (setq stl_path (strcat outdir "/" safename ".stl"))

            ; Export STL (vybere znovu jen objekty na téhle vrstvě)
            (setq sel2 (ssget "_X" (list (cons 0 "3DSOLID") (cons 8 lname))))
            (if (and sel2 (> (sslength sel2) 0))
              (progn
                (command "._STLOUT" sel2 "" "Y" stl_path)
                (setq exported (cons lname exported))
                (princ (strcat "\n  ✓ " lname " → " safename ".stl"))
              )
            )

            ; Obnov stav zmrazení všech vrstev
            (foreach state orig_freeze_states
              (setq restore_lay
                (vla-Item layers (car state)))
              ; Zapni všechny (vypnuté CLAYER)
              (vl-catch-all-apply
                '(lambda (l) (vla-put-LayerOn l :vlax-true)) (list restore_lay))
              ; Obnov freeze
              (if (not (equal (car state) curr_layer))
                (vl-catch-all-apply
                  '(lambda (l freeze) (vla-put-Freeze l freeze))
                  (list restore_lay (cadr state)))
              )
            )
          )

          ; Vrstva nemá 3D solid objekty
          (setq skipped (cons lname skipped))
        )
      )
    )
  )

  ; ── Finální obnova stavu ─────────────────────────────────
  (foreach state orig_freeze_states
    (setq restore_lay (vla-Item layers (car state)))
    (vl-catch-all-apply
      '(lambda (l) (vla-put-LayerOn l :vlax-true)) (list restore_lay))
    (if (not (equal (car state) curr_layer))
      (vl-catch-all-apply
        '(lambda (l freeze) (vla-put-Freeze l freeze))
        (list restore_lay (cadr state)))
    )
  )

  ; ── Výsledná zpráva ──────────────────────────────────────
  (princ (strcat "\n\n=== Export dokončen ==="))
  (princ (strcat "\nExportováno: " (itoa (length exported)) " vrstev"))
  (princ (strcat "\nSložka: " outdir))
  (princ "\n\nDalší krok:  Finder → otevři složku → Vybrat vše (⌘A) → pravý klik → Komprimovat")
  (princ "\nVýsledný ZIP nahraj do systému Razzor v záložce 3D v BOM editoru.")

  (alert (strcat
    "Hotovo! Exportováno " (itoa (length exported)) " vrstev.\n\n"
    "Složka se STL soubory:\n" outdir "\n\n"
    "Postup:\n"
    "1. Otevři složku ve Finderu\n"
    "2. Vyber vše (⌘A)\n"
    "3. Pravý klik → Komprimovat\n"
    "4. Nahraj ZIP do Razzor → BOM editor → záložka 3D"))

  (princ)
)

; Nápověda
(princ "\nRazzor 3D Export načten. Spusť příkazem: ExportLayers3D\n")
(princ)
