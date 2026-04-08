import json
from pathlib import Path
from fastapi import APIRouter

router = APIRouter(prefix="/api/reports", tags=["reports"])

REPORTS_DIR = Path(__file__).resolve().parent.parent.parent / "reports"

@router.get("/countries")
async def list_country_reports():
    """List available country reports."""
    countries = []
    # Check for each country's smartfind report
    for prefix, name, flag in [
        ("kr_smartfind", "South Korea", "\U0001f1f0\U0001f1f7"),
        ("au_test", "Australia", "\U0001f1e6\U0001f1fa"),
        ("de_test", "Germany", "\U0001f1e9\U0001f1ea"),
        ("uk_test", "United Kingdom", "\U0001f1ec\U0001f1e7"),
    ]:
        reports = sorted(REPORTS_DIR.glob(f"{prefix}_*.json"))
        if reports:
            countries.append({"code": prefix[:2].upper(), "name": name, "flag": flag, "report": reports[-1].name})
    return countries

@router.get("/country/{code}")
async def get_country_report(code: str):
    """Get detailed report for a country."""
    prefix_map = {"KR": "kr_smartfind", "AU": "au_test", "DE": "de_test", "UK": "uk_test"}
    prefix = prefix_map.get(code.upper())
    if not prefix:
        return {"error": "Unknown country code"}

    reports = sorted(REPORTS_DIR.glob(f"{prefix}_*.json"))
    if not reports:
        return {"error": "No report found"}

    with open(reports[-1]) as f:
        data = json.load(f)

    return data

@router.get("/failure-analysis")
async def get_failure_analysis():
    """Get GPT failure analysis results."""
    path = REPORTS_DIR / "kr_failure_analysis.json"
    if not path.exists():
        return {"error": "No analysis found. Run scripts/analyze_failures.py first."}
    with open(path) as f:
        return json.load(f)
