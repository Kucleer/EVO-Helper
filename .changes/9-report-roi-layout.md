---
issue: 9
agent: root
type: Added
date: 2026-08-07
---

新增 `evo_helper.vision.report_layout`：在 `evo-20260807-live` 批次原图上实测的报告 ROI 几何。

- `LIVE_LAYOUT` 记录邮件行、报告头、详情/回放 VS 块、参战战舰左右列的像素框，全部在 1920×879 上量取并逐个裁图核对过（含以 86px 行距推到第 6 行仍准确对齐）。
- `layout_for_viewport` 对其他视口**直接报错而不缩放**。裁歪的框会静默截断 OCR 文本，而被截断的舰队列与「舰队更小」在结果上完全无法区分；且实测改窗口尺寸不刷新根本不会重排游戏画布，视口不符说明采集环境已漂移，应当修环境而不是近似。
- 回合区块会随滚动移动，因此 `ColumnBand` 只固定左右 x 边界，y 范围由采集时定位 `第N回合【剩余战舰】` 横幅后给出，不写死。
- `BINARIZE_THRESHOLD = 140`：报告面板在同一列里渲染了暗色装饰文字（`COMMAND OFFICERS` / `TOTAL CREWS` / `-17003` / `personnel`），会污染 OCR。实测 140 能完全去掉装饰层且前景笔画完整；170 已开始侵蚀中文字形，故取 140 留双向余量。

- 配置：无变更
- 数据库：无变更
- 验证：`pytest`（156 passed）、`ruff check src tests`、`ruff format --check src tests`、`mypy src`
- 安全：纯几何常量与取值门槛，未新增点击路径；`dry_run` 仍为 true
- 回滚：删除 `src/evo_helper/vision/report_layout.py` 与 `tests/unit/vision/test_report_layout.py`
