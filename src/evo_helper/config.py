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
    dry_run: bool = True
    database_url: str = "sqlite:///var/evo-helper.db"
    #: In-game fleet preset used for scanning. Its signature is still
    #: verified before any dispatch; this only prefills the plan form.
    default_fleet_preset: str = DEFAULT_PRESET.name
    default_fleet_preset_signature: str = DEFAULT_PRESET.signature

    # -- 部署相关（换机器 / 换账号就要改的地方）---------------------------------

    #: Tesseract 可执行文件。默认是 Windows 安装器的落点。
    #: 装到别处（或装的是 portable 版）时用 `EVO_HELPER_TESSERACT_PATH` 指过去。
    tesseract_path: str = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

    #: 主星，写成 `星系:恒星系:位置`。换账号就要换——飞行时间、战报匹配、
    #: 海盗巡航范围全都从它算起。
    origin: str = str(ORIGIN)

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
