---
issue: 57
agent: web-api
type: Fixed
date: 2026-08-17
---

军力值不再带一串浮点尾巴：换算在源头改走 `Decimal`，库里已有的脏值在显示层收一次。

军力榜页面上出现 `64959.99999999999` / `64260.00000000001` / `64180.00000000001`
这种值。成因在 `tools/ranking_scan.py` 的 `parse_score`：榜上读到的原文是
`64.96K`，而那里写的是 `float("64.96") * 1_000.0`——`64.96` 这个十进制小数在
二进制里没有精确表示，乘完误差就露出来了。不是 OCR 读错，三个值除以 1000
都精确落回原文。

**源头改成十进制乘法**：`float(Decimal("64.96") * 1_000)` 恰好是 64960.0。

**没有改成「乘完取整」**，尽管榜上的刻度允许：K 值只到两位小数（最小刻度
0.01K = 10）、M 值同样两位（0.01M = 10000）。但那条正则同时认**没有单位的裸数**
（`(\d+(?:\.\d+)?)\s*([KM])?`），取整那一支会把 `1.5` 抹成 `2`。`Decimal`
对三种单位一视同仁地精确，不需要靠「最小刻度是 10」这个前提兜底。

**显示层另收一次**（`web/display.settled_score`，两位小数），因为库里
`bot_targets.military_score` 已经存了一批脏值，而这一轮不碰生产库——历史值不许
UPDATE，只能在读出来交给页面的那一步收，以后重采自然覆盖。刻度停在两位小数
而不是整数位：`domain.ranking.interpolate_scores` 取中点，两个已知值之和为奇数时
必然带 `.5`（页面上的 `72252.5 (估算)` 就是），那是**合法值不是误差**，
收到整数位就等于把一个真值报错。

收敛的只是**显示**：筛选（`score_min` / `score_max`）、分档
（`domain.military_attack.tier_for`）、排序全都还读原值——那几处差 1e-11
不改变任何结论，而在那里动手才是真的在改数据。

- Configuration: 无。
- Database: **无迁移、无 schema 变更、一行都没写过生产库。** 历史脏值原样留在
  `bot_targets.military_score` 里，靠显示层收敛，重采时被新的干净值覆盖。
- Verification: 2170 passed（起点 2162）/ ruff check + format `src tests` 全绿 /
  mypy 117 源文件零问题。三处变异各验过一次（改坏 → 测试变红 → 还原 → 复跑全绿）：
  ① `parse_score` 退回 `float(...) * 1_000.0` → 1 条红
  （`test_the_k_suffix_lands_exactly_on_the_listed_value`）；
  ② 取整改得过激（`parse_score` 乘完 `round()`，且 `SCORE_DECIMALS` 改成 0）
  → 3 条红，其中两条正是「裸数 `1.5` 不许被抹平」与「插值的 `.5` 不许被抹掉」；
  ③ 路由里那一次 `settled_score(...)` 调用删掉 → 1 条红（e2e，钉的是接线而不是
  那个函数——单元测试全绿也挡不住脏值一路上页面）。
  真实样本钉的是**恰好相等**，没有用 `pytest.approx`。
- Safety: 纯读数与显示，一次点击都不产生。`ranking_scan` 仍然只导航、读数、入库，
  不开 `allow_actions`。
- Rollback: 还原这三个文件即可，没有需要回滚的数据变更。
