; Carrot NSIS customisation — a fast uninstall.
;
; electron-builder's generated uninstaller removes the app file by file, from
; a list baked in at build time. That is fine for a normal Electron app of a
; few thousand files. Carrot ships a frozen Python runtime *and* a full
; Chromium for the Agent tab, which together run to tens of thousands of small
; files, and a per-file uninstall of that takes minutes with the progress bar
; apparently stuck — long enough that people kill it and leave a half-removed
; install behind.
;
; RMDir /r is a single recursive delete handled by the OS and finishes in
; seconds. This macro replaces the generated file removal entirely.
;
; Deliberately plain NSIS (StrCmp / IfFileExists rather than LogicLib, and no
; reference to electron-builder's internal macros): this file is compiled only
; on the Windows CI runner, so anything that does not compile fails the build
; rather than a local test.

!macro customRemoveFiles
  ; Guard the recursive delete. RMDir /r on an empty or unexpected $INSTDIR
  ; would take whatever it does point at, so refuse unless this looks like a
  ; real Carrot install — the marker is our own bundled backend.
  StrCmp $INSTDIR "" carrot_skip_fast 0
  IfFileExists "$INSTDIR\resources\backend\*.*" 0 carrot_skip_fast

    DetailPrint "Removing bundled browser and backend…"
    RMDir /r "$INSTDIR\resources\pw-browsers"
    RMDir /r "$INSTDIR\resources\backend"
    RMDir /r "$INSTDIR\resources"

    DetailPrint "Removing application files…"
    RMDir /r "$INSTDIR\locales"
    RMDir /r "$INSTDIR\swiftshader"
    Delete "$INSTDIR\*.*"
    ; Leaves $INSTDIR itself for the generated uninstaller, which still has to
    ; delete its own executable after this macro returns.
    Goto carrot_removed

  carrot_skip_fast:
    ; Not a layout we recognise. Delete what is safe to name and leave the
    ; rest alone rather than recursing into an unknown directory.
    DetailPrint "Removing application files…"
    Delete "$INSTDIR\*.*"

  carrot_removed:
!macroend
