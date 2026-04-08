import json
from fastapi import APIRouter, Depends, Query
import aiosqlite
from ..database import get_db
from ..schemas import SiteOut, PaperOut, PaperListOut
from ..country_map import classify_country
from .site_status import _load_status as _load_site_status

router = APIRouter(prefix="/api/sites", tags=["sites"])


@router.get("", response_model=list[SiteOut])
async def list_sites(db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute("""
        SELECT s.id, s.name, s.base_url, s.last_crawled,
               COUNT(p.id) as paper_count
        FROM sites s
        LEFT JOIN papers p ON s.id = p.site_id
        GROUP BY s.id
        ORDER BY paper_count DESC
    """)
    rows = await cursor.fetchall()
    site_status = _load_site_status()
    result = []
    for r in rows:
        sid = r[0]
        ss = site_status.get(sid, {})
        paper_count = r[4]
        status = ss.get("status", "not_tested")
        if status == "not_tested" and paper_count > 0:
            status = "success"
        result.append(
            SiteOut(
                id=sid,
                name=r[1],
                base_url=r[2],
                last_crawled=r[3],
                paper_count=paper_count,
                country=classify_country(sid, r[1], r[2]),
                crawl_status=status,
                crawl_status_reason=ss.get("reason"),
            )
        )
    return result


@router.get("/{site_id}/papers", response_model=PaperListOut)
async def get_site_papers(
    site_id: str,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    q: str = Query(None),
    db: aiosqlite.Connection = Depends(get_db),
):
    offset = (page - 1) * limit

    if q:
        count_cursor = await db.execute(
            "SELECT COUNT(*) FROM papers WHERE site_id = ? AND (title LIKE ? OR abstract LIKE ?)",
            (site_id, f"%{q}%", f"%{q}%")
        )
        cursor = await db.execute(
            "SELECT * FROM papers WHERE site_id = ? AND (title LIKE ? OR abstract LIKE ?) ORDER BY crawled_at DESC LIMIT ? OFFSET ?",
            (site_id, f"%{q}%", f"%{q}%", limit, offset)
        )
    else:
        count_cursor = await db.execute("SELECT COUNT(*) FROM papers WHERE site_id = ?", (site_id,))
        cursor = await db.execute(
            "SELECT * FROM papers WHERE site_id = ? ORDER BY crawled_at DESC LIMIT ? OFFSET ?",
            (site_id, limit, offset)
        )

    total = (await count_cursor.fetchone())[0]
    rows = await cursor.fetchall()
    items = [_row_to_paper(r) for r in rows]

    return PaperListOut(items=items, total=total, page=page, limit=limit)


def _row_to_paper(r) -> PaperOut:
    """Convert a DB row to PaperOut, parsing JSON fields.

    Column order (0-based):
      0=id, 1=site_id, 2=external_id, 3=title, 4=authors, 5=abstract,
      6=category, 7=keywords, 8=published_date, 9=url, 10=pdf_url,
      11=doi, 12=department, 13=metadata, 14=crawled_at,
      15=summary, 16=download_status, 17=download_path
    """
    def parse_json_list(val):
        if not val:
            return []
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    return PaperOut(
        id=r[0],
        site_id=r[1],
        external_id=r[2],
        title=r[3] or "",
        authors=parse_json_list(r[4]),
        abstract=r[5],
        category=r[6],
        keywords=parse_json_list(r[7]),
        published_date=r[8],
        url=r[9],
        pdf_url=r[10],
        doi=r[11],
        department=r[12],
        crawled_at=r[14],
        summary=r[15],
        download_status=r[16],
    )
