"""Fleet preset identity: a name plus a composition signature.

Safety invariant 9 requires the preset name, ship types and counts to all
match before an attack may be dispatched. The name alone is not enough, and
for this account it is actively fragile: the user's preset is named ``探路``,
two characters, which is below the OCR snap threshold — a misread cannot be
repaired against the unit vocabulary the way a ship name can. The composition
signature is the durable half of the check.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class FleetPreset:
    name: str
    signature: str


def composition_signature(counts: Mapping[str, int]) -> str:
    """Canonical ``name:count`` signature, sorted so ordering cannot change it."""
    return ",".join(f"{ship}:{count}" for ship, count in sorted(counts.items()))


#: The in-game preset this account scans with: 探路 = 轻型战斗机 x1.
DEFAULT_PRESET = FleetPreset(name="探路", signature=composition_signature({"轻型战斗机": 1}))
