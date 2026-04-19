from functools import lru_cache
from urllib.parse import quote_plus

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    openai_api_key: str = Field(alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")
    embedding_model: str = Field(default="text-embedding-3-small", alias="EMBEDDING_MODEL")

    db_host: str = Field(alias="DB_HOST")
    db_name: str = Field(alias="DB_NAME")
    db_user: str = Field(alias="DB_USER")
    db_password: str = Field(alias="DB_PASSWORD")
    db_port: int = Field(default=5432, alias="DB_PORT")
    db_sslmode: str = Field(default="require", alias="DB_SSLMODE")

    redis_url: str = Field(default="redis://redis:6379/0", alias="REDIS_URL")
    ticket_lock_ttl_seconds: int = Field(default=900, alias="TICKET_LOCK_TTL_SECONDS")

    tool_failure_simulation: bool = Field(default=True, alias="TOOL_FAILURE_SIMULATION")
    tool_failure_rate: float = Field(default=0.08, alias="TOOL_FAILURE_RATE")

    request_rate_limit_per_minute: int = Field(default=80, alias="REQUEST_RATE_LIMIT_PER_MINUTE")
    kb_chunk_max_chars: int = Field(default=1200, alias="KB_CHUNK_MAX_CHARS")
    frontend_static_dir: str | None = Field(default=None, alias="FRONTEND_STATIC_DIR")
    frontend_api_base_url: str | None = Field(default=None, alias="FRONTEND_API_BASE_URL")

    pgvector_enabled: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def database_url(self) -> str:
        encoded_user = quote_plus(self.db_user)
        encoded_password = quote_plus(self.db_password)
        ssl_suffix = f"?sslmode={quote_plus(self.db_sslmode)}" if self.db_sslmode else ""
        return (
            f"postgresql+psycopg2://{encoded_user}:{encoded_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}{ssl_suffix}"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
