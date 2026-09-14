---
issue: 342
agent: vision-game
type: Fixed
date: 2026-09-14
---

**切不了二级标签的那一趟不许记成「翻完了」** —— 两处提前返回漏了 `aborted=True`。

## 病

`_scan_mail_rows` 里两处「切不了二级标签就提前返回」只给了 `cut_short`，
而完成判据 `tally.swept = not deadline_hit and not aborted` **不看 `cut_short`**：

    swept = True
       ↓
    record_daily_reconciliation(...)   ← 对账时刻照写
       ↓
    后续轮次因冷却跳过

**一趟什么都没扫的行程被记成「翻完了」。**
⚠️ 这与 2026-09-13 夜那次事故是**同一个病**：失败的一趟被记成成功的一趟。

⚠️⚠️ `aborted` 的 docstring 里**逐字写着这一档** ——
「半路出事（丢了邮件列表重进不回、**切不回二级标签**）」。字段就是为它设计的，我没设。

⚠️ 两处里 `fleet_sub_tab=True` 那个是 `#336` 就有的，不是 `#340` 新引入。

## 用例：三条，每条都做过删除实验

- `test_a_trip_that_scanned_nothing_is_never_recorded_as_swept` ——
  真跑那两条路径，断言返回对象的 `aborted`，并按 `_scan_for_reconcile` 的算式断言 `swept is False`；
- `test_a_trip_that_gave_up_on_the_tab_writes_no_reconciliation` ——
  **真跑 `reconcile_today`**，断言仓储侧一条对账记录都没写；
- `test_a_normal_trip_still_writes_its_reconciliation` ——
  反向：正常那趟必须照写，否则「永远不写」也能全绿。

⚠️⚠️ **第一版用例是假的**：它只搜源码里的 `aborted=True` 字符串，
而两处 `return` 的**注释里也有这串字** —— 删掉真参数只留注释，测试照样绿（当场试过）。
源码文本匹配分不清代码和注释（同 `guards-must-read-the-verdict-not-the-whole-source`）。
重写成真跑路径之后，再删一次参数它会红（已验）。

## ⚠️ 仍然开着（本次不修，已在源码原处登记）

1. `_select_mail_sub_tab` 重试时 `if attempt or self._on_mail_list():` **短路**，
   页面校验第二次起不执行 —— 「点完离开了列表」不被检查；
2. 筛选**双向误判**：没筛过的列表若混着舰队类与读不出的攻击报告，
   `here > 0 and other == 0` 同样成立，会**反手把筛选打开**；
3. 两处提前返回**绕过 `_close_mail()`**，信箱面板留在屏上，后续是否自行收尾未验。

**三条都需要实机证据才能动**，而这段代码正是 09-13 夜三个错叠在一起的地方。

## 附带：文档按五轮复核逐条订正

`docs/回收闭环/` 下的事故报告与修复说明。主要是把说过头的话收回：
「停摆 3.5 小时」→ 57 分钟；「216 轮」→ 42 轮；「27 份永久损失」→ 可恢复性待核；
判据阈值的「28/188 实测」→ 那 188 里 172 条来自故障窗口，最终只剩两组计数分布本身，
**识别效力至今未独立验证**。

- Configuration: 无
- Database: 无
- Verification: `ruff` 全绿；`mypy` 3 个**既有**错（`intel_query.py`、`mission_scheduler.py`，
  本次未碰这两个文件）；`pytest` 4647 passed / 264 skipped / 0 failed。
  另有针对性的删除实验：删掉 `aborted=True` → 相关用例红，还原 → 绿。
  ⚠️ **实机验收一次都没做**；本次效力止于「源码可核 + 单元用例」。
- Safety: 改动只让「已经放弃的那一趟」不再写对账时刻 —— 方向是**少记**，不会多跑或误点。
  反向用例钉住「正常那趟照写」，防止退化成「永远不写」。
- Rollback: 回退这个提交即可。回退后回到「放弃的一趟也会推进对账时刻」，
  也就是本次要修的那个缺陷。
