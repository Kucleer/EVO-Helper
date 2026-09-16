"""用生产 dump 复测 #353 采样框改动。

已知状态（肉眼 + 2026-09-16 逐像素核对）：

| 文件 | 战斗 | 侦察 | 舰队 | 说明 |
|---|---|---|---|---|
| dump-mail-sub-tab-舰队-unconfirmed-065255.png | 亮 | 灭 | 灭 | 昨早，列表全是攻击报告 |
| dump-mail-sub-tab-舰队-foreign-filter-102120.png | 亮 | left=47（同选中档） | 灭 | 今早 |
| dump-mail-sub-tab-战斗-foreign-filter-105142.png | — | — | — | **不是信箱**（行星视图） |
| dump-mail-sub-tab-舰队-foreign-filter-171217.png | 亮 | 亮 | 亮 | 昨下午，用户确认侦察真选中 |

⚠️ 102120 的 left=47 与 171217 真选中**同档**，颜色分不出悬停/真选中。
所以这一版的验收不是「102120 不再报侦察」，而是：
  · 065255 真·未选中 → 不含侦察
  · 侦察亮着 → **不再整趟收手**（单元测试钉住）
  · 不在信箱 → 色判据跳过
"""

from __future__ import annotations

from pathlib import Path

from evo_helper.tools.pirate_loop import (
    MAIL_SUB_TAB_LIT_R,
    MAIL_SUB_TAB_SAMPLES,
    mail_filters_that_are_on,
)

ROOT = Path(r"D:\eternal-void\var\logs")

CASES = [
    ("065255_off", "dump-mail-sub-tab-舰队-unconfirmed-065255.png", {"战斗"}, "侦察应灭"),
    ("102120_false", "dump-mail-sub-tab-舰队-foreign-filter-102120.png", {"战斗"}, "旧框误报侦察"),
    ("171217_true", "dump-mail-sub-tab-舰队-foreign-filter-171217.png", None, "昨下午多选，含侦察"),
]


def main() -> None:
    print(f"threshold={MAIL_SUB_TAB_LIT_R}")
    print(f"samples={MAIL_SUB_TAB_SAMPLES}")
    from PIL import Image

    for label, name, expect, note in CASES:
        path = ROOT / name
        if not path.exists():
            print(f"MISS {label} {name}")
            continue
        im = Image.open(path).convert("RGB")
        on = mail_filters_that_are_on(im)
        print(f"{label:14} on={sorted(on)}  expect~{expect}  ({note})")
        if expect is not None:
            ok = on == expect or (expect <= on and "侦察" not in on)
            print(f"{'  OK' if ok else '  FAIL'}")

    # 逐按钮报 left 条 avgR，方便肉眼对表
    print("\n--- left-strip avgR ---")
    for label, name, _, _ in CASES:
        path = ROOT / name
        if not path.exists():
            continue
        im = Image.open(path).convert("RGB")
        parts = []
        for btn, box in MAIL_SUB_TAB_SAMPLES.items():
            crop = im.crop(box)
            px = list(crop.getdata())
            avg = sum(p[0] for p in px) / len(px)
            parts.append(f"{btn}={avg:.1f}{'*' if avg >= MAIL_SUB_TAB_LIT_R else ' '}")
        print(f"{label:14} {' '.join(parts)}")


if __name__ == "__main__":
    main()
