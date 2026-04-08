import json
from pathlib import Path
from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["site-status"])

STATUS_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "site_status.json"


def _load_status():
    if STATUS_FILE.exists():
        return json.loads(STATUS_FILE.read_text())
    return {}


@router.get("/site-status")
async def get_all_status():
    return _load_status()


@router.get("/site-status/{site_id}")
async def get_site_status(site_id: str):
    data = _load_status()
    return data.get(site_id, {
        "status": "not_tested",
        "method": None,
        "reason": None,
        "tested_at": None,
        "country": None,
    })


@router.put("/site-status/{site_id}")
async def update_site_status(site_id: str, body: dict):
    data = _load_status()
    data[site_id] = {
        "status": body.get("status", "not_tested"),
        "method": body.get("method"),
        "reason": body.get("reason"),
        "tested_at": body.get("tested_at"),
        "country": body.get("country"),
    }
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return data[site_id]
