# 待办交接（2026-08-09 · 第二轮）

分支 `agent/root-scan-runner`。下一个会话从「立刻能做的下一步」那节开始。

验证基线（本轮末尾全绿）：

```bash
python -m pytest tests -q && python -m ruff check src tests && python -m mypy src
```

`680 passed` / `All checks passed!` / `no issues found in 82 source files`。

---

## 一、需求状态

| # | 需求 | 状态 |
|---|------|------|
| 1 | 控制台局域网可访问、不占 8000 | **已完成**（上一轮） |
| 2 | 08/08 战报及之后的详情页换中文舰艇名 | **已完成**（其实上一轮就完成了，见下） |
| 3 | 海盗攻击模式 | **判定输入已做完，驱动循环未写** |
| 4 | 控制台整合：去掉定时，保留开始/结束 + 选任务种类 | **已完成**（扩写成常驻调度器，见文末） |
| 追加 | 攻击日志（双时间戳、预设、bot/海盗） | **已完成**，本轮补上「战果」列 |
| 追加 | 海盗战报只记胜负与战损总数 | **已完成**（用户本轮口径） |

---

## 二、本轮改了什么

### 海盗战报：只记胜负 + 战损总数（用户口径，为省性能）

明细要进回放页、读两列名称与数量、还要反复重拍到合计对上，一份报告两三秒；
海盗全是同一个预设打的，逐舰种没有分析价值。所以这条链路**只看详情页**。

- `vision/pirate_reports.py`：轻量读取（胜负横幅 + 单位/损失单位总数 + VS 坐标）
- `vision/optional/report_screens.py`：新增 `loss_totals()`、`outcome_banner()`
- `battle_reports` 新增 `outcome` / `attacker_losses` / `defender_losses`
  （迁移 `a91c6d4e8b07`，三个都可空——旧战报没读过这些，补值等于凭空造战果）
- `tools/ingest_pirate_report.py`：`--detail` + `--bottom` 两张截图入库
- `/logs` 新增「战果」列；`AttackLogView` 经 `dispatch_id` 接上战报
- **不写 `fleet_snapshots`**：明细混进去，情报中心会把海盗的预设当成对方的舰队

两封真实海盗战报已入库（09/08 04:38:46、03:21:01，都是 VICTORY，战损 0/783，明细 0 行）。
库已备份到 `var/evo-helper.backup-20260809.db`。

### 侦察报告：判定输入（需求 3 的一半）

`vision/scout_reports.py` + `report_screens.named_counts()`。只读四个判定舰种，
不读资源、建筑、全部 21 行。实拍验证通过（`var/logs/scout1-*.png`）：
`深空吞噬者 2 / 噬能截击者 4 / 收割者 0`，`钛能守卫者` 没读出来，判定仍为 **打**。

**判据是三值不是布尔**（`VERDICT_ATTACK` / `SKIP` / `UNREADABLE`），不对称：

- 读到任一 > 1 → 打。缺的格子只会让对方更强，不会让结论反过来。
- 都 ≤ 1 但有格子没读出来 → **不下结论**。缺的那格可能正是一支舰队。
- 四格都读出来且都 ≤ 1 → 不值得打。

### 顺手修掉的三个 OCR 坑（都是实测撞出来的）

1. **锚点不能取最后一条亮带**。详情页拖到底之后，最靠下的亮带是黄色的
   「查看战斗回放」按钮，照它算出来的行落在空白上。改成「哪条亮带下面
   第一行是两个能解析的数，就是它」——判据和答案是同一件事。
2. **行窗高度 24 → 20**。窗口碰到下一行的顶边，`--psm 7` 当场读空。
3. **等距网格在长清单上会漂**。侦察报告行距 27.5px，取整成 27 到第 12 行
   就差半行，`钛能守卫者` 那格因此落在框外。按名字取数时改用每行**实测**的 y。
   （`read_fleet_rows` 仍用网格，那边是刻意的——它要给「整行没认出来」补位置。）

另外 `TOTALS_RECIPES` 比 `COUNT_RECIPES` 多一档 **2×**：战损常是孤零零一个 `0`，
实测只有 2× 读得出来，放大反而更差。

### 编辑距离收成一份

`domain/text.py` 的 `edit_distance` / `snap_to_vocabulary`，
`vision.parsers` 与 `game.pirate_ui` 的两份私有实现都改成用它。

---

## 三、纠正上一版交接里的三条错误信息

1. **详情页「回合行与终局行混显」早就修好了**（提交 6033789），
   `tests/integration/storage/test_report_detail_rounds.py` 两条用例守着。
2. **08/08 13:09:51 那份战报的舰种名早就是中文了**。`repair_ship_names` 复跑
   报「没有需要改名的行」。上一版列的那串乱码（SRLS HL / BHR / MEM…）是旧的。
3. **信箱里翻不到 08/08 的报告了**——列表只剩 8 封，最老一封是 09/08 00:38:34。
   重拍那条路已经走不通。

⚠️ **但那份战报的数量是错的，而且修不了**：守方参战存 81
（17/31/13/1/6/8/4/1），详情页「单位」是 247，回放截图上真值是
117/31/13/11/6/8/4/1/39/17。这是 `fleet_counts` 那套「合计对不上就拒收」
上线之前入库的旧数据；邮件已经没了，拿不到滚动后的回放截图。
要么留着（建议在页面上标出来），要么删掉这份的明细行只留总数。

---

## 四、实机标定（本轮新增，都是点通过的）

坐标是 client 空间、1920×917（含 Chrome `--app` 那条 38px 标题栏）。
版面 ROI 是裁掉标题栏之后的 1920×879。

### 战报详情页 → 回放页

```
邮件行(900, 285 + index*86)  → 详情页
  「战斗详情」只是分节标题，不是按钮，点了没反应
  慢拖一次（960,700 → 960,300）→ 露出黄色「查看战斗回放」(959, 761)
```

### 拖动必须是慢拖

一步到位的 `pyautogui.dragTo` 会被游戏面板当成点击——**同样的起止点，
有时滚有时不滚**。要「按下 → 分步移动（12–14 步）→ 停一下 → 松开」。
本轮 `scratchpad/slow_drag.py` 是可用的样子，正式代码里应该包成
`HumanInput.drag(steps=)`。

面板拖到底会**夹住**，落点稳定：实测拖 280px 与拖 520px 结果完全一致。
这就是「拖到底」能当标定姿势用的原因。

### 信箱

- 「报告」标签页列全部报告，共 8 封，翻一屏就到底。
- 第二行那排按钮（战斗/侦察/舰队/系统）是**隐藏开关不是筛选器**：
  点「战斗」之后列表只剩侦察报告，再点一次恢复。想找攻击报告别用它。
- 底部三个按钮最右边是删除，任何情况下都不要点（917 空间约 (1099, 833)）。

### 侦察报告

```
未滚动：开头那行有出发与目标坐标（ROI 见 scout_reports.SCOUT_INTRO_LINE_ROI）
慢拖两次 → 战舰清单尾段，四个判定舰种都在这一段
```

坐标那一行**中英混读读不出来**（`[2:137:18]` 读成 `[e:137:18]`，
`[2:137:4]` 读成 `[137:4]`——首位被吃掉而剩下的仍像合法片段）。
必须数字白名单 + `eng`，多套配方各读一遍，再按「恰好两个且都在范围内」挑。

---

## 五、立刻能做的下一步

### 第一步：`pirate_loop` 只差「回到自己星球地表」这一步坐标

`tools/pirate_loop.py` 已经实机跑通了侦查半条：2:137 的 1–4 位全部认出
「敌对海盗」，**四发侦察全部派出**，每一发都过了简报闸门并正确退出「飞行中」列表。

```bash
python -m evo_helper.tools.pirate_loop --systems 2:137                    # 只扫，不派
python -m evo_helper.tools.pirate_loop --systems 2:137 --scout            # 加上侦察
python -m evo_helper.tools.pirate_loop --systems 2:137 --scout --attack   # 完整循环
```

`--attack` 现在会停在「切不到自己星球地表，读不了信箱；安全停止」。缺的就是这一个坐标：

> 点底部导航的「行星」`NAV_PLANET (840,862)` 开出来的**不是**地表，而是
> **行星列表浮层**（我的三颗星球各一行：奥格瑞玛 [2:137:18]、风暴哨壁 [9:250:8]、
> 纳克萨玛斯 [4:96:7]），每行右侧一排八个图标，其中「前往此处」才是去地表的那个。
> 在 1536×733 那次截图里它大约在 (1168, 262)——**换算到 1920×917 要重新量**。
>
> ⚠️ 同一排里还有 运输 / 部署 / 传送 / 转移 / 投送 / 保护 / 扩张，
> 点错任何一个都是真实操作。量之前不许往那排图标上点。

量完之后 `_goto_planet_surface()` 改成「点行星 → 点该行的前往此处 → 回读星球名横条」，
`--attack` 就能走完整条链路。

### 实机事故（已加护栏，但要记住这个教训）

第一次跑 `--scout --attack` 时，派出侦察之后游戏停在「飞行中」列表上，而代码以为
还在恒星系视图，照着信箱路径连点三下——**第一下点在了某条探索任务的「取消」上**，
游戏弹出「确定要取消该任务吗？此行星的自动探索导航将被停用。」。人工否掉了，
用户的三条探索任务都还在跑。

加的三条护栏：派出之后必须自己退出「飞行中」列表并回读导航栏标签确认；
去信箱的每一步都先认屏再点，认不出就抛异常停下；开工先 `ensure_game_window()` 校几何
（本轮两次发现窗口自己变回 1536×733，那时所有坐标一起失效且悄无声息）。

### 第二步：需求 4 —— **已完成**，但扩写成了一个调度器

澄清后发现用户要的不是「换个下拉框」，而是带优先级、共享航线、有日配额的**常驻调度器**：
勾选并拖拽三条链路的次序，优先打满海盗每日 32 次，然后扫描+攻击 bot，
**在等航线的间隙插入全星系扫描**，攻击到点回来收战报再开下一轮。

设计规格：`docs/superpowers/specs/2026-08-09-mission-scheduler-design.md`（十二节，权威）。

**上一版这里写的三条建议现已全部作废**：不需要给 `scan_plans` 加 `mission_kind`
（三条链路参数形状不通，另建了 `mission_tasks`）；不需要「`window_start`/`window_end`
先别删」（本次一列都没碰，那份引用面清单不再有意义）；不需要给 runner 加 `--once`
（两个 runner 的 `run()` 本来就单趟遍历完就 return）。

**⚠️ 整套东西一次实机都没跑过。** 页面上那个「开始」按钮从来没被点过——点下去会真的
拉起子进程去点鼠标、派真实舰队。下一轮开工的第一件事应该是**在监视下点一次**。

### 需求 4 之后仍然敞着的口子

**调度器已合入 main**（PR #59，合并提交 `e8695e1`）。940 passed / ruff check /
ruff format / mypy 四条全绿。

**剩下的全部是实机工作，由 issue #60 跟踪**：

1. 首次监督运行——「开始」按钮**从未被点击过**，`mission_runs` 是 0 条
2. 回自己星球地表的坐标（走**左下角的切换星系**，不是底部导航的「行星」）
3. 三个派遣弹窗的 ROI 与关键词（见规格第十三节）
4. 核对侦察简报的飞行时间 ROI（现在假定与攻击简报同位置，未实测）

**上一版这里列的两条已经关闭，不要再去做**：

- ~~配额的「硬信号」需要撞到超限、截下那封邮件~~ —— **不需要了**。第二道保险是
  游戏自己的禁止；而且用户口径（2026-08-10）是开启海盗任务的当天完全不手打、
  开任务之前也不打，所以「数当天派了几发」本身就是准的。
- ~~`BRIEFING_ARRIVAL_ROI` 要标定，好切回双来源交叉校验~~ —— **不需要了**。用户
  口径：读到时长就够了（本地记发出时刻 + 时长即可）。`MAX_CREDIBLE_FLIGHT = 6 小时`
  是长期方案而不是临时防线。

### 一条给下一个人的提醒：基线是四条不是三条

```bash
python -m pytest tests -q
python -m ruff check src tests
python -m ruff format --check src tests    # ← 这条最容易漏，CI 会查
python -m mypy src
```

本轮就漏了 `format --check`，一路带到 CI 才炸。而这个教训仓库历史里早就记着了
（提交 `230208c`）。

### 会话稳定性

连续跑之前先接 `game/session_keeper.py` 与 `game/reconnect.py`。
实机守卫在游戏窗口抢不到前台时会拒绝点击（`RuntimeError: 游戏窗口抢不到前台`）——
这是设计如此，别绕过它。

窗口尺寸每次开工先核一遍：本轮开工时窗口是 1536×733，所有坐标全体失效。
`game_window.ensure_game_window()` 会把它调回 1920×917。

---

## 六、当前工作区状态

未提交（**本轮结束时仍未提交**，包含上一轮遗留的四个文件）：

```
M  src/evo_helper/application/report_ingest.py
M  src/evo_helper/domain/records.py
M  src/evo_helper/game/pirate_ui.py
M  src/evo_helper/storage/models.py
M  src/evo_helper/storage/repository.py
M  src/evo_helper/tools/ingest_report.py
M  src/evo_helper/tools/repair_ship_names.py
M  src/evo_helper/tools/scan_coordinates.py
M  src/evo_helper/vision/optional/report_screens.py
M  src/evo_helper/vision/parsers.py
M  src/evo_helper/vision/report_layout.py
M  src/evo_helper/web/persistent_service.py
M  src/evo_helper/web/service.py
M  src/evo_helper/web/templates/logs.html
?? alembic/versions/a91c6d4e8b07_pirate_outcome_and_losses.py
?? src/evo_helper/domain/text.py
?? src/evo_helper/vision/pirate_reports.py
?? src/evo_helper/vision/scout_reports.py
?? src/evo_helper/tools/ingest_pirate_report.py
?? tests/unit/vision/test_pirate_reports.py
?? tests/unit/vision/test_scout_reports.py
?? tests/integration/storage/test_pirate_report_persistence.py
?? tests/e2e/test_attack_log_page.py
?? .changes/27-lan-console.md
```

数据库已经跑过 `alembic upgrade head`（当前 head `a91c6d4e8b07`），
备份在 `var/evo-helper.backup-20260809.db`。

证据截图（`var/` 不进 Git，删了就没了）：

```
var/logs/pir1-detail.png   var/logs/pir1-bottom.png   海盗战报 04:38:46
var/logs/pir2-detail.png   var/logs/pir2-bottom.png   海盗战报 03:21:01
var/logs/scout1-detail.png var/logs/scout1-ships.png  侦察报告 02:55:37
var/logs/pir1-replay.png                              海盗战报的回放页（AAA 预设实证）
```
