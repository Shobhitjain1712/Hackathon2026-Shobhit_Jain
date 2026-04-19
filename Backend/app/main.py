from pathlib import Path

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.api.routes import router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.rate_limit import InMemoryRateLimitMiddleware
from app.db.database import init_database

configure_logging()
settings = get_settings()


def _resolve_static_dir() -> Path:
    repo_root = Path(__file__).resolve().parents[2]
    candidates: list[Path] = []

    if settings.frontend_static_dir:
        candidates.append(Path(settings.frontend_static_dir))

    candidates.append(repo_root / "Frontend")
    candidates.append(Path(__file__).resolve().parent / "static")

    for candidate in candidates:
        if (candidate / "index.html").exists() and (candidate / "app.js").exists() and (candidate / "styles.css").exists():
            return candidate

    return candidates[-1]


STATIC_DIR = _resolve_static_dir()


def _serve_asset(filename: str) -> FileResponse:
    asset = STATIC_DIR / filename
    if not asset.exists():
        raise HTTPException(status_code=404, detail=f"Asset not found: {filename}")
    return FileResponse(asset)

app = FastAPI(title="Autonomous Support Resolution Agent", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(
    InMemoryRateLimitMiddleware,
    max_requests_per_minute=settings.request_rate_limit_per_minute,
)


@app.on_event("startup")
def on_startup() -> None:
    init_database()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def serve_frontend_index() -> FileResponse:
    return _serve_asset("index.html")


@app.get("/app.js", include_in_schema=False)
def serve_frontend_js() -> FileResponse:
    return _serve_asset("app.js")


@app.get("/styles.css", include_in_schema=False)
def serve_frontend_css() -> FileResponse:
    return _serve_asset("styles.css")


app.include_router(router)
