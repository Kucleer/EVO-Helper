"""在游戏里把「当前星球」切到指定坐标，或者只演一遍给人看。

    # 只打印「打算点哪里、因为读到了什么」，一次都不点
    python -m evo_helper.tools.switch_planet --origin 9:250:8 --dry-run

    # 真的点「前往此处」，并回读派遣面板的「起点」确认
    python -m evo_helper.tools.switch_planet --origin 9:250:8 --commit

两档**必须显式选一个**，没有默认值。照 `pirate_loop --attack` 的规矩来：
会动鼠标的那一档不许是「不写参数时顺手发生的事」。

`--dry-run` 那一档仍然会开浮层、也仍然会拖——要给人看的正是「它认到的是不是
那一行」，而那个答案只能从真实画面上来。这两个动作都只翻自己的星球清单。
它不会点「前往此处」，也不会去开派遣面板回读。
"""

from __future__ import annotations

import argparse
import sys

from evo_helper.game.planet_list import SwitchResult
from evo_helper.tools.pirate_loop import LoopOptions, PirateLoop, parse_origin
from evo_helper.tools.scan_coordinates import LiveDriver, make_ocr, say


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", type=parse_origin, required=True, help="要切到的星球坐标")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="只打印打算点哪里，一次都不点")
    mode.add_argument("--commit", action="store_true", help="真的点「前往此处」并回读确认")
    args = parser.parse_args(argv)

    import ctypes

    getattr(ctypes, "windll").shcore.SetProcessDpiAwareness(2)

    say(f"出发星球：{'演一遍' if args.dry_run else '真的切'} → {args.origin}")
    # `allow_actions` 只在 `--commit` 那一档打开。开关只有这一处，
    # 与 `pirate_loop` / `bot_loop` 同一规矩。
    #
    # ⚠️ dry-run 那一档也要能开浮层和拖动，所以这里不能靠 `allow_actions` 挡；
    # 真正挡住「点前往此处」的是 `PlanetSwitcher.dry_run`。两道各挡各的：
    # 这一道挡住的是「万一哪天有人在 dry-run 路径上加了别的点击」。
    driver = LiveDriver(allow_actions=True)
    driver.window()
    loop = PirateLoop(driver, make_ocr(), LoopOptions(systems=(), scout=False, attack=False))
    switcher = loop.planet_switcher(dry_run=args.dry_run)

    from evo_helper.game.game_window import ensure_game_window

    ensure_game_window()
    result = switcher.switch_to(args.origin)
    say(f"结局：{result.value}")
    return 0 if result in (SwitchResult.SWITCHED, SwitchResult.DRY_RUN) else 1


__all__ = ["main"]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
