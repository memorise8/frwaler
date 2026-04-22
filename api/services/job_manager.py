import uuid
from datetime import datetime
from typing import Optional

_jobs: dict[str, dict] = {}


def create_job(job_type: str, site_id: str) -> str:
    job_id = str(uuid.uuid4())[:8]
    _jobs[job_id] = {
        "id": job_id,
        "type": job_type,
        "site_id": site_id,
        "status": "running",
        "progress": None,
        "started_at": datetime.now().isoformat(),
        "completed_at": None,
        "result": None,
        "diagnosis": None,
    }
    return job_id


def update_job(job_id: str, status: str, progress: str = None, result: str = None, diagnosis: list = None):
    if job_id in _jobs:
        _jobs[job_id]["status"] = status
        if progress:
            _jobs[job_id]["progress"] = progress
        if result:
            _jobs[job_id]["result"] = result
        if diagnosis is not None:
            _jobs[job_id]["diagnosis"] = diagnosis
        if status in ("completed", "failed"):
            _jobs[job_id]["completed_at"] = datetime.now().isoformat()


def get_job(job_id: str) -> Optional[dict]:
    return _jobs.get(job_id)


def list_jobs() -> list[dict]:
    return sorted(_jobs.values(), key=lambda j: j["started_at"], reverse=True)
