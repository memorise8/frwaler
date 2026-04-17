from pydantic import BaseModel, field_validator
from typing import Optional, List, Any, Dict


class RegisterRequest(BaseModel):
    name: str
    email: str

    @field_validator("name")
    @classmethod
    def name_min_length(cls, v: str) -> str:
        if len(v.strip()) < 2:
            raise ValueError("name must be at least 2 characters")
        return v.strip()

    @field_validator("email")
    @classmethod
    def email_basic_validation(cls, v: str) -> str:
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("invalid email address")
        return v.strip().lower()


class RegisterResponse(BaseModel):
    key: str
    plan: str
    message: str


class LicenseInfo(BaseModel):
    key: str
    owner: str
    email: Optional[str] = None
    plan: str
    active: int
    created_at: Optional[str] = None
    daily_usage: int = 0
    monthly_usage: int = 0
    ip: Optional[str] = None


class AdminActionResponse(BaseModel):
    key: str
    status: str  # 'approved' | 'rejected' | 'deactivated'
    message: str


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


class BjtParameters(BaseModel):
    vceo_v: Optional[float] = None
    vcbo_v: Optional[float] = None
    vebo_v: Optional[float] = None
    ic_max_a: Optional[float] = None
    hfe_min: Optional[float] = None
    hfe_max: Optional[float] = None
    icbo_a_at_vcb: Optional[float] = None
    ft_hz: Optional[float] = None
    pd_w: Optional[float] = None
    tj_max_c: Optional[float] = None
    polarity: Optional[str] = None
    package: Optional[str] = None


class MosfetParameters(BaseModel):
    bvdss_v: Optional[float] = None
    vgs_th_v: Optional[float] = None
    rds_on_ohm: Optional[float] = None
    id_max_a: Optional[float] = None
    idss_a: Optional[float] = None
    qg_c: Optional[float] = None
    pd_w: Optional[float] = None
    tj_max_c: Optional[float] = None
    gate_oxide: Optional[str] = None
    polarity: Optional[str] = None  # 'N-channel' | 'P-channel'
    package: Optional[str] = None


class FactorSource(BaseModel):
    title: str
    url: str
    doi: Optional[str] = None
    note: Optional[str] = None


class FactorScore(BaseModel):
    name: str
    score: float
    weight: float
    coverage: float
    rationale: str
    sources: List[FactorSource] = []
    value: Optional[float] = None
    direction: str


class HeritageMatch(BaseModel):
    mpn: str
    manufacturer: Optional[str] = None
    qual_level: Optional[str] = None
    similarity: float
    source_url: Optional[str] = None


class ScreeningRequest(BaseModel):
    mpn: Optional[str] = None
    manufacturer: Optional[str] = None


class RiskFlag(BaseModel):
    code: str
    message: str
    severity: str  # 'info' | 'warning' | 'critical'


class ScreeningReport(BaseModel):
    id: str
    input_mpn: Optional[str] = None
    input_source: str  # 'pdf' | 'mpn'
    parameters: BjtParameters
    factor_scores: List[FactorScore] = []
    overall_score: float
    status: str  # 'pass' | 'caution' | 'fail'
    confidence: float
    heritage_matches: List[HeritageMatch] = []
    risk_flags: List[RiskFlag] = []
    extraction_confidence: Optional[float] = None
    tokens_used: int = 0
    created_at: Optional[str] = None


class FactorOut(BaseModel):
    factor_name: str
    part_type: str
    weight: float
    direction: str
    rationale: str
    sources: List[FactorSource] = []
    thresholds: Optional[Dict[str, Any]] = None


class FeedbackRequest(BaseModel):
    report_id: str
    factor_name: Optional[str] = None
    rating: str  # 'up' | 'down'
    comment: Optional[str] = None


class FeedbackResponse(BaseModel):
    id: int
    message: str


class FeedbackFactorSummary(BaseModel):
    factor_name: Optional[str]
    up: int
    down: int


class FeedbackSummary(BaseModel):
    report_id: str
    total_up: int
    total_down: int
    per_factor: List[FeedbackFactorSummary]


# ─── User Account Schemas ────────────────────────────────────────────────────


class SignupRequest(BaseModel):
    email: str
    name: str
    password: str

    @field_validator("email")
    @classmethod
    def email_valid(cls, v: str) -> str:
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("invalid email address")
        return v.strip().lower()

    @field_validator("name")
    @classmethod
    def name_valid(cls, v: str) -> str:
        if len(v.strip()) < 2:
            raise ValueError("name must be at least 2 characters")
        return v.strip()

    @field_validator("password")
    @classmethod
    def password_valid(cls, v: str) -> str:
        if len(v) < 6:
            raise ValueError("password must be at least 6 characters")
        return v


class LoginRequest(BaseModel):
    email: str
    password: str


class AuthResponse(BaseModel):
    token: str
    user_id: str
    email: str
    name: str
    license_key: str
    expires_in: int  # seconds


class UserProfile(BaseModel):
    user_id: str
    email: str
    name: str
    license_key: Optional[str]
    created_at: Optional[str]


class ReportListItem(BaseModel):
    id: str
    input_mpn: Optional[str] = None
    input_source: str
    overall_score: float
    status: str
    confidence: float
    created_at: Optional[str] = None


class ReportListResponse(BaseModel):
    reports: List[ReportListItem]
    page: int
    per_page: int
    total: int
