from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://medintel:medintel@localhost:5432/medintel"
    redis_url: str = "redis://localhost:6379/0"

    anthropic_api_key: str | None = None
    llm_model: str = "claude-haiku-4-5-20251001"
    hunter_api_key: str | None = None

    countries: list[str] = Field(default_factory=lambda: ["CR", "PA"])

    data_input_dir: Path = ROOT / "data" / "input"
    data_output_dir: Path = ROOT / "data" / "output"
    snapshots_dir: Path = ROOT / "data" / "snapshots"


settings = Settings()
