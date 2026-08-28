import json

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings


@pytest.fixture(autouse=True)
def clear_settings_cache() -> None:
    get_settings.cache_clear()


def test_missing_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize("environment", ["local", "test", "production"])
def test_valid_environment_loads(environment: str) -> None:
    settings = Settings(
        database_url=(
            "postgresql://user:password@localhost:5432/app?sslmode=require&channel_binding=require"
        ),
        environment=environment,  # type: ignore[arg-type]
        _env_file=None,
    )

    assert settings.environment == environment
    assert settings.database_url.scheme == "postgresql+asyncpg"
    normalized_url = str(settings.database_url)
    assert "ssl=require" in normalized_url
    assert "sslmode" not in normalized_url
    assert "channel_binding" not in normalized_url


def test_invalid_environment_fails() -> None:
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql://user:password@localhost:5432/app",
            environment="staging",  # type: ignore[arg-type]
            _env_file=None,
        )


def test_modal_outbound_allowlist_accepts_comma_separated_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:password@localhost:5432/app")
    monkeypatch.setenv(
        "MODAL_OUTBOUND_DOMAIN_ALLOWLIST",
        "control.example.com,api.openai.com",
    )

    settings = Settings(_env_file=None)

    assert settings.modal_outbound_domain_allowlist == [
        "control.example.com",
        "api.openai.com",
    ]


def test_settings_parse_watchdog_timeouts_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:password@localhost:5432/app")
    monkeypatch.setenv("TOOL_TIMEOUT_S", "120")
    monkeypatch.setenv("TOOL_RECOVERY_TIMEOUT_S", "60")

    settings = Settings(_env_file=None)

    assert settings.tool_timeout_s == 120
    assert settings.tool_recovery_timeout_s == 60


def test_watchdog_timeout_defaults_fit_default_sandbox_timeout() -> None:
    settings = Settings(
        database_url="postgresql://user:password@localhost:5432/app",
        _env_file=None,
    )
    # Defaults (600 + 300 + 60 = 960s) fit the default 3600s sandbox lifetime.
    assert settings.tool_timeout_s + settings.tool_recovery_timeout_s + 60 <= 3600


def test_tracing_defaults_off_and_api_key_is_masked() -> None:
    secret = "review-secret-key"
    database_password = "database-secret"
    settings = Settings(
        database_url=f"postgresql://user:{database_password}@localhost:5432/app",
        langsmith_api_key=secret,
        _env_file=None,
    )

    assert settings.langsmith_tracing is False
    assert secret not in repr(settings)
    serialized = json.dumps(settings.model_dump(mode="json"))
    assert secret not in serialized
    assert database_password not in repr(settings)
    assert database_password not in str(settings)
    assert database_password not in serialized
