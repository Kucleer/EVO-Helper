from evo_helper.config import Settings


def test_defaults_are_dry_run_and_loopback_only() -> None:
    settings = Settings()

    assert settings.dry_run is True
    assert settings.host == "127.0.0.1"
