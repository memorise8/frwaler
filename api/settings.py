import os
from pathlib import Path

from pydantic_settings import BaseSettings


DEFAULT_DB_PATH = os.environ.get(
    "FINOLAW_DB_PATH",
    str(Path(__file__).resolve().parent.parent / "data" / "data.db"),
)


class Settings(BaseSettings):
    database_url: str = DEFAULT_DB_PATH
    cors_origins: list[str] = ["http://localhost:3001", "http://localhost:30003"]
    pro_api_url: str = ""  # e.g. "https://pro.crawler-api.com"
    pro_license_key: str = ""  # User's license key
    openai_api_key: str = ""

    class Config:
        env_prefix = "CRAWLER_"


settings = Settings()
