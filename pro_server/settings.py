from pydantic_settings import BaseSettings


class ProSettings(BaseSettings):
    openai_api_key: str = ""
    license_db_path: str = "./data/licenses.db"
    max_requests_per_day: int = 100  # per license key
    max_requests_per_month: int = 1000

    class Config:
        env_prefix = "PRO_"


pro_settings = ProSettings()
