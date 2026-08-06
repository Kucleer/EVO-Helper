# Safety invariants

The default configuration is `dry_run=true`. It is prohibited for tests, tools, vision code, or web
routes to directly invoke a real attack. A future dispatch adapter must require a valid, unused,
unexpired ActionGuard token and must re-observe the screen immediately before a click.

The helper must pause safely if UI version, focus, display geometry, key OCR fields, or coordinate
sources are uncertain or inconsistent. `pyautogui.FAILSAFE` must remain enabled.
