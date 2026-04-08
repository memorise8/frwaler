from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .settings import settings
from .routers import sites, papers, stats, crawl, jobs, pro, url_test, auto_add, site_status, config_editor, crawl_preview, smart_find, reports

app = FastAPI(title="Crawler API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(sites.router)
app.include_router(papers.router)
app.include_router(stats.router)
app.include_router(crawl.router)
app.include_router(jobs.router)
app.include_router(pro.router)
app.include_router(url_test.router)
app.include_router(auto_add.router)
app.include_router(site_status.router)
app.include_router(config_editor.router)
app.include_router(crawl_preview.router)
app.include_router(smart_find.router)
app.include_router(reports.router)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/config")
async def get_config():
    from .services.pro_client import pro_client
    return {
        "pro_configured": pro_client.is_configured,
    }
