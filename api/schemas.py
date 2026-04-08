from pydantic import BaseModel
from typing import Optional
import json


class SiteOut(BaseModel):
    id: str
    name: str
    base_url: str
    last_crawled: Optional[str] = None
    paper_count: int = 0
    country: str = ""
    crawl_status: str = "not_tested"
    crawl_status_reason: str | None = None


class PaperOut(BaseModel):
    id: str
    site_id: str
    external_id: Optional[str] = None
    title: str
    authors: list[str] = []
    abstract: Optional[str] = None
    category: Optional[str] = None
    keywords: list[str] = []
    published_date: Optional[str] = None
    url: Optional[str] = None
    pdf_url: Optional[str] = None
    doi: Optional[str] = None
    department: Optional[str] = None
    summary: Optional[str] = None
    download_status: Optional[str] = None
    crawled_at: Optional[str] = None


class PaperListOut(BaseModel):
    items: list[PaperOut]
    total: int
    page: int
    limit: int


class StatsOut(BaseModel):
    total_sites: int
    total_papers: int
    sites: list[dict]


class JobOut(BaseModel):
    id: str
    type: str  # crawl, download, convert, summarize
    site_id: str
    status: str  # pending, running, completed, failed
    progress: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    result: Optional[str] = None
    diagnosis: Optional[list] = None


class CrawlRequest(BaseModel):
    limit: Optional[int] = None


class AutoAddRequest(BaseModel):
    url: str
    site_id: Optional[str] = None
    site_name: Optional[str] = None
    browser: bool = False
