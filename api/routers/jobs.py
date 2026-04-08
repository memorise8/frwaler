from fastapi import APIRouter, HTTPException
from ..schemas import JobOut
from ..services.job_manager import get_job, list_jobs

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("", response_model=list[JobOut])
async def get_jobs():
    return list_jobs()


@router.get("/{job_id}", response_model=JobOut)
async def get_job_status(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job
