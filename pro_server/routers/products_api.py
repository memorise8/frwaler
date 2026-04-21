from __future__ import annotations
import json, sqlite3
from typing import Optional, List
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from ..auth import verify_license
from ..settings import pro_settings

router = APIRouter(prefix="/pro/api", tags=["products"])


class ProductOut(BaseModel):
    id: str
    site_id: str
    external_id: Optional[str] = None
    name: str
    brand: Optional[str] = None
    category: Optional[str] = None
    device_type: Optional[str] = None
    description: Optional[str] = None
    url: Optional[str] = None
    datasheet_url: Optional[str] = None
    availability: Optional[str] = None


class ProductListResponse(BaseModel):
    products: List[ProductOut]
    page: int
    per_page: int
    total: int


class ProductStatsResponse(BaseModel):
    total: int
    by_device_type: dict
    by_site: dict
    by_brand: dict


@router.get("/products", response_model=ProductListResponse)
async def list_products(
    device_type: Optional[str] = None,
    site_id: Optional[str] = None,
    brand: Optional[str] = None,
    q: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    license_info: dict = Depends(verify_license),
):
    conn = sqlite3.connect(pro_settings.products_db_path)
    conn.row_factory = sqlite3.Row

    where_clauses = []
    params = []
    if device_type:
        where_clauses.append("device_type = ?")
        params.append(device_type)
    if site_id:
        where_clauses.append("site_id = ?")
        params.append(site_id)
    if brand:
        where_clauses.append("brand = ?")
        params.append(brand)
    if q:
        where_clauses.append("(name LIKE ? OR external_id LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])

    where = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    total = conn.execute(f"SELECT COUNT(*) FROM products {where}", params).fetchone()[0]

    offset = (page - 1) * per_page
    rows = conn.execute(
        f"SELECT id, site_id, external_id, name, brand, category, device_type, description, url, metadata, availability FROM products {where} ORDER BY name LIMIT ? OFFSET ?",
        params + [per_page, offset],
    ).fetchall()

    products = []
    for r in rows:
        meta = json.loads(r["metadata"]) if r["metadata"] else {}
        products.append(ProductOut(
            id=r["id"],
            site_id=r["site_id"],
            external_id=r["external_id"],
            name=r["name"],
            brand=r["brand"],
            category=r["category"],
            device_type=r["device_type"],
            description=(r["description"] or "")[:200],
            url=r["url"],
            datasheet_url=meta.get("datasheet_url"),
            availability=r["availability"],
        ))
    conn.close()
    return ProductListResponse(products=products, page=page, per_page=per_page, total=total)


@router.get("/products/stats", response_model=ProductStatsResponse)
async def product_stats(license_info: dict = Depends(verify_license)):
    conn = sqlite3.connect(pro_settings.products_db_path)

    total = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]

    by_device: dict = {}
    for r in conn.execute(
        "SELECT device_type, COUNT(*) FROM products GROUP BY device_type ORDER BY COUNT(*) DESC"
    ):
        by_device[r[0] or "unknown"] = r[1]

    by_site: dict = {}
    for r in conn.execute(
        "SELECT site_id, COUNT(*) FROM products GROUP BY site_id ORDER BY COUNT(*) DESC"
    ):
        by_site[r[0]] = r[1]

    by_brand: dict = {}
    for r in conn.execute(
        "SELECT brand, COUNT(*) FROM products WHERE brand IS NOT NULL AND brand != '' GROUP BY brand ORDER BY COUNT(*) DESC LIMIT 20"
    ):
        by_brand[r[0]] = r[1]

    conn.close()
    return ProductStatsResponse(total=total, by_device_type=by_device, by_site=by_site, by_brand=by_brand)
