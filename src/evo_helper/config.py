"""Application configuration with safety-preserving defaults."""

from pydantic_settings import BaseSettings, SettingsConfigDict

from evo_helper.domain.fleet_preset import DEFAULT_PRESET
from evo_helper.domain.missions import ORIGIN
from evo_helper.domain.models import Coordinate


class Settings(BaseSettings):
    """Runtime settings sourced from environment variables or an optional .env file."""

    model_config = SettingsConfigDict(env_prefix="EVO_HELPER_", env_file=".env", extra="ignore")

    #: 监听地址。默认 `0.0.0.0`，即局域网内的手机/平板也能打开控制台——
    #: 这是用户明确要的。**代价是控制台在同网段内不设防**：读页面不验票
    #: （用户已确认），写请求只有同源校验，而局域网里的浏览器天然同源。
    #: 所以这个默认值只适用于可信内网；在公共 Wi-Fi 上跑请显式设回
    #: `EVO_HELPER_HOST=127.0.0.1`。
    host: str = "0.0.0.0"  # noqa: S104 - 局域网可访问是明确需求，见上
    #: 避开 8000/8080/8888 这类常规端口，减少与本机其他开发服务撞车。
    port: int = 8770
    database_url: str = "sqlite:///var/evo-helper.db"
    #: `system_log` 保留多少天。控制台每次启动清一次早于这个期限的行。
    #: 0 或负数表示**不清理**（不是「全删」——见 `infrastructure.system_log_db`）。
    system_log_retention_days: int = 14
    #: In-game fleet preset used for scanning. Its signature is still
    #: verified before any dispatch; this only prefills the plan form.
    default_fleet_preset: str = DEFAULT_PRESET.name
    default_fleet_preset_signature: str = DEFAULT_PRESET.signature

    # -- 部署相关（换机器 / 换账号就要改的三处）---------------------------------

    #: Tesseract 可执行文件。默认是 Windows 安装器的落点。
    #: 装到别处（或装的是 portable 版）时用 `EVO_HELPER_TESSERACT_PATH` 指过去。
    tesseract_path: str = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

    #: Chrome 可执行文件。留空则按 `game.game_window.chrome_candidates()` 依次找。
    #: 显式指定时**找不到就报错**，不回落到候选列表——回落的话用户以为跑的是
    #: 自己指定的那个（比如带着已登录 profile 的），实际跑的是另一个。
    chrome_path: str | None = None

    #: 页面 DPR。传给 Chrome 的 `--force-device-scale-factor`。
    #:
    #: 所有点击坐标和 OCR 的 ROI 编码的是**「窗口物理尺寸 ÷ 缩放率」**得到的
    #: CSS 版面，不是物理尺寸。本机物理 1920x1080、系统缩放 125%，窗口 client
    #: 1920x917 物理，游戏按 1536x703 CSS 排版；换到 100% 缩放的机器，同样的
    #: 物理窗口会变成 1920x879 CSS，版面完全不同、坐标全废，而几何校验只看物理
    #: 尺寸，一路都是绿的。钉死这个值，CSS 版面就跟目标机器的系统缩放无关了。
    #:
    #: **这不是偏好项，是标定常量。** 列在这里只为了把它显式写出来：取值必须
    #: 等于 `game.game_window.CALIBRATED_SCALE_FACTOR`，填别的会被当场拦下。
    #: 判据放在 `game_window.verified_scale_factor()`——它要跟同一次标定出来的
    #: `APP_TITLE_BAR_PX`、`CALIBRATED_VIEWPORT` 住在一起，而本模块反过来
    #: import 那边会成环（那边要读 Settings）。
    device_scale_factor: float = 1.25

    #: 主星，写成 `星系:恒星系:位置`。换账号就要换——飞行时间、战报匹配、
    #: 海盗巡航范围全都从它算起。
    origin: str = str(ORIGIN)

    # -- 邮件安全告警 ---------------------------------------------------------

    #: 收到「你的行星被侦察」邮件时的通知收件人。留空时仍会把告警落库，
    #: 但不会尝试联网投递。
    alert_email_to: str | None = None
    #: SMTP 参数使用邮箱服务商的**客户端授权码**，不是网页登录密码。
    smtp_host: str | None = None
    smtp_port: int = 465
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_use_ssl: bool = True
    smtp_from: str | None = None

    @property
    def smtp_ready(self) -> bool:
        """是否具备一次性投递告警所需的完整 SMTP 配置。

        不在这里校验网络或登录：启动服务时因邮箱暂时不可用而失败，会把原本
        本地可用的调度台一起拖垮。真正发送时才报出可见的失败原因。
        """
        return all(
            (
                self.alert_email_to,
                self.smtp_host,
                self.smtp_username,
                self.smtp_password,
                self.smtp_from or self.smtp_username,
            )
        )

    #: 自己的游戏内玩家名。**只用来在排行榜上把自己那一行剔掉。**
    #:
    #: ⚠️ 那一行是**吸附**的，不是固定在某个 y：实机 2026-08-15 里，滚过自己名次
    #: 之前它钉在列表底部（y=837），滚过之后**跳到了顶部**（y≈254）。所以按坐标
    #: 排除是排不掉的——它换个位置继续混进来，而且混在**第一行**，正好是
    #: 「首行变没变」这条到底判据看的地方。
    #:
    #: 换账号就要改。留空则不剔除（自己那一行会被当成榜单行重复入库）。
    player_name: str = "Kucleer"

    @property
    def origin_coordinate(self) -> Coordinate:
        """把 `origin` 解析成坐标；格式不对就当场报错。

        刻意不回落到默认主星：回落的后果是舰队从上一个账号的星球出发，
        飞行时间和战报匹配跟着一起错，而全程一句警告都没有。
        """
        parts = self.origin.split(":")
        if len(parts) != 3 or not all(part.strip().isdigit() for part in parts):
            raise ValueError(f"主星要写成 `星系:恒星系:位置`，收到 {self.origin!r}")
        galaxy, system, position = (int(part) for part in parts)
        return Coordinate(galaxy, system, position)

    @property
    def lan_exposed(self) -> bool:
        """是否绑在了回环之外——启动时据此打印警告，别让人无意中暴露出去。"""
        return self.host not in {"127.0.0.1", "localhost", "::1"}
