---
issue: 9
agent: root
type: Added
date: 2026-08-07
---

新增 `evo_helper.vision.optional.report_screens.ImageReportScreens`：`ReportScreens` 的实现，
按 `LIVE_LAYOUT` 裁 ROI、放大后送 Tesseract（`chi_sim+eng`）。Pillow / pytesseract 属 `vision` extra，
缺失时抛错而非静默降级到错误结果。

同时**修正**上一条 `.changes/9-report-roi-layout.md` 中一个用眼睛而非用 OCR 验证的结论：

- `BINARIZE_THRESHOLD = 140` 已移除。140 确实能把面板里的暗色装饰文字去干净——看图是对的——
  但实测送进 Tesseract 反而**更差**，因为它破坏了 Tesseract 自己的自适应二值化：
  计数出现 `95`→`a5`、`166`→`165`、`16`→`15`。改为**不二值化**，只做灰度 + LANCZOS 放大
  （`OCR_UPSCALE = 4`）。装饰文字本身足够暗，Tesseract 会自行丢弃——装饰文字最密的攻方列
  实测不产生任何伪造行。
- 坐标改为**各自单独一个窄 ROI、`--psm 7` + 数字白名单**读取。在整块 VS 宽裁图里
  Tesseract 会把 `[2:137:18]` 读成 `[e:137:18]`，随后坐标正则匹配失败；单独读则双方都精确。

新增离线回归 `tests/integration/vision/test_live_batch_ocr.py`，直接跑批次原图：

- 双方坐标 `[2:137:18]` / `[2:149:17]` 精确
- 报告时间 `06/08/2026 11:45:03` → `2026-08-06T11:45:03+00:00`
- 参战舰队 17 个计数全部正确（攻方 2 行、守方 15 行）
- 装饰文字未产生伪造行
- 当前邮件列表页不含 `攻击报告`，`list_attack_reports` 正确返回空

- 配置：新增可选环境变量 `TESSERACT_CMD`，默认 `C:\Program Files\Tesseract-OCR\tesseract.exe`
- 数据库：无变更
- 验证：`pytest`（158 passed，缺 vision extra 时离线回归自动 skip）、`ruff check src tests`、
  `ruff format --check src tests`、`mypy src`；离线回归在装有 vision extra 的解释器上 4 passed
- 安全：纯只读解析，未新增点击路径；`dry_run` 仍为 true
- 回滚：删除 `src/evo_helper/vision/optional/report_screens.py` 与该离线回归
