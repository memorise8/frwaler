from fastapi import APIRouter, Depends, Query, HTTPException
import aiosqlite
from ..database import get_db
from ..schemas import PaperOut, PaperListOut
from .sites import _row_to_paper

router = APIRouter(prefix="/api/papers", tags=["papers"])


@router.get("", response_model=PaperListOut)
async def search_papers(
    q: str = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: aiosqlite.Connection = Depends(get_db),
):
    offset = (page - 1) * limit

    if q:
        count_cursor = await db.execute(
            "SELECT COUNT(*) FROM papers WHERE title LIKE ? OR abstract LIKE ?",
            (f"%{q}%", f"%{q}%")
        )
        cursor = await db.execute(
            "SELECT * FROM papers WHERE title LIKE ? OR abstract LIKE ? ORDER BY crawled_at DESC LIMIT ? OFFSET ?",
            (f"%{q}%", f"%{q}%", limit, offset)
        )
    else:
        count_cursor = await db.execute("SELECT COUNT(*) FROM papers")
        cursor = await db.execute(
            "SELECT * FROM papers ORDER BY crawled_at DESC LIMIT ? OFFSET ?",
            (limit, offset)
        )

    total = (await count_cursor.fetchone())[0]
    rows = await cursor.fetchall()
    items = [_row_to_paper(r) for r in rows]

    return PaperListOut(items=items, total=total, page=page, limit=limit)


@router.get("/{paper_id}", response_model=PaperOut)
async def get_paper(paper_id: str, db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute("SELECT * FROM papers WHERE id = ?", (paper_id,))
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Paper not found")
    return _row_to_paper(row)
