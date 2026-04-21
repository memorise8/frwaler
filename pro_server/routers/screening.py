from __future__ import annotations
import json, sqlite3, uuid
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from ..auth import verify_license, log_usage
from ..settings import pro_settings
from ..schemas import (ScreeningReport, BjtParameters, MosfetParameters,
                       FactorOut, FactorScore, FactorSource, HeritageMatch, RiskFlag,
                       FeedbackRequest, FeedbackResponse, FeedbackSummary, FeedbackFactorSummary)
from ..services.factor_kb import load_factors
from ..services.heritage_db import list_heritage, get_vectors, VECTOR_KEYS, MOSFET_VECTOR_KEYS
from ..services.scorer import score_report
from ..services.datasheet_parser import extract_from_pdf_bytes, extract_mosfet_from_pdf_bytes
from ..services.normalizer import normalize_bjt_params, normalize_mosfet_params
from ..services.products_lookup import find_product, fetch_datasheet_bytes

router = APIRouter(prefix="/pro/api", tags=["screening"])


def _check_global_quota():
    """Reject if total daily screening requests across all users exceed the global cap."""
    from datetime import date
    conn = sqlite3.connect(pro_settings.license_db_path)
    today = date.today().isoformat()
    count = conn.execute(
        "SELECT COUNT(*) FROM usage_log WHERE endpoint IN ('screen-bjt','screen-mosfet') AND timestamp >= ?",
        (today,),
    ).fetchone()[0]
    conn.close()
    if count >= pro_settings.max_global_requests_per_day:
        raise HTTPException(
            status_code=429,
            detail=f"일일 전체 분석 한도({pro_settings.max_global_requests_per_day}건)를 초과했습니다. 내일 다시 시도해주세요.",
        )


@router.post("/screen-bjt", response_model=ScreeningReport)
async def screen_bjt(
    file: Optional[UploadFile] = File(None),
    mpn: Optional[str] = Form(None),
    manufacturer: Optional[str] = Form(None),
    license_info: dict = Depends(verify_license),
):
    """Accept either a PDF upload or MPN (form field)."""
    _check_global_quota()
    if file is None and not mpn:
        raise HTTPException(status_code=400, detail="Provide either a PDF file or an mpn")

    tokens = 0
    extraction_confidence = 1.0
    input_source = "pdf" if file else "mpn"

    if file:
        pdf_bytes = await file.read()
        if len(pdf_bytes) < 100:
            raise HTTPException(status_code=400, detail="PDF too small / empty")
        if len(pdf_bytes) > 20 * 1024 * 1024:  # 20MB
            raise HTTPException(status_code=413, detail="PDF exceeds 20MB")
        try:
            raw_params, extraction_confidence, t = extract_from_pdf_bytes(pdf_bytes, mpn_hint=mpn)
            tokens += t
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Datasheet extraction failed")
            raise HTTPException(status_code=422, detail="Datasheet extraction failed")
        # PDF path: raw_params has LLM-returned raw datasheet labels → normalize
        normalized = normalize_bjt_params(raw_params)
    else:
        # MPN-only path: look up in heritage DB first (parameters already canonical)
        heritage = [h for h in list_heritage("bjt") if h["mpn"].lower() == mpn.lower()]
        if heritage:
            normalized = heritage[0]["parameters"]
            extraction_confidence = 1.0
        else:
            # Fallback: check the 56K crawler products DB and try its datasheet PDF
            product = find_product(pro_settings.products_db_path, mpn, "bjt")
            if product and product.get("datasheet_url"):
                try:
                    pdf_bytes = fetch_datasheet_bytes(product["datasheet_url"])
                    raw_params, extraction_confidence, t = extract_from_pdf_bytes(
                        pdf_bytes, mpn_hint=mpn
                    )
                    tokens += t
                    normalized = normalize_bjt_params(raw_params)
                    input_source = "pdf"
                except Exception:
                    import logging
                    logging.getLogger(__name__).exception("Auto-fetch datasheet failed")
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "error_code": "fetch_failed",
                            "mpn": mpn,
                            "message": "데이터시트 자동 다운로드에 실패했습니다. PDF를 직접 받아 업로드해주세요.",
                            "product_url": product.get("url"),
                            "datasheet_url": product.get("datasheet_url"),
                            "brand": product.get("brand"),
                        },
                    )
            elif product:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "error_code": "needs_upload",
                        "mpn": mpn,
                        "message": "이 부품은 자동 분석이 어렵습니다. 데이터시트 PDF를 업로드해주세요.",
                        "product_url": product.get("url"),
                        "brand": product.get("brand"),
                    },
                )
            else:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "error_code": "not_found",
                        "mpn": mpn,
                        "message": "아직 등록되지 않은 부품입니다. 데이터시트 PDF를 업로드하면 바로 분석할 수 있습니다.",
                    },
                )

    params = BjtParameters(**{k: v for k, v in normalized.items()
                               if k in BjtParameters.model_fields})

    # Load KB + heritage
    factors = load_factors("bjt")
    heritage_rows, heritage_vectors = get_vectors("bjt")

    # Score
    report_id = str(uuid.uuid4())
    report = score_report(
        params=params,
        factors=factors,
        heritage_rows=heritage_rows,
        heritage_vectors=heritage_vectors,
        vector_keys=VECTOR_KEYS,
        extraction_confidence=extraction_confidence,
        input_mpn=mpn,
        input_source=input_source,
        report_id=report_id,
        tokens_used=tokens,
    )

    # Persist
    _save_report(report, license_info["key"])

    # Usage log
    log_usage(license_info["key"], "screen-bjt", tokens)

    return report


@router.post("/screen-mosfet", response_model=ScreeningReport)
async def screen_mosfet(
    file: Optional[UploadFile] = File(None),
    mpn: Optional[str] = Form(None),
    manufacturer: Optional[str] = Form(None),
    license_info: dict = Depends(verify_license),
):
    """Accept either a PDF upload or MPN (form field) for MOSFET screening."""
    _check_global_quota()
    if file is None and not mpn:
        raise HTTPException(status_code=400, detail="Provide either a PDF file or an mpn")

    tokens = 0
    extraction_confidence = 1.0
    input_source = "pdf" if file else "mpn"

    if file:
        pdf_bytes = await file.read()
        if len(pdf_bytes) < 100:
            raise HTTPException(status_code=400, detail="PDF too small / empty")
        if len(pdf_bytes) > 20 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="PDF exceeds 20MB")
        try:
            raw_params, extraction_confidence, t = extract_mosfet_from_pdf_bytes(pdf_bytes, mpn_hint=mpn)
            tokens += t
        except Exception:
            import logging
            logging.getLogger(__name__).exception("MOSFET datasheet extraction failed")
            raise HTTPException(status_code=422, detail="Datasheet extraction failed")
        normalized = normalize_mosfet_params(raw_params)
    else:
        heritage = [h for h in list_heritage("mosfet") if h["mpn"].lower() == mpn.lower()]
        if heritage:
            normalized = heritage[0]["parameters"]
            extraction_confidence = 1.0
        else:
            product = find_product(pro_settings.products_db_path, mpn, "mosfet")
            if product and product.get("datasheet_url"):
                try:
                    pdf_bytes = fetch_datasheet_bytes(product["datasheet_url"])
                    raw_params, extraction_confidence, t = extract_mosfet_from_pdf_bytes(
                        pdf_bytes, mpn_hint=mpn
                    )
                    tokens += t
                    normalized = normalize_mosfet_params(raw_params)
                    input_source = "pdf"
                except Exception:
                    import logging
                    logging.getLogger(__name__).exception("Auto-fetch datasheet failed")
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "error_code": "fetch_failed",
                            "mpn": mpn,
                            "message": "데이터시트 자동 다운로드에 실패했습니다. PDF를 직접 받아 업로드해주세요.",
                            "product_url": product.get("url"),
                            "datasheet_url": product.get("datasheet_url"),
                            "brand": product.get("brand"),
                        },
                    )
            elif product:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "error_code": "needs_upload",
                        "mpn": mpn,
                        "message": "이 부품은 자동 분석이 어렵습니다. 데이터시트 PDF를 업로드해주세요.",
                        "product_url": product.get("url"),
                        "brand": product.get("brand"),
                    },
                )
            else:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "error_code": "not_found",
                        "mpn": mpn,
                        "message": "아직 등록되지 않은 부품입니다. 데이터시트 PDF를 업로드하면 바로 분석할 수 있습니다.",
                    },
                )

    params = MosfetParameters(**{k: v for k, v in normalized.items()
                                  if k in MosfetParameters.model_fields})

    factors = load_factors("mosfet")
    heritage_rows, heritage_vectors = get_vectors("mosfet", MOSFET_VECTOR_KEYS)

    report_id = str(uuid.uuid4())
    report = score_report(
        params=params,
        factors=factors,
        heritage_rows=heritage_rows,
        heritage_vectors=heritage_vectors,
        vector_keys=MOSFET_VECTOR_KEYS,
        extraction_confidence=extraction_confidence,
        input_mpn=mpn,
        input_source=input_source,
        report_id=report_id,
        tokens_used=tokens,
    )

    _save_report(report, license_info["key"])
    log_usage(license_info["key"], "screen-mosfet", tokens)

    return report


@router.get("/screen-bjt/{report_id}", response_model=ScreeningReport)
async def get_screening(report_id: str, license_info: dict = Depends(verify_license)):
    conn = sqlite3.connect(pro_settings.screening_db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM screening_results WHERE id=? AND license_key=?",
        (report_id, license_info["key"]),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Report not found")
    return _row_to_report(row)


@router.get("/factors", response_model=List[FactorOut])
async def list_bjt_factors(part_type: str = "bjt", license_info: dict = Depends(verify_license)):
    return load_factors(part_type)


@router.post("/feedback", response_model=FeedbackResponse)
async def submit_feedback(req: FeedbackRequest, license_info: dict = Depends(verify_license)):
    if req.rating not in ("up", "down"):
        raise HTTPException(status_code=400, detail="rating must be 'up' or 'down'")
    conn = sqlite3.connect(pro_settings.screening_db_path)
    # Validate report exists
    row = conn.execute(
        "SELECT id FROM screening_results WHERE id=?", (req.report_id,)
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Report not found")
    # INSERT OR REPLACE for one-feedback-per-(report, factor, license)
    # SQLite UNIQUE(report_id, factor_name, license_key) — need to handle NULL factor_name
    # Use a sentinel for NULL since NULL != NULL in UNIQUE constraints
    cursor = conn.execute(
        """INSERT OR REPLACE INTO screening_feedback
               (report_id, factor_name, rating, comment, license_key)
           VALUES (?, ?, ?, ?, ?)""",
        (req.report_id, req.factor_name, req.rating, req.comment, license_info["key"]),
    )
    feedback_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return FeedbackResponse(id=feedback_id, message="Feedback recorded")


@router.get("/feedback/{report_id}", response_model=FeedbackSummary)
async def get_feedback(report_id: str, license_info: dict = Depends(verify_license)):
    conn = sqlite3.connect(pro_settings.screening_db_path)
    rows = conn.execute(
        "SELECT factor_name, rating FROM screening_feedback WHERE report_id=?",
        (report_id,),
    ).fetchall()
    conn.close()

    total_up = sum(1 for r in rows if r[1] == "up")
    total_down = sum(1 for r in rows if r[1] == "down")

    factor_map: dict = {}
    for factor_name, rating in rows:
        key = factor_name  # may be None for overall
        if key not in factor_map:
            factor_map[key] = {"up": 0, "down": 0}
        factor_map[key][rating] += 1

    per_factor = [
        FeedbackFactorSummary(factor_name=k, up=v["up"], down=v["down"])
        for k, v in factor_map.items()
    ]
    return FeedbackSummary(
        report_id=report_id,
        total_up=total_up,
        total_down=total_down,
        per_factor=per_factor,
    )


def _save_report(r: ScreeningReport, license_key: str) -> None:
    conn = sqlite3.connect(pro_settings.screening_db_path)
    conn.execute(
        """
        INSERT INTO screening_results
            (id, input_mpn, input_source, parameters, factor_scores,
             overall_score, status, confidence, heritage_matches, risk_flags,
             license_key, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        (
            r.id,
            r.input_mpn,
            r.input_source,
            json.dumps(r.parameters.model_dump()),
            json.dumps([fs.model_dump() for fs in r.factor_scores]),
            r.overall_score,
            r.status,
            r.confidence,
            json.dumps([hm.model_dump() for hm in r.heritage_matches]),
            json.dumps([rf.model_dump() for rf in r.risk_flags]),
            license_key,
        ),
    )
    conn.commit()
    conn.close()


def _row_to_report(row) -> ScreeningReport:
    raw_params = json.loads(row["parameters"])
    # Detect MOSFET vs BJT by checking for MOSFET-specific keys
    if "bvdss_v" in raw_params or "rds_on_ohm" in raw_params:
        params = MosfetParameters(**raw_params)
    else:
        params = BjtParameters(**raw_params)
    factor_scores = [FactorScore(**fs) for fs in json.loads(row["factor_scores"])]
    heritage_matches = [HeritageMatch(**hm) for hm in json.loads(row["heritage_matches"])]
    risk_flags = [RiskFlag(**rf) for rf in json.loads(row["risk_flags"])]
    return ScreeningReport(
        id=row["id"],
        input_mpn=row["input_mpn"],
        input_source=row["input_source"],
        parameters=params,
        factor_scores=factor_scores,
        overall_score=row["overall_score"],
        status=row["status"],
        confidence=row["confidence"],
        heritage_matches=heritage_matches,
        risk_flags=risk_flags,
        created_at=row["created_at"],
    )
