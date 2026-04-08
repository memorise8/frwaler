import json
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/config", tags=["config"])

CONFIGS_DIR = Path(__file__).resolve().parent.parent.parent / "crawler" / "sites" / "configs"


@router.get("/list")
async def list_configs():
    configs = []
    for p in sorted(CONFIGS_DIR.glob("*.json")):
        configs.append(p.stem)
    return configs


@router.get("/{site_id}")
async def get_config(site_id: str):
    path = CONFIGS_DIR / f"{site_id}.json"
    if not path.exists():
        raise HTTPException(404, f"Config not found: {site_id}")
    return json.loads(path.read_text(encoding="utf-8"))


@router.put("/{site_id}")
async def update_config(site_id: str, body: dict):
    path = CONFIGS_DIR / f"{site_id}.json"
    if not path.exists():
        raise HTTPException(404, f"Config not found: {site_id}")
    # Validate required fields
    if "site_id" not in body:
        body["site_id"] = site_id
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"status": "saved", "site_id": site_id}


class CloneConfigRequest(BaseModel):
    source_site_id: str
    new_site_id: str
    new_url: str
    new_site_name: Optional[str] = None


@router.post("/clone")
async def clone_config(req: CloneConfigRequest):
    source_path = CONFIGS_DIR / f"{req.source_site_id}.json"
    if not source_path.exists():
        raise HTTPException(404, f"Source config not found: {req.source_site_id}")

    target_path = CONFIGS_DIR / f"{req.new_site_id}.json"
    if target_path.exists():
        raise HTTPException(400, f"Config already exists: {req.new_site_id}")

    config = json.loads(source_path.read_text(encoding="utf-8"))

    # Update with new values
    config["site_id"] = req.new_site_id
    if req.new_site_name:
        config["site_name"] = req.new_site_name

    # Update URL in list_page
    if "list_page" in config and isinstance(config["list_page"], dict):
        config["list_page"]["url"] = req.new_url

    # Update base_url from new URL
    from urllib.parse import urlparse
    parsed = urlparse(req.new_url)
    config["base_url"] = f"{parsed.scheme}://{parsed.netloc}"

    target_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"status": "cloned", "source": req.source_site_id, "target": req.new_site_id}
