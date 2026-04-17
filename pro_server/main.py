import os
import sqlite3

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .settings import pro_settings
from .auth import init_license_db
from .routers import auto_add, summarize, analyze, license, screening, users

app = FastAPI(title="Crawler PRO API", version="1.0.0")

# CORS: parse comma-separated allowed_origins; empty = allow all (dev).
_origins = [o.strip() for o in pro_settings.allowed_origins.split(",") if o.strip()]
_origin_regex = r"https://.*\.vercel\.app" if pro_settings.allow_vercel_preview else None

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["*"],
    allow_origin_regex=_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    os.makedirs(os.path.dirname(pro_settings.license_db_path), exist_ok=True)
    init_license_db(pro_settings.license_db_path)

    from .migrations import run_migrations, seed_if_empty
    os.makedirs(os.path.dirname(pro_settings.screening_db_path), exist_ok=True)
    run_migrations(pro_settings.screening_db_path)
    seed_if_empty(
        pro_settings.screening_db_path,
        pro_settings.factors_seed_path,
        pro_settings.heritage_seed_path,
        mosfet_factors_json_path=pro_settings.mosfet_factors_seed_path,
        mosfet_heritage_json_path=pro_settings.mosfet_heritage_seed_path,
    )


app.include_router(auto_add.router)
app.include_router(summarize.router)
app.include_router(analyze.router)
app.include_router(license.router)
app.include_router(screening.router)
app.include_router(users.router)


@app.get("/pro/api/health")
async def health():
    """Liveness probe. Reports: server status, DB reachability, LLM provider readiness."""
    checks: dict[str, str] = {}

    for label, path in (
        ("license_db", pro_settings.license_db_path),
        ("screening_db", pro_settings.screening_db_path),
    ):
        try:
            conn = sqlite3.connect(path)
            conn.execute("SELECT 1").fetchone()
            conn.close()
            checks[label] = "ok"
        except Exception as exc:
            checks[label] = f"error: {type(exc).__name__}"

    providers = []
    if pro_settings.gemini_api_key:
        providers.append("gemini")
    if pro_settings.openai_api_key:
        providers.append("openai")
    checks["llm_providers"] = ",".join(providers) if providers else "none"

    overall = "ok" if all(
        v == "ok" for k, v in checks.items() if k in ("license_db", "screening_db")
    ) else "degraded"

    return {"status": overall, "mode": "pro", "checks": checks}
