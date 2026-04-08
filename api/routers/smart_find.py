import json
import threading
import uuid
from urllib.parse import urlparse
from fastapi import APIRouter
from pydantic import BaseModel
from ..services.job_manager import create_job, update_job, get_job

router = APIRouter(prefix="/api", tags=["smart-find"])

# In-memory store for finder results
_finder_results = {}


class SmartFindRequest(BaseModel):
    url: str
    max_pages: int = 20
    max_depth: int = 3
    use_ai: bool = True


@router.post("/smart-find")
async def smart_find(req: SmartFindRequest):
    job_id = create_job("smart-find", req.url)
    thread = threading.Thread(
        target=_run_smart_find,
        args=(job_id, req.url, req.max_pages, req.max_depth, req.use_ai),
        daemon=True,
    )
    thread.start()
    return get_job(job_id)


@router.get("/smart-find/{job_id}/files")
async def get_found_files(job_id: str):
    files = _finder_results.get(job_id, [])
    return {"job_id": job_id, "files": files, "total": len(files)}


def _run_smart_find(job_id, url, max_pages, max_depth, use_ai):
    try:
        from crawler.smart_finder import SmartDocumentFinder

        def on_progress(msg):
            update_job(job_id, "running", progress=msg)

        finder = SmartDocumentFinder(delay=1.0, callback=on_progress)
        result = finder.find(url, max_pages=max_pages, max_depth=max_depth, use_ai=use_ai)

        # Store found files
        files = []
        for doc in result.documents:
            files.append({
                "id": doc.id,
                "title": doc.title,
                "file_url": doc.file_url,
                "file_type": doc.file_type,
                "source_page": doc.source_page,
            })
        _finder_results[job_id] = files

        # Save to DB
        saved = 0
        if result.documents:
            try:
                from crawler import db as db_module
                conn = db_module.get_db()
                db_module.init_db(conn)

                # Create site entry
                parsed = urlparse(url)
                site_id = f"sf-{parsed.netloc.replace('www.', '').replace('.', '-')}"
                site_name = f"Smart Find: {parsed.netloc}"
                conn.execute(
                    "INSERT OR IGNORE INTO sites (id, name, base_url) VALUES (?, ?, ?)",
                    (site_id, site_name, f"{parsed.scheme}://{parsed.netloc}")
                )
                conn.commit()

                for doc in result.documents:
                    paper = {
                        "id": None,
                        "site_id": site_id,
                        "external_id": doc.id,
                        "title": doc.title,
                        "authors": "[]",
                        "abstract": "",
                        "category": doc.file_type,
                        "keywords": "[]",
                        "published_date": getattr(doc, "date", "") or "",
                        "url": doc.source_page,
                        "pdf_url": doc.file_url,
                        "doi": "",
                        "department": "",
                        "metadata": json.dumps({"finder": "smart_find"}, ensure_ascii=False),
                    }
                    try:
                        if not db_module.paper_exists(conn, site_id, url=doc.file_url):
                            paper["id"] = str(uuid.uuid4())
                            db_module.upsert_paper(conn, paper)
                            saved += 1
                    except Exception:
                        pass

                conn.close()
            except Exception as e:
                result.errors.append(f"DB save error: {str(e)[:100]}")

        summary = f"{len(result.documents)}개 파일 발견, {saved}개 저장 ({result.fetch_method})"
        if result.errors:
            summary += f" | 오류: {len(result.errors)}건"

        update_job(job_id, status="completed", result=summary)

    except Exception as e:
        update_job(job_id, status="failed", result=str(e)[:200])


@router.post("/smart-find/{job_id}/download")
async def download_found_files(job_id: str):
    """Trigger download of all found files."""
    files = _finder_results.get(job_id, [])
    if not files:
        return {"status": "no_files"}

    download_job_id = create_job("download", f"smart-find-{job_id}")
    thread = threading.Thread(
        target=_run_downloads,
        args=(download_job_id, job_id, files),
        daemon=True,
    )
    thread.start()
    return get_job(download_job_id)


def _run_downloads(download_job_id, find_job_id, files):
    try:
        from crawler.summarizer import download_file_curl
        from crawler import db as db_module

        conn = db_module.get_db()
        downloaded = 0
        failed = 0

        for i, f in enumerate(files):
            update_job(download_job_id, "running", progress=f"다운로드 중: {i+1}/{len(files)}")
            try:
                # Find paper in DB by pdf_url
                row = conn.execute(
                    "SELECT id, site_id, external_id FROM papers WHERE pdf_url = ?",
                    (f["file_url"],)
                ).fetchone()
                if row:
                    paper_id, site_id, ext_id = row[0], row[1], row[2]
                    success = download_file_curl(
                        f["file_url"], site_id, ext_id or paper_id, conn
                    )
                    if success:
                        downloaded += 1
                    else:
                        failed += 1
                else:
                    failed += 1
            except Exception:
                failed += 1

        conn.close()
        update_job(download_job_id, status="completed",
                   result=f"다운로드 완료: {downloaded}개 성공, {failed}개 실패")
    except Exception as e:
        update_job(download_job_id, status="failed", result=str(e)[:200])
