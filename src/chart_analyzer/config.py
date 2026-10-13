"""Application settings, read from environment variables (and ``.env`` in development)."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- service ------------------------------------------------------------------------------
    environment: str = "development"
    log_level: str = "INFO"
    log_json: bool = True

    # --- infrastructure -----------------------------------------------------------------------
    database_url: str = "postgresql+asyncpg://chart:chart@localhost:5432/chart_analyzer"
    redis_url: str = "redis://localhost:6379/0"
    rabbitmq_url: str = "amqp://guest:guest@localhost:5672/"
    upload_dir: Path = Path("uploads")

    # --- AI (any OpenAI-compatible endpoint, e.g. a LiteLLM proxy) ----------------------------
    openai_api_key: SecretStr = SecretStr("")
    openai_base_url: str = "https://api.openai.com/v1"
    openai_recognition_model: str = "gpt-4o-mini"
    openai_analysis_model: str = "gpt-4o"
    openai_timeout_seconds: float = 60.0
    openai_recognition_max_tokens: int = 300
    openai_analysis_max_tokens: int = 2500

    # --- market data --------------------------------------------------------------------------
    finnhub_api_key: SecretStr = SecretStr("")
    finnhub_base_url: str = "https://finnhub.io/api/v1"
    finnhub_timeout_seconds: float = 10.0
    news_lookback_days: int = 7
    news_candidates: int = 12  # articles offered to the AI
    max_news: int = 3  # articles shown to the user
    cache_ttl_price_seconds: int = 60
    cache_ttl_news_seconds: int = 600
    cache_ttl_asset_seconds: int = 86_400
    cache_ttl_analyst_review_seconds: int = 86_400

    # --- uploads ------------------------------------------------------------------------------
    max_image_bytes: int = 10 * 1024 * 1024
    max_image_side_px: int = 1568  # larger charts are downscaled; bounds the AI input cost

    # --- quota --------------------------------------------------------------------------------
    quota_daily_tokens: int = 500_000
    quota_reservation_tokens: int = 20_000  # upper bound of what one analysis can spend
    quota_max_active_analyses: int = 3

    # --- worker -------------------------------------------------------------------------------
    worker_prefetch: int = 8
    analysis_lease_seconds: int = 180
    retry_delays_seconds: tuple[int, ...] = Field(default=(5, 30, 120))
    retry_busy_delay_seconds: int = 30
    outbox_poll_interval_seconds: float = 0.5
    outbox_batch_size: int = 50

    # --- progress events ----------------------------------------------------------------------
    progress_ttl_seconds: int = 3600
    sse_keepalive_seconds: float = 15.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
