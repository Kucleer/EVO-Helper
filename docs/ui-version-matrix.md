# UI version matrix

Each independently parsed screen has its own version. A single release label
must never be used to approve the whole game flow.

| Screen | Runtime version field | Current baseline | Status | Safe fallback |
| --- | --- | --- | --- | --- |
| Galaxy / planet | `galaxy_ui_version` | `galaxy-v2` contract only | Needs current samples | Pause navigation |
| Planet action panel | `galaxy_ui_version` | Unknown | Needs current samples | Pause target action |
| Fleet preset / attack | `attack_ui_version` | `attack-v2` contract only | Needs current samples | Reject final action |
| Mail list | `mail_list_ui_version` | `mail-list-v2` | Must be recollected | Pause report navigation |
| Battle detail | `battle_detail_ui_version` | Archived fixture baseline | Regression only | Manual review |
| Battle replay | `battle_replay_ui_version` | Archived fixture baseline | Regression only | Manual review |

## Versioning rules

- A parser accepts only its declared UI version and fails closed on unknown
  layouts.
- Every observation stores the screen and its specific UI version with the
  evidence artifact.
- The legacy 7/21 mail list is permanently archival. It cannot appear in a
  current-mail training, validation, or regression manifest.
- Battle detail and replay fixtures remain valid only for their own parsers;
  they do not establish mail-list compatibility.
- A new current UI baseline requires a manifest with SHA-256 hashes, viewport
  metadata, independent-session split information, and reviewed confidence
  metrics before it can change this matrix from “Needs current samples”.
