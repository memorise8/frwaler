# -*- coding: utf-8 -*-
"""Product browsing and search API."""

import json
import os
from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import HTMLResponse
import aiosqlite
from ..database import get_db
from ..schemas import ProductOut, ProductListOut

router = APIRouter(prefix="/api/products", tags=["products"])


def _row_to_product(row) -> dict:
    """Convert a DB row to ProductOut-compatible dict."""
    d = dict(row)
    # Parse JSON fields
    if d.get("image_urls"):
        try:
            d["image_urls"] = json.loads(d["image_urls"])
        except (json.JSONDecodeError, TypeError):
            d["image_urls"] = []
    else:
        d["image_urls"] = []
    if d.get("specs"):
        try:
            d["specs"] = json.loads(d["specs"])
        except (json.JSONDecodeError, TypeError):
            d["specs"] = None
    return d


@router.get("", response_model=ProductListOut)
async def list_products(
    q: str = Query(None, description="Search query"),
    site_id: str = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: aiosqlite.Connection = Depends(get_db),
):
    """List products with optional search and filtering."""
    offset = (page - 1) * limit

    where_clauses = []
    params = []

    if q:
        where_clauses.append("(name LIKE ? OR brand LIKE ? OR category LIKE ? OR description LIKE ?)")
        params.extend([f"%{q}%"] * 4)
    if site_id:
        where_clauses.append("site_id = ?")
        params.append(site_id)

    where_sql = " WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    count_cursor = await db.execute(f"SELECT COUNT(*) as cnt FROM products{where_sql}", params)
    total = (await count_cursor.fetchone())["cnt"]

    rows_cursor = await db.execute(
        f"SELECT * FROM products{where_sql} ORDER BY crawled_at DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    )
    items = [_row_to_product(row) for row in await rows_cursor.fetchall()]

    return ProductListOut(items=items, total=total, page=page, limit=limit)


@router.get("/stats")
async def product_stats(db: aiosqlite.Connection = Depends(get_db)):
    """Get product crawling statistics."""
    rows_cursor = await db.execute("""
        SELECT site_id, COUNT(*) as total,
               SUM(CASE WHEN html_path IS NOT NULL AND html_path != '' THEN 1 ELSE 0 END) as html_saved
        FROM products GROUP BY site_id ORDER BY site_id
    """)
    sites = [dict(row) for row in await rows_cursor.fetchall()]

    total_cursor = await db.execute("SELECT COUNT(*) as cnt FROM products")
    total = (await total_cursor.fetchone())["cnt"]

    return {"total_products": total, "sites": sites}


@router.get("/categories")
async def product_categories(
    site_id: str = Query(None),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Get distinct product categories with counts."""
    where_sql = " WHERE site_id = ?" if site_id else ""
    params = [site_id] if site_id else []
    cursor = await db.execute(
        f"SELECT category, COUNT(*) as count FROM products{where_sql} GROUP BY category ORDER BY count DESC",
        params,
    )
    rows = [dict(row) for row in await cursor.fetchall()]
    return {"categories": rows, "total": len(rows)}


@router.get("/{product_id}", response_model=ProductOut)
async def get_product(product_id: str, db: aiosqlite.Connection = Depends(get_db)):
    """Get a single product by ID."""
    cursor = await db.execute("SELECT * FROM products WHERE id = ?", (product_id,))
    product = await cursor.fetchone()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return _row_to_product(product)


@router.get("/{product_id}/html", response_class=HTMLResponse)
async def get_product_html(product_id: str, db: aiosqlite.Connection = Depends(get_db)):
    """Get the archived HTML of a product page."""
    cursor = await db.execute("SELECT html_path FROM products WHERE id = ?", (product_id,))
    product = await cursor.fetchone()
    if not product or not product["html_path"]:
        raise HTTPException(status_code=404, detail="HTML not found")

    html_path = product["html_path"]
    if not os.path.exists(html_path):
        raise HTTPException(status_code=404, detail="HTML file not found on disk")

    with open(html_path, "r", encoding="utf-8") as f:
        return f.read()
