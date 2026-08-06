# Browser capture and recovery

## Purpose and safety boundary

Browser capture supplies evidence for the vision and game adapters. It must not
create a dispatch, submit a game form, read credentials, cookies, local storage,
or session tokens. Production automation stays `dry_run=true` until the CP6
evidence gate is complete.

Only the root agent controls the logged-in browser session. A capture session
records screenshots and its manifest under a unique `session_id`; no account
identifier belongs in filenames, manifests, logs, or source control.

## Reconnect state machine

```text
DISCOVER_TAB -> CLAIM_TAB -> OBSERVE
OBSERVE -> ENTRY_PAGE -> ENTER_ONCE -> WAIT_FOR_STABLE_GAME
OBSERVE -> GAME_READY -> CAPTURE
WAIT_FOR_STABLE_GAME -> GAME_READY | SAFETY_PAUSED
any state -> CONTROL_DISCONNECTED -> DISCOVER_TAB
```

- `ENTRY_PAGE` is recognized only by a known logged-in entry screen and a
  high-confidence visual observation.
- `ENTER_ONCE` may click the visible entry control once per reconnect attempt.
  It must not retry blindly.
- `WAIT_FOR_STABLE_GAME` requires two consecutive high-confidence game-ready
  observations before navigation resumes.
- `CONTROL_DISCONNECTED` preserves the browser tab and the capture session;
  reconnect starts with discovery rather than a reload or a new login.
- Any unknown screen, modal, CAPTCHA, low confidence, or conflicting visual
  evidence enters `SAFETY_PAUSED` and creates a diagnostic capture.

The game adapter uses `SessionRecoveryGate` and `RecoveringGameAdapter` to
enforce the same rule before galaxy navigation. Recovery never runs immediately
before final dispatch; `ActionGuard` remains the sole final-action gate.

## Current capture procedure

1. Use the existing EVO tab; do not reload it merely to collect a sample.
2. Record the viewport, browser zoom, display scale, UTC time, screen name, and
   UI version for every image.
3. Capture stable states and transition states separately: entry page, galaxy
   loading/ready/error, planet action panel, preset states, capacity states,
   current mail list, battle detail, and battle replay.
4. Write each image through `evo-capture` (or an equivalent platform adapter)
   and retain SHA-256, session id, and batch in its manifest.
   Validate live batches with `validate_capture_manifest`, which also requires
   artifact id, capture time, screen, UI version, viewport, source, and a
   matching batch value for every sample.
5. Mark the old 7/21 mail list as `is_legacy=true`; it is archival only and
   cannot be eligible for the current-mail baseline.
6. Use the newly captured mail-list session, not legacy mail images, for parser
   development, validation, and regression baselines.

Example (real screen capture on a prepared, visible game screen):

```powershell
evo-capture --platform mss --batch evo-20260806 --screen galaxy --ui-version galaxy-v2
```

The capture command records an explicit `eligible_for_current_mail_baseline`
flag. Set it only after the screen and version have been reviewed.

## Evidence needed before progression

| Gate | Required evidence | Result when missing |
| --- | --- | --- |
| Galaxy navigation | Ready/loading/error samples and two stable ready frames | Pause navigation |
| Target recognition | Normal, `bot_`, unknown, and blocked target samples | No intent or dispatch |
| Preset/attack UI | Correct, missing, mismatched, and insufficient preset samples | No final action |
| Capacity | Available, full, inconsistent, and delayed list samples | `WAITING_CAPACITY` |
| Mail list | Current version empty/list/unread/read/pagination/loading samples | Pause report navigation |
| Battle detail/replay | Normal and missing-field samples with viewport metadata | Manual review or pause |

## Control-channel incidents

When the browser-control channel times out but the tab remains discoverable,
do not retry an entry click or reload the game. Preserve the known tab identity,
perform a lightweight tab-discovery health check, then retry claiming the tab
once after the control channel reconnects. If the retry fails, gather only
non-invasive browser diagnostics and request user approval before opening a
new Chrome window. The local workflow, tests, and GitHub work may continue
while browser capture is paused.
