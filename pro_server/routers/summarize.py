from fastapi import APIRouter, Depends
from ..auth import verify_license, log_usage
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/pro/api", tags=["summarize"])


class SummarizeRequest(BaseModel):
    text: str
    title: Optional[str] = None
    max_length: int = 200


class SummarizeResponse(BaseModel):
    summary: str
    tokens_used: int


@router.post("/summarize", response_model=SummarizeResponse)
async def summarize(req: SummarizeRequest, license_info: dict = Depends(verify_license)):
    from openai import OpenAI
    from ..settings import pro_settings

    client = OpenAI(api_key=pro_settings.openai_api_key)

    prompt = f"Summarize the following document in {req.max_length} words or less:\n\n"
    if req.title:
        prompt += f"Title: {req.title}\n\n"
    prompt += req.text[:8000]  # Limit input

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=500,
    )

    summary = response.choices[0].message.content
    tokens = response.usage.total_tokens

    log_usage(license_info["key"], "summarize", tokens)

    return SummarizeResponse(summary=summary, tokens_used=tokens)
