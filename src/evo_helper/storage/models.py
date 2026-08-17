"""SQLAlchemy ORM models for the EVO-Helper persistence schema (plan 8.1)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from evo_helper.domain.records import MISSION_KIND_ATTACK

from .database import Base, UTCDateTime


class ScanPlan(Base):
    __tablename__ = "scan_plans"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    public_id: Mapped[UUID] = mapped_column(Uuid, unique=True, default=uuid4, index=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    time_window_start: Mapped[str] = mapped_column(String(5), default="08:00")
    time_window_end: Mapped[str] = mapped_column(String(5), default="20:00")
    timezone_name: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    #: Fleet lines this plan may occupy, and how many stay free for the user.
    fleet_line_limit: Mapped[int] = mapped_column(Integer, default=1)
    reserved_lines: Mapped[int] = mapped_column(Integer, default=0)
    created_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at_utc: Mapped[datetime] = mapped_column(
        UTCDateTime, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )


class ScanRangeRow(Base):
    __tablename__ = "scan_ranges"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    plan_id: Mapped[int] = mapped_column(ForeignKey("scan_plans.id"), index=True)
    start_galaxy: Mapped[int] = mapped_column(Integer)
    start_system: Mapped[int] = mapped_column(Integer)
    start_position: Mapped[int] = mapped_column(Integer)
    end_galaxy: Mapped[int] = mapped_column(Integer)
    end_system: Mapped[int] = mapped_column(Integer)
    end_position: Mapped[int] = mapped_column(Integer)
    origin_galaxy: Mapped[int] = mapped_column(Integer)
    origin_system: Mapped[int] = mapped_column(Integer)
    origin_position: Mapped[int] = mapped_column(Integer)
    fleet_preset_name: Mapped[str] = mapped_column(String(120))
    fleet_preset_signature: Mapped[str] = mapped_column(String(255))
    priority: Mapped[int] = mapped_column(Integer, default=0)


class RunInstance(Base):
    __tablename__ = "run_instances"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    plan_id: Mapped[int] = mapped_column(ForeignKey("scan_plans.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    target_date: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="DRAFT")
    cursor_galaxy: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cursor_system: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cursor_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pending_galaxy: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pending_system: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pending_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    drained_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: 松手等待期间该睡到什么时候。持久化是关键：派出后助手不持有会话，
    #: 进程可以整个退出，恢复时靠这个字段判断现在该等还是该收。
    resume_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: 连续拿不到登录的次数，用于退避。拿到会话后归零。
    session_attempts: Mapped[int] = mapped_column(Integer, default=0)
    finished_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)


class CoordinateScanRow(Base):
    __tablename__ = "coordinate_scans"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("run_instances.id"), index=True)
    galaxy: Mapped[int] = mapped_column(Integer)
    system: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer)
    scanned_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)
    owner_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    evidence_artifact_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)


class BotTargetRow(Base):
    __tablename__ = "bot_targets"
    __table_args__ = (
        UniqueConstraint("galaxy", "system", "position", name="uq_bot_targets_coordinate"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    galaxy: Mapped[int] = mapped_column(Integer)
    system: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer)
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    latest_owner_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_scanned_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_attack_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_dispatch_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_report_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: 坐标扫描已核验；排行榜只从名字反解，可能是合法但错误的 OCR 结果。
    source: Mapped[str] = mapped_column(String(16), default="scan", server_default="scan")
    military_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    military_score_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    military_score_estimated: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )
    #: 榜单名次。只为事后能拿「降序」这条免费校验和复核军力值（见 domain.records）。
    military_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)


class AttackIntentRow(Base):
    __tablename__ = "attack_intents"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "target_galaxy",
            "target_system",
            "target_position",
            "cycle_start_utc",
            "forced_revisit",
            name="uq_attack_intent_run_target_cycle",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("run_instances.id"), index=True)
    origin_galaxy: Mapped[int] = mapped_column(Integer)
    origin_system: Mapped[int] = mapped_column(Integer)
    origin_position: Mapped[int] = mapped_column(Integer)
    target_galaxy: Mapped[int] = mapped_column(Integer)
    target_system: Mapped[int] = mapped_column(Integer)
    target_position: Mapped[int] = mapped_column(Integer)
    preset_name: Mapped[str] = mapped_column(String(120))
    preset_signature: Mapped[str] = mapped_column(String(255))
    cycle_start_utc: Mapped[datetime] = mapped_column(UTCDateTime)
    guard_status: Mapped[str] = mapped_column(String(32), default="PENDING")
    forced_revisit: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)
    #: `bot` 或 `pirate`（见 `domain.records.TARGET_KIND_*`）。攻击日志按它分类。
    target_kind: Mapped[str] = mapped_column(String(16), default="bot", server_default="bot")


class AttackDispatchRow(Base):
    __tablename__ = "attack_dispatches"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    intent_id: Mapped[UUID] = mapped_column(ForeignKey("attack_intents.id"), unique=True)
    dispatched_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)
    accepted: Mapped[bool] = mapped_column(Boolean)
    evidence_artifact_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    #: 派出时读到的飞行时长，以及据此算出的预计战报时间。
    #: 助手派出后就松手，靠这个时间决定什么时候回来登录收报告。
    #: 读不到飞行时间时为 NULL——那时改为立即尝试收取，而不是无限等待。
    flight_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_report_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: **第二个钟**：这条航线什么时候空出来（出发 + 飞行时长 × 1 或 × 2，
    #: 见 `domain.report_wait.line_free_at`）。与上面那一列是两个不同的时刻——
    #: 战报在**抵达**时产生，航线要等舰队**飞回来**才释放。
    #: 拿战报那个钟去判航线，调度器会在航线其实还占着时就去派，撞上游戏的
    #: 「同时派遣的舰队数量已达上限。」。
    #: 飞行时长读不到时同样为 NULL，**NULL 照样占航线**，占到派出时刻 +
    #: `domain.report_wait.UNKNOWN_LINE_HOLD` 为止（早先的「NULL 不计入在飞数」
    #: 已被实机推翻，判据见 `storage.repository._still_holding_a_line`）。
    line_free_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: **人工放手的时刻**：用户在游戏里看过、确认这一发的舰队已经回港，于是在
    #: 调度台上把这条航线占用清掉。非 NULL 就是「不管上面那个钟怎么说，这条
    #: 航线现在是空的」。
    #:
    #: 为什么另起一列而不是去改 `line_free_at_utc`：那一列记的是**当时读到的
    #: 飞行时长推算出来的返航时刻**，是一条观测记录。把它改写成「现在」，
    #: 这一发究竟飞了多久就再也查不出来了，而飞行时长正是
    #: `domain.report_wait.vet_flight_time` 那道下限赖以校准的样本。
    #: 两列分开之后，「舰队几点回来」与「人几点说它回来了」各说各的话。
    line_released_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: 这一发是攻击还是侦察（见 `domain.records.MISSION_KIND_*`）。
    #: **日配额只数 `ATTACK`**：侦察也是打向海盗的，不分开数的话一轮 4 发侦察
    #: 就吃掉 4 次攻击额度。**在飞数两者都数**：侦察一样占航线。
    #: 存量行一律算 `ATTACK`——这一列加进来之前，侦察压根没有记录。
    mission_kind: Mapped[str] = mapped_column(
        String(16), default=MISSION_KIND_ATTACK, server_default=MISSION_KIND_ATTACK
    )


class BattleReportRow(Base):
    __tablename__ = "battle_reports"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    reported_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)
    raw_time_text: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attacker_origin_galaxy: Mapped[int] = mapped_column(Integer)
    attacker_origin_system: Mapped[int] = mapped_column(Integer)
    attacker_origin_position: Mapped[int] = mapped_column(Integer)
    defender_target_galaxy: Mapped[int] = mapped_column(Integer)
    defender_target_system: Mapped[int] = mapped_column(Integer)
    defender_target_position: Mapped[int] = mapped_column(Integer)
    match_status: Mapped[str] = mapped_column(String(16), default="UNMATCHED")
    match_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    manual_review_status: Mapped[str] = mapped_column(String(16), default="PENDING")
    is_from_revisit: Mapped[bool] = mapped_column(Boolean, default=False)
    ui_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 战斗详情页的「单位」总数，双方各一。**不是**逐行明细之和——
    #: 大舰队的数量显示成 `5.36K` 这样的四舍五入值，逐行相加凑不出精确总数。
    #: 可空：早先入库的战报没有这个数，补 0 会让它看起来像「舰队为空」。
    attacker_units: Mapped[int | None] = mapped_column(Integer, nullable=True)
    defender_units: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 详情页那行大字：`VICTORY` / `FAIL`（游戏画面原文，不翻译）。
    #: 可空：这个字段之前入库的战报没读过胜负，填个值等于凭空造战果。
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: 详情页的「损失单位」总数，双方各一。海盗战报只记胜负 + 这两个数，
    #: 不写 `fleet_snapshots`（用户口径 2026-08-09，为省性能）。
    attacker_losses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    defender_losses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dispatch_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("attack_dispatches.id"),
        unique=True,
        nullable=True,
    )


class BattleReportScreenshotRow(Base):
    """读一份战报时截下来的那一屏面板，**字节直接存库**。

    用户口径（2026-08-17）：进入邮件详情读战报时截一张图，能在攻击日志页看到。

    ## 为什么是字节，不是路径

    `artifacts` 那张表存的是路径，而这条链路**跑在另一台机器上**（runner 在
    `E:\\Kucleer_code\\EVO\\EVO-Helper`，人常在另一台机器上开控制台）。存路径
    等于在控制台上点开一个必然 404 的链接——图还在，只是没人看得见。
    库是两台机器唯一共享的东西，所以图就存在库里。

    ## 为什么是**自己一张表**，不是塞进 `system_log.payload_json`

    那张表按设计要保持轻：海盗一轮半小时、光 `say()` 就有 80 个调用点，两周
    几十万行，主视图是「按时刻倒序翻页」。往里面塞几十 KB 的二进制，翻页查询
    会连着 blob 一起扫，一张按设计只增不改的诊断表会被拖成负担。

    分表还带来一个真正要紧的性质：**攻击日志的列表查询绝不会碰到这些字节**。
    列表一页几十行、每行几十 KB，连着 blob 查一次就是几 MB 的响应。页面只按
    `EXISTS` 问「有没有图」，真正的字节由 `/api/reports/{id}/screenshot` 单取。

    ## 一份战报最多一张

    `report_id` 上是唯一约束。同一份战报被重复读到时（换库、重认）不该攒出
    好几张几乎一样的图；重复入库那条路径本来就走不到这里（`ReportIngest.KNOWN`
    直接返回）。
    """

    __tablename__ = "battle_report_screenshots"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    #: 这张图是哪一份战报的。唯一——一份战报最多一张图。
    report_id: Mapped[UUID] = mapped_column(
        ForeignKey("battle_reports.id"), unique=True, index=True
    )
    #: 截图那一刻（真实时间）。**保留期清理按它算**，不按战报时间：
    #: 补录会把很旧的战报读进来，按战报时间算的话那张图一入库就过期。
    captured_at_utc: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    #: 编码格式，目前恒为 `webp`。记下来是为了将来换编码时旧行仍能正确回放——
    #: 接口要靠它填 `Content-Type`，猜错就是浏览器直接下载而不是显示。
    image_format: Mapped[str] = mapped_column(String(8), default="webp")
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    #: 字节数。单独一列是为了能不取 blob 就统计占用——保留期这件事要能先量再调。
    byte_size: Mapped[int] = mapped_column(Integer)
    image_bytes: Mapped[bytes] = mapped_column(LargeBinary)


class BattleReportResourceRow(Base):
    """战报「获得资源」网格里**非零**的那几格。

    用户口径（2026-08-17）：只统计「获得资源」那 12 个值，残骸与两个百分比不做。

    ## ⚠️ 没有行 = 这一格是 0，**不是**「没读到」

    只存非零的格子——一份战报十有八九只有三五格有数，全存等于给每份战报凭空
    加十二行。所以读的时候是**全有或全无**：12 格但凡有一格读不出来，
    这份战报一行都不写（判据在 `domain.battle_resources.parse_resource_grid`）。
    不这样的话，「读到 8 格」会在库里长得和「另外 4 格是 0」一模一样。

    ⚠️ **残留的歧义说在前面**：一份战报**一行都没有**时，既可能是 12 格全 0
    （正常的一发白打），也可能是这条链路根本没读过资源（这个 PR 之前入库的
    存量战报全是这种）。库里分不开。当前不要紧——存量战报本来就没有收获数据，
    而全 0 与没读到在页面上都显示成「没有收获」。真要分开，加一个「读过没有」
    的标记列即可，别去猜。

    ## 为什么存 `slot` 不存资源名

    位置是观测到的事实，名字是解释。解释错了以后还能靠 slot 重新映射；
    把名字硬编进库里，原始观测就找不回来了。对照表在
    `domain.battle_resources.SLOT_LABELS`，页面渲染时才翻译。

    ## 为什么不在 `battle_reports` 上加 12 列

    那是把一张关系表当成电子表格用。加 12 列之后，「第 7 格是什么」只能靠列名
    回答，而列名一旦定错（见上一段）就只能靠迁移改；更要紧的是，格数是游戏的
    排版，不是这套系统的常量——它变一次，表结构就要动一次。
    """

    __tablename__ = "battle_report_resources"
    __table_args__ = (
        # 一份战报的一个格子只能有一行。重复读到同一份战报走的是「库里已有」
        # 那条早停路径，根本走不到这里；真撞上了，宁可写失败也不要攒出两份收获。
        UniqueConstraint("report_id", "slot", name="uq_battle_report_resources_slot"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    report_id: Mapped[UUID] = mapped_column(ForeignKey("battle_reports.id"), index=True)
    #: 网格位置 0..11，**行优先**（第一行左起 0/1/2/3）。不是资源名，理由见上。
    slot: Mapped[int] = mapped_column(Integer)
    #: 数量。`BigInteger`——画面上已经出现过 `3.7M`，而 `B` 后缀也在解析器里，
    #: 32 位在这条量级上只是等着某天溢出。
    amount: Mapped[int] = mapped_column(BigInteger)
    #: 画面上是缩写显示的（`928K`），真值取不回来了。页面上要标「约」。
    approximate: Mapped[bool] = mapped_column(Boolean, default=False)
    #: 最大绝对误差（半个末位刻度）。`928K` 是 500、`501.1K` 是 50、`3.7M` 是 50000。
    #: 单独记一列而不是按 `approximate` 现算：现算要知道当初显示了几位有效数字，
    #: 而那个信息在换算成整数的那一刻就没了。
    uncertainty: Mapped[int] = mapped_column(BigInteger, default=0)


class FleetSnapshotRow(Base):
    __tablename__ = "fleet_snapshots"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    report_id: Mapped[UUID] = mapped_column(ForeignKey("battle_reports.id"), index=True)
    side: Mapped[str] = mapped_column(String(16))
    ship_type: Mapped[str] = mapped_column(String(64))
    count: Mapped[int] = mapped_column(Integer)
    round_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 这一行的数没有把握。胜负只看两侧的「单位」与「损失单位」两个合计
    #: （`domain.battle_outcome`），个别舰种行不准不影响决策，
    #: 但必须让人看得出哪几行不能信。
    uncertain: Mapped[bool] = mapped_column(Boolean, default=False)


class ScoutReportRow(Base):
    """一份海盗侦察报告。**独立于 `battle_reports`，两者不可互相借用。**

    `battle_reports` 是攻击战报表：`dispatch_id` 认领一发派遣、`match_status`
    记认领结果、`outcome` / `attacker_units` / `*_losses` 全是打完之后才有的东西。
    侦察报告一样都没有，塞进去只会凭空多出一行「没认领上的战报」，让判态那一侧
    以为还有一发攻击在等回音。

    去重口径与 `repository.has_report_at` 一致：**目标 + 报告时间**。报告时间是
    游戏自己写在报告上的字，不受本地时钟与重跑影响；同一趟信箱被翻两次
    （活链路每一轮都会翻同样那几行）不会写出第二行。唯一约束是硬保证，
    `repository.append_scout_report` 里的预检只是让正常路径不必靠异常收场。
    """

    __tablename__ = "scout_reports"
    __table_args__ = (
        UniqueConstraint(
            "target_galaxy",
            "target_system",
            "target_position",
            "reported_at_utc",
            name="uq_scout_reports_target_time",
        ),
        Index("ix_scout_reports_reported_at_utc", "reported_at_utc"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    reported_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)
    #: 报告头上那串原文（`DD/MM/YYYY HH:MM:SS`），供事后核对时区换算。
    raw_time_text: Mapped[str] = mapped_column(String(64))
    origin_galaxy: Mapped[int] = mapped_column(Integer)
    origin_system: Mapped[int] = mapped_column(Integer)
    origin_position: Mapped[int] = mapped_column(Integer)
    target_galaxy: Mapped[int] = mapped_column(Integer)
    target_system: Mapped[int] = mapped_column(Integer)
    target_position: Mapped[int] = mapped_column(Integer)


class PlanetScoutAlertRow(Base):
    """Persisted security mail and its one-shot notification outcome."""

    __tablename__ = "planet_scout_alerts"
    __table_args__ = (
        UniqueConstraint("fingerprint", name="uq_planet_scout_alerts_fingerprint"),
        Index("ix_planet_scout_alerts_reported_at_utc", "reported_at_utc"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    #: sha256 of immutable game-mail evidence. It is the hard de-duplication key.
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    reported_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)
    raw_time_text: Mapped[str] = mapped_column(String(64))
    source_galaxy: Mapped[int] = mapped_column(Integer)
    source_system: Mapped[int] = mapped_column(Integer)
    source_position: Mapped[int] = mapped_column(Integer)
    target_galaxy: Mapped[int] = mapped_column(Integer)
    target_system: Mapped[int] = mapped_column(Integer)
    target_position: Mapped[int] = mapped_column(Integer)
    source_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    intercepted_probes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    subject: Mapped[str] = mapped_column(String(255))
    raw_body: Mapped[str] = mapped_column(Text)
    #: SENT / FAILED / NOT_CONFIGURED. New rows begin PENDING only briefly.
    delivery_status: Mapped[str] = mapped_column(String(32), default="PENDING")
    delivery_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivered_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class ScoutTriggerShipRow(Base):
    """侦察报告里某一个判定舰种那一格。

    ⚠️ **`count` 可空，而 `NULL` 的含义是「这一格没读出来」，不是 0。**
    这不是可有可无的洁癖：数量为 0 的格子在画面上只是一个孤零零的 `0`，
    实测最容易读空（见 `vision.scout_reports.PirateScoutReading.missing`）。
    把读空补成 0 存进来，就等于把「没看清」记成「这里是空的」，
    而三值判定（ATTACK / SKIP / UNREADABLE）整个建立在这个区分上——
    下一轮据此判「不值得打」，一支实打实的舰队就此被放过。

    ⚠️ **这不是舰队快照，别拿它当 `fleet_snapshots` 用。** 这里只有
    `PIRATE_TRIGGER_SHIPS` 那四个舰种，不是对方的全部舰队。
    """

    __tablename__ = "scout_trigger_ships"
    __table_args__ = (
        UniqueConstraint("report_id", "ship_type", name="uq_scout_trigger_report_ship"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    report_id: Mapped[UUID] = mapped_column(ForeignKey("scout_reports.id"), index=True)
    #: 读到的先后次序。`PirateScoutReading.missing` 是有序元组，读回来要一模一样。
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    ship_type: Mapped[str] = mapped_column(String(64))
    #: 读到的数量；**`NULL` = 没读出来**，与 0 是两回事。
    count: Mapped[int | None] = mapped_column(Integer, nullable=True)


class TargetRevisitRow(Base):
    __tablename__ = "target_revisits"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    #: 32 而不是 16：调度器要写的 `BOT_TIER_NEGLIGIBLE` / `BOT_REPORT_MISSING`
    #: 都超过 16 字。SQLite 不校验 VARCHAR 长度，所以超了也照存不误——正因为
    #: 现在不报错，声明与实际存的东西对不上这件事才必须在这里改掉。
    scope: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(String(255))
    target_galaxy: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_system: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requested_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)
    executed_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")


class UiObservationRow(Base):
    __tablename__ = "ui_observations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    screen: Mapped[str] = mapped_column(String(32))
    ui_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detection_result: Mapped[str | None] = mapped_column(String(255), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    evidence_artifact_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    observed_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)


class StateEventRow(Base):
    __tablename__ = "state_events"
    __table_args__ = (Index("ix_state_events_aggregate", "aggregate_type", "aggregate_id"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    aggregate_type: Mapped[str] = mapped_column(String(32))
    aggregate_id: Mapped[UUID] = mapped_column(Uuid)
    event: Mapped[str] = mapped_column(String(64))
    before_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    after_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    occurred_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)


class ArtifactRow(Base):
    __tablename__ = "artifacts"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    path: Mapped[str] = mapped_column(String(512), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    media_type: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(64))
    retention_policy: Mapped[str] = mapped_column(String(32), default="KEEP")
    created_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)


class IntelFilterRow(Base):
    """A named, reusable intel query. The tree is stored as JSON text."""

    __tablename__ = "intel_filters"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(120), index=True)
    condition_tree: Mapped[str] = mapped_column(Text)
    span_start: Mapped[str | None] = mapped_column(String(16), nullable=True)
    span_end: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)


class MilitaryRankingSnapshotRow(Base):
    """One completed read of the in-game military-score ranking."""

    __tablename__ = "military_ranking_snapshots"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    captured_at_utc: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    row_count: Mapped[int] = mapped_column(Integer, default=0)


class MilitaryRankingEntryRow(Base):
    """A ranking line retained with its snapshot so score changes stay auditable."""

    __tablename__ = "military_ranking_entries"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "ordinal", name="uq_military_ranking_snapshot_ordinal"),
        Index("ix_military_ranking_entries_rank", "rank"),
        Index("ix_military_ranking_entries_coordinate", "galaxy", "system", "position"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey("military_ranking_snapshots.id"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    player_name: Mapped[str] = mapped_column(String(128))
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    galaxy: Mapped[int | None] = mapped_column(Integer, nullable=True)
    system: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: **这一行是什么时候读到的**，行级而不是快照级。用户口径（2026-08-16）：
    #: 「军力榜我需要的是每条数据的更新时间」。
    #:
    #: 快照的 `captured_at_utc` 回答不了这个问题：一趟读榜要滚几十屏，逐屏之间
    #: 差得开，而快照只有一个时刻。写入口在为空时回落到快照时刻（见
    #: `military_rankings.append_snapshot`），所以这一列**永远非空**——
    #: 页面上不必再处理「这行没有时间」这种状态。
    #:
    #: ⚠️ **故意不建索引。** 现在没有任何查询按它筛或排（`latest()` 先锁定
    #: 快照、再按 `rank`/`ordinal` 排），而这张表是一次写入上千行的追加表，
    #: 白建的索引只是往每次入库上加成本。真要按时间查历史时再补。
    observed_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)


class MissionTaskRow(Base):
    """一个任务一行。优先级由用户在页面上拖出来。

    ⚠️ **`kind` 不再唯一。** 用户口径（2026-08-13）：「可能会新增多个同一个类型的
    任务，比如 2 个 bot 攻击，从主星出发 5 条航线，从 2 号线出发 2 条航线」。
    所以任务的身份是 `id`，不是 `kind`——接口、调度判据、`mission_runs` 的台账
    全部按 `id` 认人。海盗与扫描仍然各只有一行（用户确认只有 bot 需要多任务），
    但那是配置上的事实，不再是数据库约束。
    """

    __tablename__ = "mission_tasks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    #: `PIRATE` / `BOT` / `SCAN`。**不唯一**，见类文档。
    kind: Mapped[str] = mapped_column(String(16), index=True)
    #: 用户给这个任务起的名字。同类型的多个任务全靠它区分。
    #: 空串表示没起名，显示层回落到 `web.display.MISSION_LABELS[kind]`。
    name: Mapped[str] = mapped_column(String(60), default="", server_default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    #: 升序即优先级。
    priority: Mapped[int] = mapped_column(Integer, default=0)
    #: 各链路自己的参数。海盗 `{"radius": N}`，bot
    #: `{"galaxy": G, "first_system": A, "last_system": B}`，扫描 `{}`。
    #: 存 JSON 而不是逐列：以后加任务种类不用再动表结构。
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    #: 出发星球。**三列一起为 NULL 表示「用全局主星」**（`EVO_HELPER_ORIGIN`，
    #: 解析在 `config.Settings.origin_coordinate`）。
    #:
    #: 逐列存而不是塞进 `params_json`：它不是「这一轮打谁」那种参数，而是账本的
    #: 一部分——`attack_intents.origin_*` 照它写，战报认领与航线记账都按它分组，
    #: 那些都要能在 SQL 里 join。
    #:
    #: 留成可空而不是给每行都填死一个坐标：海盗与扫描从来没有过「自己的出发
    #: 星球」这个概念，给它们钉死一个值等于把换账号（改 `EVO_HELPER_ORIGIN`）
    #: 这条路悄悄堵掉——舰队会继续从上一个账号的星球算飞行时间。
    origin_galaxy: Mapped[int | None] = mapped_column(Integer, nullable=True)
    origin_system: Mapped[int | None] = mapped_column(Integer, nullable=True)
    origin_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 这个任务允许在它那颗出发星球上占用几条航线。
    #: NULL 表示「用 `scheduler_config.fleet_line_limit`」。
    #:
    #: **上限是按星球各一份的**（用户口径 2026-08-13），所以它必须挂在任务上而不是
    #: 只有一个全局值：主星 5 条 + 2 号星 2 条是两颗星各占各的，不是一共 7 条。
    fleet_lines: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 定时开启 / 定时关闭的时刻。**绝对时刻，一次性**，不是每天循环、不按星期几。
    #: 两列都可空，都为空表示不限——那时的行为与没有这项功能时完全一致。
    #:
    #: ⚠️ **它们与 `enabled` 取交集，定时器绝不回写 `enabled`**（用户口径
    #: 2026-08-17）。`enabled` 是用户的意志，被定时器改掉的话，用户手动开的会被
    #: 悄悄关掉，而且事后分不清是谁关的。判据见
    #: `domain.scheduler.within_schedule_window`。
    enabled_from_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    enabled_until_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: 仅 bot 用：本轮从何时算起。早于这个时刻的战报属于上一轮。
    round_started_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: 仅海盗用：收到游戏超限邮件时写下的封锁截止时刻。比计数更硬的信号。
    quota_exhausted_until_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: 连续异常退出次数。到阈值就自动停用，免得调度循环在一个坏掉的任务上空转。
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    disabled_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: 这次停用**靠什么被放回来**，取值见 `domain.scheduler.DisabledRecovery`。
    #:
    #: 上面那一列是给人看的一句中文，这一列是给判据看的。分成两列而不是让判据
    #: 去比对文案：措辞改一次判据就静默失效，而失效的样子是「任务停用之后再也
    #: 没人放它出来」——2026-08-17 生产库里那条配了 9 条航线、只占 2 条、却一直
    #: 挂着「空闲航线不足」的 bot 任务就是这么来的。
    #:
    #: **NULL 一律当 `MANUAL` 读**：没停用的行是 NULL，本列上线之前的历史行也是
    #: NULL。认不出来就要用户动手，这是唯一安全的默认。
    disabled_recovery: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)


class MissionTaskOriginRow(Base):
    """军力攻击任务的多颗出发星球。

    单 origin 列不能搬走：区域攻击已经在用它们。新表只给军力攻击加并行来源，
    为空时调度器才回落到旧列，保证已有任务的含义不变。
    """

    __tablename__ = "mission_task_origins"
    __table_args__ = (
        UniqueConstraint("task_id", "galaxy", "system", "position", name="uq_task_origin"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("mission_tasks.id", ondelete="CASCADE"), index=True
    )
    #: 全局星球配置的引用。旧记录迁移后会回填；坐标列保留为历史快照，避免
    #: 升级中断时既有任务失去出发点。
    planet_id: Mapped[int | None] = mapped_column(
        ForeignKey("attack_planets.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    galaxy: Mapped[int] = mapped_column(Integer)
    system: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer)
    fleet_lines: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class AttackPlanetRow(Base):
    """可供攻击任务选择的出发星球。编号按 ``sort_index`` 从 1 连续显示。"""

    __tablename__ = "attack_planets"
    __table_args__ = (
        UniqueConstraint("galaxy", "system", "position", name="uq_attack_planet_coordinate"),
        UniqueConstraint("sort_index", name="uq_attack_planet_sort_index"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    sort_index: Mapped[int] = mapped_column(Integer)
    galaxy: Mapped[int] = mapped_column(Integer)
    system: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer)


class MilitaryAttackConfigRow(Base):
    """军力攻击的全局档位方案；所有军力任务共享这一份。"""

    __tablename__ = "military_attack_config"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    tiers_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    #: 军力榜采集开榜后先「盲拖」几屏（`game.ranking_ui.BLIND_SCROLLS`）。
    #:
    #: **可空，空 = 用代码里的默认值 40**，与加这一列之前的行为完全一致。
    #: 不给它写 `default=40`：那样「没配」和「配了 40」就分不开了，日后调默认值
    #: 时所有老行都会被钉死在 40 上，而它们表达的其实是「跟着默认走」。
    #:
    #: 放在这张全局表而不是 `mission_tasks.params_json`：用户口径（2026-08-17）
    #: 是「盲拖数量需在攻击配置页可配置」，而这一页存的就是全局的那几项。
    blind_scrolls: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    #: 对账那一趟翻信箱最多往回读几个**小时**
    #: （`tools.pirate_loop.PirateLoop.backfill_reports` 的 routine 那一档）。
    #:
    #: **可空，空 = 用代码里的默认值 `domain.report_wait.MAX_REPORT_AGE`（6 小时）**，
    #: 理由同上面那一列：不写 `server_default`，「没配」与「配了 6」才分得开。
    #:
    #: 放在这张全局表而不是任务参数：用户口径（2026-08-17）是「这个参数改为可配置，
    #: 这样遇到活动我可以灵活调整」，而对账那一趟是两条链路（海盗 / bot）共用的，
    #: 挂在某一个任务上等于另一条链路配不着。
    report_scan_hours: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)


class MissionRunRow(Base):
    """调度器每起一个子进程记一行。"""

    __tablename__ = "mission_runs"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    #: 是**哪一个任务**起的这一轮。同类型可以有多个任务，光看 `kind` 分不出来，
    #: 而重启冷却正是按任务算的（`domain.scheduler.cooling_down`）。
    #:
    #: 可空：这一列加进来之前的历史行没有它。那些行不参与冷却判据——最坏的代价
    #: 是升级后的头五分钟里某个任务少等一次冷却，比给它硬猜一个任务号好。
    #: 不加外键约束：任务删掉之后这一行仍然要留着，它是账。
    task_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    #: 实际拉起的命令行。事后翻账时「那一轮到底打了谁」全靠它。
    command: Mapped[str] = mapped_column(Text)
    #: 用来在控制台重启后认出可能还活着的孤儿进程。
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)
    ended_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: `USER` / `SELF` / `PREEMPTED` / `SHUTDOWN` / `UNKNOWN`
    stopped_by: Mapped[str | None] = mapped_column(String(16), nullable=True)
    log_path: Mapped[str] = mapped_column(String(255))


class SchedulerConfigRow(Base):
    """单行配置。航线是全局资源，不属于任何单个任务。"""

    __tablename__ = "scheduler_config"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    fleet_line_limit: Mapped[int] = mapped_column(Integer, default=1)
    reserved_lines: Mapped[int] = mapped_column(Integer, default=0)
    #: 游戏硬限制。超了会收到邮件且攻击被强制返回。
    pirate_daily_quota: Mapped[int] = mapped_column(Integer, default=32)
    #: 扫描起来后至少跑这么久才允许被抢占。防止航线一空一占引起秒级反复切换。
    min_dwell_seconds: Mapped[int] = mapped_column(Integer, default=60)
    #: 过了预计战报时间再等这么久仍读不到，就判为「战报缺失」跳过。
    report_grace_minutes: Mapped[int] = mapped_column(Integer, default=30)
    #: 同一条链路两次启动之间的最小间隔。堵的是「战报还没到就反复进信箱扑空」
    #: 的空转——每轮几十秒的导航全白费，还一直占着鼠标不让扫描进来。
    restart_cooldown_seconds: Mapped[int] = mapped_column(Integer, default=300)


class DailyReconciliationRow(Base):
    """开工对账的结果：某个 UTC 日、某条链路，信箱里数到了几份攻击战报。

    ⚠️ **这张表不是派遣台账，一行也不代表一发派遣。** 它只记「观测到 N 份战报」
    这一个数，供 `repository.count_dispatches_since` 与库内计数取大。
    对账绝不往 `attack_dispatches` 里补行：多一条不存在的派遣，调度器就会以为
    一条航线被占着、等一份永远不会来的战报，要到 6 小时后才被判缺失清掉。
    """

    __tablename__ = "daily_reconciliations"
    __table_args__ = (
        UniqueConstraint("day_utc", "target_kind", name="uq_reconciliation_day_kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    #: `YYYY-MM-DD`，**UTC+0 的那一天**——配额的日界就是 UTC 00:00。
    #: 存成字符串是为了能和 SQLite 的 `date(dispatched_at_utc)` 直接比。
    day_utc: Mapped[str] = mapped_column(String(10), index=True)
    #: `pirate` / `bot`，与 `attack_intents.target_kind` 同一套词。
    target_kind: Mapped[str] = mapped_column(String(16))
    #: 那天信箱里数到的本链路战报份数。
    observed_reports: Mapped[int] = mapped_column(Integer, default=0)
    #: 有没有一直翻到「昨天的报告」。为假时上面那个数只是「今天至少这么多」。
    #:
    #: **它不是过滤条件。** 下界照样参与配额取大——扔掉它就等于回到只按库算，
    #: 也就是回到会超额的那一侧。这一列只作诊断：日志要说清那个数是不是全天。
    complete: Mapped[bool] = mapped_column(Boolean, default=False)
    #: 那天库内已被游戏接受的**攻击**派遣数（侦察发不数，口径同
    #: `repository.count_dispatches_since`）。当前事实，照实写。
    dispatched_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    #: 那天**已用配额** = `dispatched_count` 与 `observed_reports` 取大，
    #: 且按 UTC 日**只增不减**。多一层只增不减是因为库可能被换过/清过：那时
    #: `dispatched_count` 会掉下来，而游戏里已经用掉的额度不会跟着退回去。
    #: 偏大只让助手提前收手，偏小才会白飞舰队。
    attacks_used: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    #: 写这一刻还有几发已派出、还没有战报、且还没被判放弃。
    #: ⚠️ **这是瞬时状态，不是计数**，所以它可增可减——做成只增不减的话，
    #: 舰队全回来之后那个数会永远停在最高水位，回读出来的「还在等」全是假的。
    awaiting_reports: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    reconciled_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)


class SystemLogRow(Base):
    """实机脚本与控制台的诊断输出，集中一行一条。

    **为什么要有这张表**：实机跑在一台机器上，人常在另一台机器上看控制台。
    `print` 只落在跑实机那台的 cmd 窗口和它本地的 `var/logs/mission-*.log` 里，
    换台机器完全看不到。数据库是**额外的一份**（双写），不取代任何现有输出——
    库连不上的时候，本机那份仍然是全的。

    **刻意没有 `seq` 列。** 写入是同一个进程 FIFO 排队、后台线程按序批量刷盘，
    所以同一进程内 `id` 递增就是发生顺序，再加一列序号只是在每条日志上多摊一次
    写入成本，而这条链路一轮（海盗半小时）就有几千条。跨进程的先后由
    `logged_at_utc` 回答——那是**产生时刻**，不是入库时刻，正是为了让批量刷盘
    与网络抖动不改变时间线。

    `payload_json` 用 `Text` 存 JSON 而不是 `JSONB`：测试跑 SQLite，生产才是
    PostgreSQL，同 `mission_tasks.params_json` 的先例。
    """

    __tablename__ = "system_log"
    __table_args__ = (
        # 主视图：按时刻倒序翻页。带上 `id` 是因为同一毫秒里能有好几条，
        # 只按时刻排的话翻页会在边界上重复或漏掉行。
        Index("ix_system_log_logged_at_id", "logged_at_utc", "id"),
        Index("ix_system_log_run_id_id", "run_id", "id"),
        Index("ix_system_log_host_logged_at", "host", "logged_at_utc"),
        Index("ix_system_log_level_logged_at", "level", "logged_at_utc"),
    )

    #: ⚠️ `with_variant(Integer, "sqlite")` **不是可选的**。实测（2026-08-16）：
    #: 纯 `BigInteger` 主键在 SQLite 上建出来是 `BIGINT`，而 SQLite 只把写成
    #: `INTEGER PRIMARY KEY` 的列当 rowid 别名——插入不带 id 当场
    #: `IntegrityError: NOT NULL constraint failed`，自增根本不发生。
    #: 加了变体之后：SQLite 上是 `INTEGER`（自增可用），PostgreSQL 上仍是
    #: `BIGSERIAL`。量大不用 UUID，就是为了这一列能便宜地既当主键又当顺序号。
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    #: **产生时刻**，不是入库时刻。批量刷盘会把入库推后几百毫秒到几秒，
    #: 而这一列是所有「那一刻发生了什么」的判据。
    logged_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)
    #: `DEBUG` / `INFO` / `WARNING` / `ERROR`。
    level: Mapped[str] = mapped_column(String(8))
    #: 产生它的模块，如 `tools.bot_loop`。
    source: Mapped[str] = mapped_column(String(64))
    #: 机器名。跨机查看的刚需——两台机器的日志混在一张表里，没有它就分不出
    #: 「实机那台没动静」和「我这台没在跑」。
    host: Mapped[str] = mapped_column(String(64))
    #: 同一台机器上可以并存多个 runner（调度器起的那个 + 手工直跑的），
    #: 光靠 host 分不开。
    pid: Mapped[int] = mapped_column(Integer)
    #: 属于哪一轮。**可空**：手工直跑不属于任何一轮，控制台自己的日志同理。
    #:
    #: ⚠️ 外键**不带 `ondelete="CASCADE"`**：日志是事后翻账用的，一轮记录被清掉
    #: 不该顺手把「那一轮到底发生了什么」也一起删了。
    run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("mission_runs.id"), nullable=True, index=False
    )
    #: 冗余一份任务号，任务被删掉之后仍然查得到——同 `mission_runs.task_id`
    #: 的先例（见那一列的注释），同样不加外键。
    task_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: `pirate` / `bot` / `scan` / `ranking`。
    mission_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    message: Mapped[str] = mapped_column(Text)
    #: 坐标、预设名、耗时、异常栈之类的结构化附加信息。
    payload_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
