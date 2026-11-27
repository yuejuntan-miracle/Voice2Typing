#Requires AutoHotkey v2.0
; Voice Dictation Side-Button Suppressor (Phase 5 Option B)
; Prevents XButton2 (Forward) from triggering browser navigation.
; The low-level hook inside dictate.py already suppresses this natively,
; but this script can be used if you prefer external AutoHotkey control.

$*XButton2::
{
    ; Do nothing - blocks the Forward navigation keystroke from reaching the browser
    return
}
