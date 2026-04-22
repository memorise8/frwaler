from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .settings import settings
from .routers import auto_add

# TODO: removed, was in pro branch
# from .routers import sites, papers, stats, crawl, jobs, pro, url_test
# from .routers import site_status, config_editor, crawl_preview, smart_find, reports

app = FastAPI(title="Crawler API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auto_add.router)


@app.get("/api/health")
async def health():
    return {"status": "ok"}
