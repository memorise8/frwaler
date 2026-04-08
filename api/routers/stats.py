from fastapi import APIRouter, Depends
import aiosqlite
from ..database import get_db
from ..schemas import StatsOut

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("", response_model=StatsOut)
async def get_stats(db: aiosqlite.Connection = Depends(get_db)):
    # Total sites
    cursor = await db.execute("SELECT COUNT(*) FROM sites")
    total_sites = (await cursor.fetchone())[0]

    # Total papers
    cursor = await db.execute("SELECT COUNT(*) FROM papers")
    total_papers = (await cursor.fetchone())[0]

    # Per-site stats
    cursor = await db.execute("""
        SELECT s.id, s.name, COUNT(p.id) as paper_count, s.last_crawled,
               SUM(CASE WHEN p.download_status = 'downloaded' THEN 1 ELSE 0 END) as downloaded,
               SUM(CASE WHEN p.summary IS NOT NULL AND p.summary != '' THEN 1 ELSE 0 END) as summarized
        FROM sites s
        LEFT JOIN papers p ON s.id = p.site_id
        GROUP BY s.id
        ORDER BY paper_count DESC
    """)
    rows = await cursor.fetchall()
    sites = [
        {
            "id": r[0],
            "name": r[1],
            "paper_count": r[2],
            "last_crawled": r[3],
            "downloaded": r[4] or 0,
            "summarized": r[5] or 0,
        }
        for r in rows
    ]

    return StatsOut(total_sites=total_sites, total_papers=total_papers, sites=sites)
