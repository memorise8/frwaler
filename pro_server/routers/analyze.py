from fastapi import APIRouter, Depends
from ..auth import verify_license, log_usage
from pydantic import BaseModel

router = APIRouter(prefix="/pro/api", tags=["analyze"])


class AnalyzeRequest(BaseModel):
    url: str


class AnalyzeResponse(BaseModel):
    site_type: str  # html, single-page, spa, api
    selectors: dict
    recommendation: str


@router.post("/analyze", response_model=AnalyzeResponse)
async def analyze_site(req: AnalyzeRequest, license_info: dict = Depends(verify_license)):
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from crawler.analyzer import analyze_url

    result = analyze_url(req.url)

    log_usage(license_info["key"], "analyze")

    return AnalyzeResponse(
        site_type=result.get("site_type", "unknown"),
        selectors=result.get("selectors", {}),
        recommendation=result.get("recommendation", ""),
    )
