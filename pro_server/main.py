from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .settings import pro_settings
from .auth import init_license_db
from .routers import auto_add, summarize, analyze, license

app = FastAPI(title="Crawler PRO API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # PRO server accepts from any client
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    import os
    os.makedirs(os.path.dirname(pro_settings.license_db_path), exist_ok=True)
    init_license_db(pro_settings.license_db_path)


app.include_router(auto_add.router)
app.include_router(summarize.router)
app.include_router(analyze.router)
app.include_router(license.router)


@app.get("/pro/api/health")
async def health():
    return {"status": "ok", "mode": "pro"}
