---
issue: 18
agent: root
type: Fixed
date: 2026-08-07
---

出发星球不再要求落在扫描区间内，并把游戏内预设 `探路` 配置为默认值。

## 出发星球校验

`range origin must lie inside the range` 与业务规则冲突：出发星球是玩家自己的星球，
按方案 §2「每个坐标区间单独绑定出发星球」，它**大概率不在**被扫描的坐标区间内。
用真实坐标（从 `2:137:18` 攻击 `1:100–1:200` 的 bot）建任务时会被直接拒绝，任务中心不可用。

该校验在**两处**各有一份：`web/service.py`（Fake）与 `web/persistent_service.py`（持久化）。
先只改了 Fake 那份，浏览器实测仍报同样的错——因为控制台走的是持久化服务。两处均已移除。
区间自身「终点不得早于起点」的校验保留。

## 默认舰队预设

新增 `domain/fleet_preset.py`：`FleetPreset` 与 `composition_signature`，
`DEFAULT_PRESET = 探路 / 轻型战斗机:1`（用户提供的游戏内预设）。
`Settings` 暴露 `default_fleet_preset` 与 `default_fleet_preset_signature`，可按账号覆盖。
任务中心表单预填该预设。

**签名取舰种组成而非名称**。安全不变量 9 要求名称、舰种与数量三者都匹配，而 `探路` 只有两个字，
低于 OCR 吸附阈值 `MIN_SNAP_LENGTH = 3`——名称被读错时无法像舰种名那样靠词表修复。
组成签名（`轻型战斗机:1`，排序后拼接，与顺序无关）才是可靠的那一半。派遣前两者仍都要校验。

## 顺带修掉的表单缺陷

任务表单同时带 `data-api` 与自定义 submit 处理器，base.html 的通用处理器会用扁平 FormData
再提交一次（结构错误），造成重复提交。已移除 `data-api`。

- 配置：新增 `EVO_HELPER_DEFAULT_FLEET_PRESET` / `EVO_HELPER_DEFAULT_FLEET_PRESET_SIGNATURE`
- 数据库：无变更
- 验证：`pytest`（308 passed）、`ruff check src tests`、`ruff format --check src tests`、`mypy src`；
  新增用例覆盖 Fake 与持久化**两个**服务的区间外出发星球（此前只测了 Fake，缺陷才漏出去）；
  浏览器实测用真实坐标建成任务，`探路 / 轻型战斗机:1` 正确落库
- 安全：仅放宽一条与业务不符的配置校验；派遣前的预设签名校验不变，`dry_run` 仍为 true
- 回滚：恢复两处 origin 校验并删除 `fleet_preset.py`
