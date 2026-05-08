from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import verify_license, log_usage
from ..settings import pro_settings
from ..services.chat_qa import answer_chat

router = APIRouter(prefix="/pro/api", tags=["chat"])

logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    question: str
    history: Optional[List[Dict[str, str]]] = None  # optional conversation context


class CandidateOut(BaseModel):
    source: str
    mpn: str
    manufacturer: Optional[str] = None
    device_type: Optional[str] = None
    specs: Dict[str, Any] = {}
    datasheet_url: Optional[str] = None
    score: float = 0.0
    score_breakdown: Dict[str, float] = {}
    explanation: str = ""


class ChatResponse(BaseModel):
    answer: str
    intent: Dict[str, Any]
    candidates: List[CandidateOut]
    tokens_used: int = 0


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, license_info: dict = Depends(verify_license)):
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="질문을 입력해주세요.")

    try:
        result = answer_chat(
            question=req.question,
            db_paths={
                "products": pro_settings.products_db_path,
                "screening": pro_settings.screening_db_path,
            },
            settings=pro_settings,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        logger.exception("chat runtime error")
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.exception("chat failed")
        raise HTTPException(
            status_code=500,
            detail=f"chat error: {type(e).__name__}: {e}",
        )

    log_usage(license_info["key"], "chat", result.get("tokens_used", 0))

    candidates = [CandidateOut(**c) for c in result.get("candidates", [])]
    return ChatResponse(
        answer=result.get("answer", ""),
        intent=result.get("intent", {}),
        candidates=candidates,
        tokens_used=int(result.get("tokens_used", 0) or 0),
    )
