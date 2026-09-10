---
issue: 310
agent: vision-game
type: Fixed
date: 2026-09-11
---

**⚠️⚠️ 贴词函数恒返回 `None` —— 读得一字不差的「回收」也贴不出来。**

`snap_panel_label` 的 `max_distance` 是 2，而词表**每一项都是两个汉字**：
任意两个两字词的编辑距离最大就是 2 ⇒ 任何输入都命中全部六项 ⇒ 判歧义 ⇒
**恒返回 `None`**。回收链路一发都派不出去的真凶。

阈值改 1，并把「**不能**放宽到 2」写成用例钉死。

外加：原来把整行 500px 交给 tesseract，读出 `'以而 TARR ia te} eX'`；
改成**逐格 72px 读**，同一帧得到 `'攻击' '侦察' '扫描' '回收'` 全对。

- Database: 无迁移
- Verification: `pytest -q` 全绿；实拍图上逐格试过配方
- Safety: 阈值收紧只会更保守
- Rollback: revert 本 PR
