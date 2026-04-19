from collections.abc import Generator
import logging

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.db_models import Base
from app.db.repositories import (
    ensure_knowledge_base_table,
    ensure_structured_tables_schema,
    export_audit_logs_json_snapshot,
)

logger = logging.getLogger(__name__)

settings = get_settings()

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def init_database() -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        ensure_structured_tables_schema(db)
        ensure_knowledge_base_table(db)
        try:
            export_audit_logs_json_snapshot(db)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to initialize audit snapshot JSON on startup: %s", exc)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
