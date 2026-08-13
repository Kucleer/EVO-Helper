# Changelog

All notable changes to this project are documented in this file.

## [Unreleased]

> 本项目还没有发过任何版本，所以下面这一节就是全部历史。
>
> 两处语言：issue #4–#18 当时用英文写，原文保留；issue #19 起改用中文（约定见
> `.changes/README.md`）。英文那几条里被后来的改动**推翻**的句子已经就地更正，并在
> 括号里注明是被哪一条取代——不是为了好看重写，是因为留着就是错的。上一次合并片段
> 停在 #18，这一次把 #19–#46 一并合进来。

### Added

- Initial safety-first project bootstrap and frozen domain contracts.
  （原文还写着 "dry-run defaults"；演习模式已由 #35 整个删除，见「Removed」。）
- Domain & persistence (Wave 1): lexicographic coordinate ranges and cursor-based claiming,
  UTC+8 time-window scheduling with cross-day arming and DRAINING semantics, weekly-cycle
  dedupe and idempotent starts, SQLAlchemy 2 schema (all plan 8.1 tables), Alembic initial
  migration, append-only history with strict report-to-dispatch matching, and fleet diff
  computation (issues #4, #5).
- Vision pipeline (Wave 1): pluggable YOLO/OCR/template engines with safe offline fallbacks,
  deterministic UI parsers (mail list, battle detail, battle replay, galaxy, preset
  signature), multi-frame consistency, and three-source coordinate fusion with a 0.995
  confidence gate; 7/21 legacy UI annotation rules (issues #7, #8, #10).
- Game safety adapter (Wave 1): ActionGuard single-use short-lived dispatch tokens, fresh
  re-observation immediately before any click, line-capacity gate combining user limit,
  game feedback, and in-flight fleets (issue #11).
- Web/API (Wave 1): a FastAPI application with plans CRUD, idempotent manual run start,
  pause/resume/emergency-stop, run status, bot targets, coordinate history and fleet
  diff pages, forced revisits, and diagnostics; same-origin/local-token protection for
  mutating endpoints; application-service seam with an in-memory fake for tests (issues #12,
  #13). （原文写的是 "loopback-only"；#27 起默认绑 `0.0.0.0:8770`，那条硬校验已删，
  见「Changed」与「Security」。）
- Dataset tooling (Wave 1): capture CLI with manifest and SHA-256 evidence hashing, dataset
  utilities, optional `vision` dependency group, and `evo-capture`/`evo-dataset` console
  scripts.
- Application integration (issue #14): the safe workflow closing scanning, dispatch
  recording, and report draining; database-backed range bindings resolving each run's origin
  and preset signature; the SQLite-backed Web service and persistent application factory; and
  the `evo-web` command, which applies Alembic migrations before serving.
  （原文写的是 "dry-run dispatch recording"；#35 之后只剩一条真派遣路径。）
- Evidence stores (issue #6): artifact persistence with SHA-256 indexing, UI-observation
  records, strict live-capture metadata validation, and a fail-closed session-recovery wrapper
  for the logged-in entry page.
- Live report reading (issues #9, #10): measured report ROI geometry for the
  `evo-20260807-live` batch, an `ImageReportScreens` adapter cropping those ROIs into
  Tesseract, and a reader chaining mail list to attack report to battle replay.
- Report ingestion (issue #13): screenshots to OCR to domain records to SQLite to the Web UI,
  with one UI observation recorded per screen so no single version label stands for the chain.
- Code-driven capture (issue #13): single-window capture via `PrintWindow` with an `mss`
  fallback, cropped to the client area — never a full-screen grab, which would pick up
  unrelated windows. Verified to reach Chrome's WebGL canvas, including off-screen and
  unfocused.
- Humanised input (issue #13): randomised click offset, travel time, and pacing for capture
  navigation, refusing any dispatch/claim/delete label and requiring `pyautogui.FAILSAFE`.
- Intel search (issue #18): coordinate-range plus condition-tree filtering (fleet total and
  per-ship-type, AND/OR nesting) with cursor pagination and sorting, all server-side, plus
  persisted named filters.
- Local operations console (issue #18): a dark two-section console (mission centre, intel
  centre) with run detail and diagnostics as auxiliary pages. Status is never carried by
  colour alone. （原文还有一句 "`dry_run` is displayed as a lock with no toggle"；那个锁
  随 #35 一起消失，任务中心本身也被 #33 的常驻调度器取代。）

- **报告读取分阶段计时与统一日志**（#19）：`read_report` 按 `header` / `versus` /
  `fleet` / `rounds` 计时并挂在返回值上——只给总时长只能说明「慢」，分阶段才指得出该
  优化哪一次 OCR 调用；**失败时也记已耗时**，一次跑了三十秒才失败的读取正是这条日志要抓
  的东西。`evo_helper` 的日志落到 `var/logs/report-read.log`（2MB 轮转、保留 5 份）。
  实测单份 2.81s、舰队列占六成，后面那次提速就是照着这个数去的。
- **扫描任务的航线配置**（#19）：`scan_plans` 新增 `fleet_line_limit` 与 `reserved_lines`
  （迁移 `c3f81a97b2d4`），保留的是「始终留给用户自己派遣、助手永不占用」的那几条。
  可用上限 = 上限 − 保留，且保留还必须扛得住游戏自身的空位反馈（游戏报 3 个空位而保留 2
  条时只允许占用 1 条）。**保留数必须小于上限**，否则任务永远派不出去，前后端各拦一道。
- **派遣简报解析，并在派攻击前校验任务类型**（#20）：简报页直接给出**绝对到达时间**，
  比「当前时间 + 飞行时长」可靠得多——不依赖本机时钟与游戏时钟同步，也不会因为「读完到
  点出发」之间的耗时而漂移；飞行时长降为交叉校验，差一分钟以上就说明至少有一处读错。
  新增的安全不变量是**派攻击前简报上必须写着「攻击」**：类型选错会把舰队派成探索或运输，
  拿不到战报还白烧一趟燃料。
- **派出舰队之后松手**（#20）：用户会在助手派出舰队后切换登录去玩，所以「登录中断」不是
  故障，而是**正常流程的一部分**。新增 `AWAITING_REPORT`（等战报，不持有会话）与
  `WAITING_SESSION` 两个**活动**状态——舰队在飞的几个小时里仍可暂停与紧急停止。会话退避
  30 秒起、封顶 8 分钟：**助手不和用户抢登录**（两个会话互相顶号会陷入死循环），而封顶
  刻意压得不高，太长会让战报在助手醒来前过期。整个等待完全靠库恢复（迁移
  `d5a37c1e08b9`），集成测试显式关掉引擎再新开来证明这一点。
- **第一次全代码驱动的实地坐标采集**（#21）：2:121–2:125 共 75 个坐标，接受 71、拒绝 4。
  拒绝里有 2 条是**面板延迟**——请求 2:123:5 时面板还显示 2:123:4；没有「请求坐标 vs 面板
  读回坐标」这道交叉校验，这两条会把数据记到错误的坐标上。情报中心**不过滤 bot**：一次
  扫描的价值有一半在于「这些坐标里没有 bot」，只列 bot 会让空扫描看起来像什么都没发生。
  顺带更正了一条环境前提：实机系统缩放是 **125%** 而不是文档假设的 100%，未声明 DPI 感知
  时窗口尺寸拿到的是逻辑像素，怎么调都对不上标定视口。
- **游戏窗口生命周期与会话巡检**（#22、#24）：窗口不在就用 `--app` 拉起、还原最大化、调回
  标定视口，视口对不上就抛错——几何不对时继续截图只会喂给解析器错位的 ROI。会话每 10
  分钟巡检一次，掉线走已知入口序列接回去，**认不出的画面绝不点击**（可能是维护公告或弹窗，
  乱点可能误触派遣、删信或领奖），但过渡态读出来的花字只说明「此刻读不清」，不等于「这一
  屏认不出」，所以轮询期间容忍 UNKNOWN、**只在真的观察到 START 时才点**。
- **扫描范围、优先级与坐标扫描器**（#23、#25）：跳过 1–4 号位（用户确认恒为海盗），每系
  实际可扫 5–20 共 16 位；顺序上 2:001–200 排最前，而**用户清单里没写的银河系一律补在
  末尾**——「优先」只能改变顺序，不能悄悄变成「只扫」，否则 9 系永远不被扫到，而界面上
  看不出任何异常。扫描器搬进 `tools/scan_coordinates.py`，续扫**按计划顺序而不是字典序**
  （计划里 `2:201–499` 排在 `1:001–499` 之前，拿字典序比大小会把整个 1 系判成已扫过）；
  游标只在坐标读完并落库之后才前进，中断最多重扫一个坐标。「每系恰好一个 bot」已由用户
  确认、实测 111/111 相符，据此提前收工（全宇宙约 144 小时 → 76 小时），而「本系已扫完」
  的判据**只有一份**（`systems_with_bot()`）：各写一份的话，补缺口会把主循环故意跳过的位
  当成缺口，每跑一次重扫一遍，永远补不完。
- **扫描的桌面悬浮窗**（#26）：扫描期间游戏窗口一直占着前台、控制台被压在后面，「它还在
  不在跑、跑了多久」原本只能靠翻日志猜。持续工作时间**把退避等待算在内**——那时它仍然
  在岗，排除掉会让一段被频繁打断的扫描看起来只干了几分钟活。状态窗**不抢焦点**（抢了焦点，
  扫描下一次点击就会打到它身上）；右键**只停不启**——停是安全动作，必须在任何状态下都说得
  准，做成「切换」在实机上撞见过「本想停、结果又起一轮」。
- **海盗侦查-攻击循环与 bot 的探路-分档-攻击**（#28、#29）：三档模式刻意分开，默认一个
  动作都不做，`--scout` 只派探测器，`--attack` 才把战斗舰队送出去。**预设只按标题匹配**
  （用户口径）：找到那个标题才点，找不到就整发放弃，**不读、不校验预设内容**——内容是用户
  自己在游戏里维护的，助手去核对既多余，也会把「用户改了预设」误判成故障。预设条**只往
  左拖**，因为最右端是「+ 保存当前舰队」，点到它会覆盖用户的预设。**意图在点「出发！」之前
  写、派遣在之后写**：被闸门拦下的那些恰恰最该出现在日志里，而它们没有派遣行。
- **侦察报告给出三值结论**（#28）：`ATTACK` / `SKIP` / `UNREADABLE` 且不对称——任一格的数量
  大于 1 就打（缺的格子只会让对方更强，不会让结论反过来）；读到的都不大于 1 但还有格子没读
  出来时**不下结论**（缺的那格可能正是一支舰队）；四格都读出来且都不大于 1 才叫不值得打。
  合成一个布尔就分不出后两种，而「把没看清当成这里是空的」是这条链路唯一会把舰队送错地方
  的方式。
- **海盗战报只记胜负与战损总数**（#28）：口径由用户定，理由是性能（海盗全是同一个预设打
  的，明细没有分析价值）。**不写 `fleet_snapshots`**——明细一旦混进去，情报中心会把我方
  预设的舰船当成对方的舰队，比缺数据坏得多。三个新字段都可空**而且必须可空**：给
  `outcome` 补值等于凭空造战果，给战损补 0 等于凭空造「零损失」。
- **常驻任务调度器**（#31、#33）：三条链路拖拽定优先级、一个开始/结束、扫描恒在最后填
  空隙。这条模型成立的前提是扫描**不派遣舰队**、因此不占航线。`MissionSupervisor` 同时只
  管一个子进程，并**刻意去掉自动续跑**——那是扫描链路的特性（不派遣、断在哪都能接着扫），
  攻击类任务自己重启会连着再派一轮舰队，一天 32 次配额可以在没人看着的时候悄悄打光。配额
  日界做成具名纯函数 `quota_day_start_utc()`，因为调用方一旦自己写 `replace(hour=0)`，
  那个 `replace` 落在本地时刻上就悄悄变成本地日历天：本地 0–8 点会**漏数**当日派遣（以为
  还有额度 → 舰队被强制返回），之后又会**多数**昨天尾巴上的。开机对齐三行任务与单行配置
  **只补不改**——第二遍要是覆盖，用户拖出来的优先级每次重启都被抹掉；只有扫描默认开着，
  两条攻击链路默认关着。
- **任务配置固化**（#39）：「开始」那一刻把三条链路的配置固化成一条 JSONL 记录，运行中
  一律拒绝修改（409，正文说清「点结束后可改」），页面同步置灰并把 `draggable` 真的改成
  `false`。正因是 `_step()` 每个 tick 都重读 `mission_tasks`，运行中改一个参数会**立刻**
  进入下一轮，而上一轮正拿着旧参数在飞；事后翻账还分不出来——海盗半径从 5 改到 12 之后，
  那一轮到底按哪个半径跑的，全仓没有任何一处记着。`params_json` 原样存不解析（解析一遍
  再存，存下来的就成了「我们以为的配置」）；按 `MissionKind` 声明顺序排而**不按 priority**
  （跟着 priority 排的话，用户拖动之后整张表错位，看起来像三条链路全改了）。「恢复」开一个
  窄口子：一条链路完全可能在调度器跑着的时候被自动停用，而那正是用户最需要恢复它的时刻。
- **情报中心的快速过滤与攻击日志的坐标范围**（#40）：三个快速过滤按**最近一次派遣**判，
  并把这句话写在页面上——「打过就算」会让一个赢过也输过的目标同时落进「胜」和「负」，两个
  筛选谁都答不上「它现在什么情况」，而这一页存在的理由是决定**下一发打谁**。「拦下」与
  「被拒」分成两档（合成一档的话，「为什么这个目标没打」在页面上就没有答案）。坐标区间比
  的是**打包坐标而不是逐分量**：`2:130:15` – `2:140:3` 这种区间里起点位号比终点大是常态，
  逐分量比较会拿 `position BETWEEN 15 AND 3` 去卡，整段中间星系一条都留不下。筛选一律
  下推 SQL——先按 limit 砍掉历史再筛，查旧账必得空页，而空页读起来和「那个坐标一发没打」
  一模一样。坐标写错**不返回 422**：那是一张 HTML 页，一页 JSON 报错读起来就是「控制台
  坏了」；改为照常渲染并在顶上挂红字说明「这一页没有按坐标筛」——默默不筛才是最坏的一种。
- **分档阈值可配，外加一个「分档阈值」菜单页**（#47）：bot 分档的三道边界从写死在
  `domain/fleet_tier.py` 里的 `TIER_BOUNDARIES` 挪进 `scheduler_config`（扩已有的单行配置表，
  不新开一张：它和航线数、日配额一样是全局的、只有一份，`restart_cooldown_seconds` 是先例）。
  ⚠️ **默认值同时把中间那道从 5000 改成 4000**——用户口径（2026-08-11）是 2K 以下不打、
  2–4K 打 AAA、4–8K 打 BBB、8K+ 打 CCC。**档位数量与预设名 AAA/BBB/CCC 不可配**：那三个是
  游戏内预设标题，派遣链路按标题 OCR 去找，对不上就整发放弃（实机见过 `预设条上找不到
  'CCC'`，PR #100 已修）。`domain/fleet_tier.py` 仍是纯函数——`tier_for(total, thresholds)`
  的阈值**必须传，没有默认值**，给它一个默认就等于让忘了传的调用方静默按另一套数分档；
  查库是仓储的事（`SqlAlchemyRepository.tier_thresholds`）。三个数必须**严格递增**，不递增
  一律 400 拒绝、**不排序也不截断**：把 BBB 起点设到 CCC 之上，BBB 就成了永远取不到的死区，
  而页面上三个框都填着数、看不出问题；静默排序则会显示成「保存成功」而实际生效的是另外三个数。
  运行中一律 409 锁死，和任务参数同一条口径（#39）——阈值决定每一发派哪套预设，一轮之内换
  一次口径，事后从台账里分不出当时用的是哪一套；这里没有「恢复」那种例外，阈值页上没有任何
  操作是在把调度器自己弄出来的状态改回去。阈值同时进**配置固化记录**（`MissionConfigFreeze`
  多一个可选字段）：它属于「这一轮用的是哪一套参数」，只留在 `mission_runs.command` 里回答不了
  「点开始那一刻页面上填的是什么」；已有的历史行没有这个字段，一律读成「未记录」而**不回填
  默认值**——那几轮实际用的是当时写死的 2K/5K/8K，编一个今天的默认值会把记录变成一份看起来
  完整的假账。**历史数据一行不改，也没有重算迁移**：查实过库里从来没有一列存过档位结论
  （存的是 `battle_reports.defender_units` 这个读数与 `attack_dispatches.preset_name` 这个实际
  用掉的预设标题），分档是每次派遣**现算**的，所以改阈值只影响改完之后发出的攻击；要读「当时
  是哪一档」看 `preset_name`，别拿今天的阈值去重算旧的 `defender_units`。阈值经
  `--tier-thresholds` 写进 runner 的 argv 而不是让 runner 自己查库：那条命令行原样存进
  `mission_runs.command`，带上这三个数之后它才同时回答得了「打了谁」和「按哪三个数分的档」，
  而且已经起来的子进程用的仍然是启动那一刻的取值。`FleetTier` 的值不再含数字（原先是
  `"2K–5K"`，边界一可配就成了一句过期的话），区间文字改由 `TierThresholds.label()` 现算。

### Changed

- Capture manifests conform to the dataset integrity contract and require explicit baseline
  eligibility; the runtime parser rejects legacy mail-list captures, and browser capture,
  reconnect, and per-screen UI-version gates are documented (issue #6).
- Plan ranges require the expected fleet-preset signature, and persisted plans carry a stable
  public UUID and an auditable update timestamp (issue #17).
- Report parsers rebuilt against the 2026-08-07 live layout; the unit catalogue now matches the
  in-game list (18 ships, 11 defences, in game order), correcting earlier guesses
  (`运输舰` to `小型运输船`, `间谍探测器` to `探测器`) and adding `收割者`, `湮灭之星`, missiles,
  and shields (issues #10, #18).
- Legacy pages redirect into the console: `/` and `/plans` to the mission centre, `/targets` to
  the intel centre (issue #18).

- **界面文案改用中文**（#19）：运行状态不再直接显示英文常量（`ARMED` → 待命、`DRAINING`
  → 收取战报……），未知状态**回落到原值**——宁可显示英文，也不要显示空白。代码标识符、
  接口字段与数据库取值保持不变：它们不是给人看的，改名会破坏接口且不减少任何歧义。
  （同批引入的「演习模式」文案已随 #35 一并删除。）
- **报告 OCR 提速 31%，输出逐字节不变**（#19）：先用计时日志定位，再用基准测试找瓶颈，
  结论是**瓶颈不是图像大小，是每次调用的固定开销**——一张 8×8 的空白图走 `chi_sim+eng`
  就要 0.654s，光加载中文模型 0.43s，而一份报告约 8 次调用。于是数量遍改用 `eng`（这一遍
  只取行尾数字，中文模型毫无用处），`OCR_UPSCALE` 由 4 降为 2；交错 A/B 各 4 次取中位数，
  5.01s → 3.46s，防守方 15 行舰种与数量 4/4 完全一致。**没有加数字白名单**——那会让
  Tesseract 失去用于切行的字形，15 行塌成 1 行。`OCR_UPSCALE` 的注释里记着实测表：4 与 2
  全对、**3 全错**；Tesseract 对缩放不单调，这个值不能靠插值调，只能实测。
- **桌面悬浮窗降级为调度器的瘦客户端**（#32）：它原本是全仓第二个真正起 runner 的地方，
  详见「Removed」。连不上时显示「未连接」并**什么都不做**——不自己拉起 Web 服务，更不退回
  「自己跑一轮扫描」的旧行为，因为调度器可能其实正在跑、只是一时接不上，那时自己再起一个
  进程正是要防的双主人。**一次问不到不算断线**（抢占那一下 `terminate()` + `wait(5)` 有几秒
  问不出状态，而它其实正在好好地派舰队，立刻翻脸说「未连接」会让用户去重启一台没坏的服务）；
  403 与「服务没起」分开提示，否则用户只会去重启一台活得好好的服务，而真正该做的是对一下
  令牌。
- **控制台默认绑 `0.0.0.0:8770`**（#27），局域网内的手机/平板可以直接打开。`Settings` 里
  那条「只许监听 127.0.0.1」的硬校验**删掉了**——它原本是安全不变量，现在被「局域网可访问」
  这个明确需求取代；取代它的是 `lan_exposed` + 启动横幅上的警告，也就是把「已经暴露出去了」
  这件事**说出来**，而不是让它不可能发生。局域网地址用连 UDP 的办法向路由表要，而不是
  `gethostbyname(gethostname())`：后者在装了 VPN / WSL / 虚拟机桥接的机器上经常给出一个连
  不通的地址，而这个地址是打印给用户拿手机去输的，给错还不如不给。横幅里也不能有 `⚠`——它
  不在 GBK 里，而这行是打到 cp936 控制台上的，`print` 会抛 `UnicodeEncodeError`，启动横幅
  把服务本身弄崩（实机撞到过）。
- **情报中心的列表口径**（#25、#40）：扫描列表把「只显示了一截」说出来——库里 2115 条扫描
  只渲染前 500 条（止于 2:32:7），计数还写着「31 / 500 条」，读起来就是**扫描死在 2:32 了**，
  而它实际跑到了 2:138；数据一条没丢，是展示在骗人，而静默截断正是本项目一直在防的那类错误。
  默认排序改看「情报时间」（战报时间与侦察报告时间取晚的那个）：海盗一份战报都没有，只按
  战报时间排会把「刚侦察完的海盗」整批沉到几百行开外，而那恰恰是最该顶在前面的一批。
  取数同时从 N+1 改成五条成批查询（全宇宙 4000 多个 bot 原先要 8000 多次往返）。
- **bot 攻击模式：不再攻击侦查，直接用预设 BBB 打，平局就对同一坐标再打**（#48，用户口径
  2026-08-13）。改动的**依据**是 8/12 通宵那一夜在生产库副本上按类别数出来的账：UTC 8/12
  共派 80 发，其中侦察 44 发（本来就不产生攻击战报）、真正该有战报的攻击发 36 发，而认领上
  的战报只有 15 份；bot 这一侧 21 发（探路 18 + AAA 3）只回来 6 份，且全是头一批——从 UTC
  15:04 起 bot 再没有一发的战报被读回来，链路从 15:51 到 23:12 一发未派、目标全卡在等战报。
  按类别拆开之后缺的那一类是明确的：**不是「还在飞」，也不是被游戏拒绝**（那一夜
  `accepted=False` 0 条），而是**翻信箱的开封预算被别的链路的报告吃光**——11 趟开工里 8 趟
  撞上 `MAIL_MAX_OPENS`（8 封），开出来的封数里 37 次「VS 块读不出来」、16 次「not an attack
  report: 海盗攻击报告」，而同样那几封海盗报告每一趟都被重开一遍（「这一封不是我的」只活在
  一趟之内）。这条改动**不动那个共用预算**，它换的是分母：每个目标要等的战报从两份（探路
  一发 + 分档一发）减到一份，同样预算下能闭合的目标数直接翻倍。`BotPhase` 从五态减到三态，
  `NEEDS_PROBE` / `AWAITING_PROBE_REPORT` **删掉而不是留成死态**——留着就是 `phase_of` 里两条
  永远走不到的分支。平局重打**有硬上限**：`MAX_ATTACKS_PER_TARGET = 3`（初打一发 + 最多补
  两发），周期是「一轮」，计数直接由 `bot_dispatch_facts(since=本轮起点)` 的行数给出，
  点「新一轮」就归零，**不新增任何一列**；取 3 是与仓里另外两条自愈配额同一档（断线重开
  3 次/滚动 1 小时、认不出目标只自愈一次）。「读不到战报算不算一次」由既有的
  `MAX_REPORT_AGE` 回答，不新增第二套计时：6 小时之内算（那一发还在事实表上，目标停在等
  战报，走不到重打），超过就整条剔掉、配额退回去——合起来的上界是「每个目标每 6 小时最多
  因此多打一发」。**算不出战果 ≠ 平局**：四个数缺一个时 `outcome` 为空，那种目标不重打，
  重打的唯一依据是确认平局；拿一次 OCR 失手去再送一支舰队出去是反的。判据只看**最后一发**
  的战果（按 any 判会让先平后胜的目标一直打到撞上限），所以仓储按 `dispatched_at_utc` 排序
  交出，次序是判据的一部分。海盗链路一个字没改——它走 `domain.scout_verdict`，不看战果。

### Fixed

- Game times are read as UTC+0, the zone the game renders in. A bare timestamp was previously
  parsed in the UTC+8 schedule zone, shifting every report by eight hours and breaking the
  strict origin/target/time match against a dispatch (issue #10).
- Ship names are snapped to the known unit vocabulary. Two OCR passes are combined — names from
  `chi_sim`, counts from `chi_sim+eng` — because neither reads both correctly. A garbled name
  made every report look like a first sighting, since the name is the fleet-timeline diff key
  (issue #18).
- The scan-range origin is no longer required to lie inside the range. It is the player's own
  planet and normally sits outside it, so real coordinates were rejected. The rule existed in
  both the fake and persistent services (issue #18).
- Capacity retries are auditable, successful report draining reaches `COMPLETED`, and a failed
  report-page navigation pauses the run; workflow outcomes synchronise the persisted run
  aggregate (issue #14).

- **迁移会把应用已配置的日志整个关掉**（#19）：`alembic/env.py` 调 `fileConfig(...)` 用的是
  默认的 `disable_existing_loggers=True`，而 Web 运行时**在启动时执行迁移**，于是
  `evo_helper` 下的日志在整个进程剩余生命周期里静音。这个缺陷是加计时日志时被测试串扰暴露
  的（单跑通过、全量跑失败），但它在生产路径上同样存在。
- **调度器仓储查询把防卡死机制原样反转成卡死机制**（#30）：`pending_reports_for_kind` 原
  查询返回该 kind 有史以来每一条真实派遣、没有时间下界，而库里现存派遣的
  `expected_report_at_utc` **全是 NULL**（飞行时间当时从来没人读过），`ReportWaitPlanner`
  见到任何一条 NULL 就无条件判「该去收」→ 海盗永远「有活干」→ 每个 tick 都去起一个 runner
  收一封永远不会到的战报 → 扫描永远抢不到空隙。改成**查询时现算**的两条规则，不依赖任何人
  先去写标记（写标记的调度器当时还不存在，先落地标记再依赖它，中间这段时间一条都排不掉）。
  同批还修掉：`bot_dispatch_facts` 漏了 `accepted` 过滤（被游戏拒掉的派遣会被当成「已派出且
  永远收不到战报」，目标永远停在 `AWAITING_ATTACK_REPORT`）；`mark_bot_target_skipped` 的
  `since` 由可选改必填（原先 `None` 是「不限时间范围」，会把该坐标历史上每一轮的意图全刷成
  跳过，而且是静默的）。
- **航线记账的两个错**（#34）：**航线不是在战报出来时释放的**——`count_inflight()` 一直用
  `expected_report_at_utc > now` 数在飞数，而那一列回答的是「战报出来没有」，航线要等舰队
  **飞回来**才空；后果是调度器在航线其实还占着时就去派，撞上游戏弹窗「同时派遣的舰队数量已
  达上限」，白跑一整轮。新增 `line_free_at_utc`，倍数按发次分岔（攻击 ×2、探路 ×1 单程——
  探路舰队会在攻击中损失、侦察 ×2）。另一半是**侦察发占航线却一条记录都没有**：海盗一轮最多
  4 发侦察，这 4 条航线对调度器完全隐形。补记录时必须避开的陷阱是配额——照 `PIRATE` 记进去
  就是**每发侦察吃掉一次攻击配额**，当天 32 次以 4 倍速度消失且完全静默，所以新增
  `mission_kind`：配额只数 `ATTACK`，在飞数**全都数**（侦察一样占航线），待收战报与 bot 三态
  只数 `ATTACK`（把不会产生战报的行喂进 `ReportWaitPlanner`，它会永远判「该去收」——与上面
  那条卡死是同一个形状）。
- **bot 链路从来没成功派出过一发，三个故障叠在一起**（#41）：实机跑了 13 分钟、44 个目标
  全军覆没，而三条失败信息互相掩护。① 攻击按钮用的是**敌对海盗**面板的坐标 (1032, 540)，
  bot 星球是**有主**面板、布局完全不同，那一点落在图标排和舰船格之间的空白处——点了等于没点，
  派遣面板压根没开，接着读预设条自然读到噪声，于是报「找不到预设」，**失败信息指向预设、
  真正的问题在按钮**。② 中文预设名被 tesseract 按字切开成 `['探', '路']`，而 `pick()` 是
  逐词 `name in text`，没有任何一个词包含「探路」；AAA 是拉丁字母不分词，所以海盗链路一直
  没事，只有中文名的预设选不中。③ `PresetNotFound` 关掉派遣面板后漏了 `invalidate()`，
  缓存仍以为银河系是 2 → 下一个目标的「设恒星系」落到银河系框上 → 游戏把 136 截断成最大值
  9 → 此后导航栏是 `[9:137:12]` 而缓存说 2:137，**银河系再也不会被重设**，44 个目标坐标核对
  全不过。坐标判据一个字都没动、也不许动：那一轮里核对**拦对了**一次真的走错星系（请求
  2:321:5、面板 2:320:5），放松成「位次对上就行」就是往错误的星球扔舰队。
- **两次「等战报」死锁，同一形状修了两遍**（#36、#38）：先是**探路发**——`phase_of` 要
  `has_report` 为真才放目标进 `NEEDS_ATTACK`，而全仓没有任何代码为 bot 探路写过
  `battle_reports`，唯一读战报的代码又只挂在 `NEEDS_ATTACK` 分支上，也就是**读战报的代码只
  在读过战报之后才会被执行**（实机跑一整夜：`AWAITING_PROBE_REPORT` 出现 152 次、
  `NEEDS_ATTACK` 出现 0 次，网页情报中心也因此整夜一行数据都没多）。后是**攻击发**——
  `_sweep()` 的收取名单只有 `AWAITING_PROBE_REPORT`，注释里那句「其余三态等调度器收」预设了
  一件不存在的事（调度器到点做的是把这条链路整个重新起一遍），于是同一页攻击日志上探路三发
  战果齐全、AAA 那一发停在「待战报」，直到 6 小时后被判缺失、目标退回去重打一遍——一条航线
  加一次配额，换来同样的结局。第二次的修法是把收取名单写成**具名集合**：漏一个态不报错、
  不留日志，只是那一档目标再也不被收取，这次就是这么漏的；两个等待态并进**同一趟信箱**，
  分两趟要把「关浮层 → 切地表 → 开信箱 → 翻四屏」付两遍（实机一趟 83 秒）。收报告这条路
  刻意只读详情页、`fleet_snapshots` 一行不写：逐舰种明细在回放页上，而打开那一屏要点「查看
  战斗回放」，那个按钮至今没有标定过的坐标——在一条真的驱动鼠标的链路上现编一个没核过的
  坐标，违反「认不出的画面绝不点击」。
- **战报收不回来的正因是信箱窗口太小，不是战报没到**（#37）：收件箱是两条链路共用的，6 行
  的窗口被海盗链路整夜的报告占满，而 6 次开封全花在**盲开**上（每封约 8 秒），真正要找的那几
  份还在第 7 行往下；点开之后又没有「这一屏铺开了」的判据（`_on_mail_detail` 早就写好，
  **从来没有人调用过**），没铺开的那一屏读出来和「这封是别人的报告」在下游长得一模一样。而
  这三件事一件都没进日志——唯一那句「还没出现在信箱最上面几行」对所有情况都成立，把「窗口
  不够大」说成了「报告还没到」，正因被这句措辞盖了一整天。改法是先在列表页读主题（一次截图
  + 六次窄 ROI OCR 约 1–2 秒，比开一封便宜整整一个量级），**筛错往「开」的一侧倒**；窗口
  6 行 → 24 行；按时间早停，下界取**派出时刻**而不是简报读到的预计时刻——后者是一次 OCR，
  实机同一天同距离六发读出 8 秒到 25 分钟不等，拿它当闸门一次读大就能把真报告永久挡在窗外
  （飞行时间是闹钟，不是闸门）。
- **开工对账，让当日配额不再只认库内计数**（#37、#43）：权威来源分两侧，证据互相独立——
  `attack_dispatches` 知道「助手自己派出去过什么」（刚派出、战报还没到的那几发只有它知道），
  信箱里的战报知道「确实打成了一发」（进程崩掉、换库、用户手动操作都不影响，但它滞后）。
  所以按 UTC 日**取大**：相加会把同一发数两遍，取小会回到会超额的那一侧。**绝不凭空造派遣
  记录**——多一条不存在的派遣，调度器就会以为一条航线被占着、等一份永远不来的战报。#43 又
  把「一天只对一次账」改成每次开工都跑：用户会暂停任务再重启，而一天一次意味着早上那次对账
  之后，库外发生的事当天再也不会被数进来，**而那正是对账存在的全部理由**；数数与开封分成两
  笔互不牵连的预算，反过来绑在一起，换过库的那天只会数到最前面八行，把「今天打了 20 发」记成
  8 发——而计数偏小正是会超额的那一侧。
- **战报认领漏了 `mission_kind == ATTACK` 过滤，四发 AAA 全卡在「待战报」**（#44）：海盗链路
  的常态就是「先侦察、判定值得打、再攻击」——同一个出发点、同一个目标、相隔几分钟，于是每份
  攻击战报都有两个候选（一发 SCOUT、一发 ATTACK），判 `AMBIGUOUS`、`dispatch_id` 留空、
  `has_report` 永远为假；当天四发无一例外。这道过滤在 `count_dispatches_since` 等五处早就写着，
  **只有认领这一侧漏了**。补上之后判据是结构性的（侦察发产生不了攻击战报），不是「时间就近」
  那种猜；真有两发攻击都对得上时**照旧记 `AMBIGUOUS`**——改的是「谁有资格当候选」，不是「多个
  候选时挑一个」。修好判据救不回已经在库里的行（`append_report` 只在写入那一刻认领一次，而
  按报告时间去重又保证它们永远不会被重新读一遍），所以补了 `rematch_report_at` 回头重认：生产
  库副本上 `MATCHED` 36 → 41，剩下 4 份早过 `MAX_REPORT_AGE` 的仍是 `UNMATCHED`——认不上才是
  真话，没有为了好看而认。
- **舰队读数的小数点在 OCR 那一层掉了**（#45）：`1.22K`（1220 艘，2K 以下、**本来就不该打**）
  被读成 `122K` = 122000，而**分档的三条边界 2K / 5K / 8K 全落在这两个读数之间，丢一个点就跨
  过全部三条**，于是判成 `8K+` 去挑最重的组合。不是后缀没处理（`parse_fleet_count("1.22K")`
  给出 1220），是那个几像素的小圆点没被读到。修在选票那一层：带点的候选吸收去点后与它相同的
  候选，方向单向，依据是「这个字体只会漏笔画，不会凭空多字」；**不放宽成子序列**——那条会把
  `11` 并进 `1.17K`。
- **「结束」按不动**（#42）：`stop()` 和**持着锁去查库**的 `tick()` / `snapshot()` 抢同一把
  `RLock`，而一次 `_facts()` 要按 bot 目标逐个问库（生产库那个范围 4237 个目标，实测 0.32
  秒），tick 每秒一次、页面每 2 秒一次、悬浮窗还有一次——这把锁基本没有空档，而 `RLock` 没有
  公平性，排在一群反复重取的线程后面可以饿任意久；FastAPI 的同步接口又跑在容量 40 的线程池
  里，轮询全卡在锁上之后那个 POST 连线程都分不到。页面上的样子正是用户描述的：秒表照走
  （浏览器本地算的），点「结束」毫无反应，也没有任何报错。锁缩到只护起停那几行，进锁之后再
  复查「用户点了结束吗」「在跑的还是决策时看到的那个吗」——这两句复查就是「任何时刻最多一个
  子进程」的守卫。
- **航线未知时的占用时长借错了量级**（#42）：`UNKNOWN_LINE_HOLD` 此前直接等于
  `MAX_REPORT_AGE` = 6 小时，而那 6 小时是「等一封战报等到什么时候死心」的天花板，不是对
  「一支舰队占多久航线」的估计（实测 236 条有航线钟的派遣，中位数 48 秒、最长 62.6 分钟）。
  于是一次读不到就被放大成一次停摆：实机六发攻击都没读到飞行时间，正好等于航线上限 6，
  `free_lines` 从此恒为 0；而**航线满了就不再派遣，也就再没有新证据能推翻这个估算**，唯一出口
  是熬到第一发满 6 小时。改成 90 分钟（实测最长往返留四成余量），**仍然是「过期」而不是
  「不计」**：估短了的代价有界且自纠（runner 看屏的 `LineCapacityGate` 复核），估长了的代价
  是上面那种没有出口的停摆。
- **扫描被误判成坏掉**（#42）：被抢占、抢不到前台都不该计入连续失败（后者定下退出码
  `EXIT_ENVIRONMENT_BUSY = 75` 作为进程间协议并只豁免这一个码——「所有退出码 1 都不算失败」
  会让真坏了也永远不停用）。真正让它今天就不再被误判的是**扫描崩掉之后也吃一次重启冷却**：
  它起来 14 秒就崩，而连续失败上限是 3，不冷却的话 43 秒就把整条链路停用，而另外两条有冷却的
  链路要撞满 10 分钟才落到同一个下场——最该一直有活干的那条，反而最容易被一阵前台争抢误判成
  坏掉。正常跑完、被抢占仍然不冷却，填空隙是它存在的全部理由。
- **导航缓存只放回读确认过的坐标**（#43）：`goto()` 打完字先把缓存清空，只有拿到证据的一方
  调 `confirm()` 才写进去，而那份证据就是行星面板坐标行的核对。按「我刚才打了什么」记，136 被
  截成 9 的那一刻缓存本身就是错的（#41）；按「面板回读到什么」记，错的记不进来。海盗链路的
  空位以前不核坐标，现在也回读一次——这一档补上了实机最贵的那种静默故障：缓存与导航栏分岔后，
  连续 44 个目标一路报「不是海盗」把整轮走完，**日志上与「今天真没海盗」一模一样**。顺带把
  同一恒星系里的字段输入从 12 次降到 6 次，但那是结果，不是目的。

### Removed

- **bot 分档整套删除**（#48，用户口径 2026-08-13「bot分档相关功能可以移除」）：不是把它留成
  没人读的死配置，是让 `domain/fleet_tier.py`、`/tiers` 页与侧栏入口、
  `GET|PATCH /api/tier-thresholds`、`TierThresholdsOut/Patch/View`、`TierBandView`、
  `bot_command` 与 `bot_loop` 的 `--tier-thresholds`、`repository.tier_thresholds` /
  `update_tier_thresholds` / `latest_defender_units` / `mark_bot_target_skipped`、
  `MissionConfigFreeze.tier_thresholds` 都不再存在。⚠️ **`parse_fleet_count` 不能跟着删**——
  它的消费者全在读战报那一侧（`vision.live_reports` / `vision.pirate_reports` /
  `vision.optional.report_screens`），是 `domain.battle_outcome` 那四个输入的解析器，与分档
  无关；搬到 `domain/fleet_counts.py`，模块名跟着用途走。迁移 `c1f70b8a26d4` 用
  `batch_alter_table` 删掉 `scheduler_config` 的 `tier_alpha_from` / `tier_beta_from` /
  `tier_gamma_from`（前一天 `a3d7b1e64c92` 刚加上）：**列在、代码不在是最难查的那种不一致**
  ——有值、有默认值，看起来像还生效的配置。已在生产库副本上验过
  `upgrade → downgrade → upgrade` 往返：20 张表逐表行数不变、配置行其余值不变、回滚后列集合
  与起点逐字一致、`integrity_check` ok、`foreign_key_check` 空。**旧的固化记录仍要读得出来**
  ——生产的 `var/mission-config-freezes.jsonl` 7 行里有 5 行写着 `tier_thresholds`，实测 7 行
  全部照常解析（`from_json` 逐个 `data.get(...)` 取字段，不认识的键一律无视；那份记录的用意
  就是事后知道当时用的哪套参数，为几个多余的键丢整行等于毁账）。
  `test_each_tier_maps_to_a_real_in_game_preset_title` **没有被连带删掉**：它守的「预设标题
  必须是游戏里真实存在的」仍然成立，改写成守 BBB，而且比原先更要紧——BBB 正是要往右拖才
  看得到的那一档（#100），标题一旦对不上，这条链路一发都派不出去。

- **演习模式 / `dry_run` 整个删除**（#35）：不是把默认值改成 False，是让这个字段、这个分支、
  这个开关、这一列都不再存在——派遣就是真派遣，没有第二条路径。迁移 `a2f6c8d31b70` 用
  `batch_alter_table` 删掉 `attack_dispatches.dry_run` 与 `scan_plans.dry_run`，已在生产库副本
  上跑过 `upgrade head`：18 张表行数逐张不变、`foreign_key_check` / `integrity_check` 干净、
  `downgrade -1` 再 `upgrade head` 往返正常。`EVO_HELPER_DRY_RUN` 从此没有任何作用
  （`extra="ignore"` 会静默忽略它，不会因此启动失败），`ActionGuard` 随之不再需要 `Settings`。
  **只删了「`dry_run=true` 一律拒绝派遣」这一条闸**：一次性短时令牌、点击前重新看屏、航线配额
  闸、预设签名校验、`FORBIDDEN_LABELS` 一条未动；仓储查询里 `accepted` 那一半过滤**全部保留**
  ——被游戏拒掉的派遣同样收不到战报，算进来会变成「已派出且永远收不到战报」的死记录。
- **`tools/scan_console.py` 里的 `ScanSupervisor` 与 `launch_scan()` 删除**（#32）：它是全仓
  **唯一**第二处真正起 runner 的地方。调度器一上线就会有两个互不知情的东西抢同一个鼠标——
  调度器以为只有自己在派舰队，而 Alt+F8 还能另开一轮扫描。「任何时刻最多一个子进程在点鼠标」
  （一个游戏窗口，一个鼠标）靠约定守不住，只能靠取消第二个启动器；所以这一条刻意排在给页面
  加调度器入口**之前**做。现在这个模块里 `Popen` / `subprocess` / `spawn` 零命中，并由一条测试
  盯着这件事——查的是**能力**而不是某条分支的行为，因为「连不上就自己跑一轮扫描」正是最容易被
  当成贴心降级重新写回来的那一种。
- **情报中心的四个舰种列**（#46，用户口径「节约性能，仅查看舰队总数」）：移除的理由是**数据源**
  而不是版面。bot 那半边根本没有这四个数（探路只读详情页、`fleet_snapshots` 一行不写，要补上得
  多点开一次「查看战斗回放」，而那个按钮至今没有标定过的坐标）；海盗那半边有，但「收割者」一列
  在实机 98 份报告里**一份都没读出来**，摆在列表上也是满屏的「—」。常量留成空元组而不是删掉，
  回放页哪天标定好就填得回去；**没有改成 `or 0`**——`None` 是「没读到」、`0` 是「真的没有」，
  整套 ATTACK / SKIP / UNREADABLE 判定就建立在这个区分上。

### Security

- ActionGuard re-verifies the required attack screen immediately before consuming a dispatch
  token, so a high-confidence but wrong screen cannot authorise the final click (issue #15).
- Unknown attack UI and exhausted capacity halt before any attack intent or dispatch record is
  created (issue #15).
- Cursor recovery is proven by a restart-level SQLite workflow test (issue #14).
  （原文前半句是 "Dry-run dispatch records cannot close battle reports"；#35 之后没有演习
  记录这种东西了。）

- **任何时刻最多一个子进程在点鼠标**（#31、#32、#33、#42）：一个游戏窗口、一把鼠标。
  `MissionSupervisor` 拒绝并发 `start()`；调度器用可重入锁，且缩锁之后靠进锁复查守住这条
  （#42）；第二个启动器已删（#32）。**权威航线闸门仍在 runner 的 `LineCapacityGate`**（它看
  屏），调度器的在飞数只是乐观估算，估高了最坏是 runner 空跑一轮就退，不会误派。
- **局域网可访问 = 读写均无身份认证**（#27）：同源校验挡的是跨站请求伪造，不是局域网里的人
  ——局域网设备打开控制台本身就是同源的。不加访问口令是用户明确确认过的选择，安全边界因此是
  「这个网段可信」，**公共 Wi-Fi 下不要开**。要退回本机独占：`EVO_HELPER_HOST=127.0.0.1`。
- **只读的那几条口径没有变**（#28、#36、#37、#38、#43、#44）：信箱里只切「报告」标签（白名单
  由 `tests/unit/tools/test_mailbox_clicks.py` 钉着），不删邮件、不领奖励、不取消/派遣他人任务；
  读不出来就不存、不存半份、缺数留空**不拿 0 顶替**；认不出的画面绝不点击；
  `pyautogui.FAILSAFE` 保持开启。
