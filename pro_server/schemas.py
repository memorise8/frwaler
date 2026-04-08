from pydantic import BaseModel
from typing import Optional


class AutoAddRequest(BaseModel):
    url: str
    site_id: Optional[str] = None
    site_name: Optional[str] = None
    browser: bool = False
    max_iterations: Optional[int] = 10


class AutoAddResponse(BaseModel):
    success: bool
    site_id: str
    method: str
    reason: str
    config: Optional[dict] = None


class LicenseKeyRequest(BaseModel):
    key: str


class ProConfigOut(BaseModel):
    pro_enabled: bool
    plan: Optional[str] = None
    reason: Optional[str] = None
