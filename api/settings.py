from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "/data_raid/ruci_workspace/crawler-poc/data/papers.db"
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:3001", "http://localhost:30003"]
    pro_api_url: str = ""  # e.g. "https://pro.crawler-api.com"
    pro_license_key: str = ""  # User's license key
    openai_api_key: str = ""

    class Config:
        env_prefix = "CRAWLER_"


settings = Settings()
