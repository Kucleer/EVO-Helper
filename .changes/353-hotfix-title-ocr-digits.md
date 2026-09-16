---
issue: 353
agent: vision-game
type: Fixed
date: 2026-09-16
---

**#356 标题 OCR 漏传 `digits`，色判据在生产上一次都没生效。**

```
读不出二级标签的选中态（make_ocr.<locals>.ocr() missing 1 required
keyword-only argument: 'digits'）；回到按列表内容判的老路
```

`scan_coordinates.make_ocr().ocr` 把 `digits` / `upscale` 都声明成**必填关键字**。
`_read` 一直传着，新写的标题确认漏了 `digits`，于是每次进 except、永远回退列表判据
——等于 #356 的颜色路径在生产上是死的。

补上 `digits=False`。

- Configuration: 无
- Database: 无
- Verification: 生产日志不再出现 `missing 1 required keyword-only argument`。
- Safety: 只补一个参数。
- Rollback: 回退本提交；回退后色判据仍全部回退列表老路（与 #356 上线时一样）。
