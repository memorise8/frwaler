from pydantic import BaseModel
from typing import Optional, List, Any, Dict


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
