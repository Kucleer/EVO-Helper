# Safety invariants

There is no rehearsal mode: a dispatch is always a real dispatch. It is prohibited for tests, tools,
vision code, or web routes to directly invoke an attack. Every dispatch adapter must require a
valid, unused, unexpired ActionGuard token and must re-observe the screen immediately before a
click; that token, not a global flag, is the only thing standing between a plan and a click.

The helper must pause safely if UI version, focus, display geometry, key OCR fields, or coordinate
sources are uncertain or inconsistent. `pyautogui.FAILSAFE` must remain enabled.
