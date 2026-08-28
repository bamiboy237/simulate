"""This module loads application configuration from environment variables."""

from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import (
    Field,
    PostgresDsn,
    SecretStr,
    field_serializer,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """This class stores validated settings for the application."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    database_url: PostgresDsn
    database_url_unpooled: PostgresDsn | None = None
    environment: Literal["local", "test", "production"] = "local"
    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "simulate"
    langsmith_otlp_endpoint: str | None = None
    otel_tracing_enabled: bool = False
    model_provider: Literal["openai", "anthropic"] | None = None
    model_name: str | None = None
    model_base_url: str | None = None
    model_api_key: SecretStr | None = None
    model_candidate_provider: Literal["openai", "anthropic"] | None = None
    model_candidate_name: str | None = None
    model_candidate_base_url: str | None = None
    model_candidate_api_key: SecretStr | None = None
    retrieval_enabled: bool = True
    embedding_api_key: SecretStr | None = None
    embedding_base_url: str | None = None
    retrieval_corpus_version: str = "policy-v1"
    modal_enabled: bool = False
    modal_app_name: str = "simulate-mvp"
    modal_timeout_s: int = 3600
    modal_outbound_domain_allowlist: Annotated[list[str], NoDecode] = Field(default_factory=list)
    control_plane_public_url: str | None = None
    investigation_heartbeat_silence_s: int = 90
    simulate_live_e2e: bool = False
    # In-sandbox Prime watchdog timeouts. TOOL_TIMEOUT_S + TOOL_RECOVERY_TIMEOUT_S
    # + MODAL_RUN_RESERVE_S must fit inside the sandbox outer timeout; SandboxSpec
    # validates and rejects violating combinations at launch.
    tool_timeout_s: int = 600
    tool_recovery_timeout_s: int = 300

    @field_validator("modal_outbound_domain_allowlist", mode="before")
    @classmethod
    def parse_outbound_domain_allowlist(cls, v: object) -> list[str]:
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            if v.startswith("[") and v.endswith("]"):
                import json

                try:
                    parsed = json.loads(v)
                    if isinstance(parsed, list):
                        return [str(x).strip() for x in parsed if str(x).strip()]
                except Exception:
                    pass
            return [item.strip() for item in v.split(",") if item.strip()]
        if isinstance(v, (list, tuple, set)):
            return [str(item).strip() for item in v if str(item).strip()]
        return []

    @field_validator("database_url", "database_url_unpooled", mode="before")
    @classmethod
    def use_asyncpg_driver(cls, database_url: object) -> object:
        if not isinstance(database_url, str):
            return database_url

        if database_url.startswith("postgresql://"):
            database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

        parsed_url = urlsplit(database_url)
        query = dict(parse_qsl(parsed_url.query, keep_blank_values=True))
        ssl_mode = query.pop("sslmode", None)
        query.pop("channel_binding", None)
        if ssl_mode is not None:
            query["ssl"] = ssl_mode

        return urlunsplit(parsed_url._replace(query=urlencode(query)))

    @field_serializer("database_url", "database_url_unpooled")
    def serialize_database_url(self, database_url: PostgresDsn | None) -> str | None:
        """This method removes database credentials from serialized settings and logs."""
        if database_url is None:
            return None
        return f"{database_url.scheme}://[REDACTED]"

    @model_validator(mode="after")
    def validate_model_settings(self) -> "Settings":
        """This method accepts complete model configurations or none at all."""
        for provider, name, api_key, base_url in (
            (self.model_provider, self.model_name, self.model_api_key, self.model_base_url),
            (
                self.model_candidate_provider,
                self.model_candidate_name,
                self.model_candidate_api_key,
                self.model_candidate_base_url,
            ),
        ):
            if (provider is None) != (name is None):
                raise ValueError("MODEL_PROVIDER and MODEL_NAME must be set together")
            if provider is not None:
                if api_key is None and not self._has_local_model_endpoint(base_url):
                    raise ValueError(
                        "MODEL_API_KEY is required unless MODEL_BASE_URL points to a local endpoint"
                    )
        return self

    def _has_local_model_endpoint(self, base_url: str | None = None) -> bool:
        if base_url is None:
            return False
        host = urlsplit(base_url).hostname or ""
        return host in {"localhost", "127.0.0.1", "::1"}

    @property
    def model_configured(self) -> bool:
        """This property reports whether these settings build a hosted model."""
        return self.model_provider is not None and self.model_name is not None

    @property
    def candidate_model_configured(self) -> bool:
        """This property reports whether a second hosted model is configured."""
        return self.model_candidate_provider is not None and self.model_candidate_name is not None

    @property
    def migration_database_url(self) -> PostgresDsn:
        """This property returns the direct database URL that schema migrations require."""
        return self.database_url_unpooled or self.database_url

    def __str__(self) -> str:
        return str(self.model_dump(mode="json"))

    def __repr__(self) -> str:
        return f"Settings({self.model_dump(mode='json')!r})"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
