"""Application configuration with safety-preserving defaults."""

from pydantic_settings import BaseSettings, SettingsConfigDict

from evo_helper.domain.fleet_preset import DEFAULT_PRESET


class Settings(BaseSettings):
    """Runtime settings sourced from environment variables or an optional .env file."""

    model_config = SettingsConfigDict(env_prefix="EVO_HELPER_", env_file=".env", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8000
    dry_run: bool = True
    database_url: str = "sqlite:///var/evo-helper.db"
    #: In-game fleet preset used for scanning. Its signature is still
    #: verified before any dispatch; this only prefills the plan form.
    default_fleet_preset: str = DEFAULT_PRESET.name
    default_fleet_preset_signature: str = DEFAULT_PRESET.signature

    def model_post_init(self, __context: object) -> None:
        if self.host != "127.0.0.1":
            raise ValueError("EVO-Helper may listen only on 127.0.0.1")
