from pydantic import Field
from pydantic_settings import BaseSettings


class ProSettings(BaseSettings):
    openai_api_key: str = ""
    # Reads directly from GEMINI_KEY (no PRO_ prefix) to match existing .env convention.
    gemini_api_key: str = Field(default="", validation_alias="GEMINI_KEY")
    license_db_path: str = "./data/licenses.db"
    max_requests_per_day: int = 100  # per license key
    max_requests_per_month: int = 1000
    max_global_requests_per_day: int = 500  # Gemini free tier daily cap
    screening_db_path: str = "./data/screening.db"
    factors_seed_path: str = "./pro_server/data/bjt_factors.json"
    heritage_seed_path: str = "./pro_server/data/heritage_bjt_seed.json"
    mosfet_factors_seed_path: str = "./pro_server/data/mosfet_factors.json"
    mosfet_heritage_seed_path: str = "./pro_server/data/heritage_mosfet_seed.json"
    # Comma-separated allowed origins for CORS. Empty = allow all (dev only).
    # Example: "https://myapp.vercel.app,https://my-custom-domain.com"
    allowed_origins: str = ""
    allow_vercel_preview: bool = True  # allow *.vercel.app preview URLs
    jwt_secret: str = "change-me-in-production"
    jwt_expiry_hours: int = 24
    admin_password: str = "admin1234"  # change in production via PRO_ADMIN_PASSWORD

    class Config:
        env_prefix = "PRO_"


pro_settings = ProSettings()
