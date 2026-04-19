from __future__ import annotations

import logging

from celery import Task
from celery.exceptions import Retry

from app.db.database import SessionLocal
from app.db.repositories import (
    insert_dead_letter,
    maybe_write_audit_snapshot_if_all_processed,
    set_ticket_retry_reason,
    set_ticket_status,
)

logger = logging.getLogger(__name__)


class DatabaseAwareTask(Task):
    abstract = True

    def on_failure(self, exc, task_id, args, kwargs, einfo):  # noqa: ANN001
        if isinstance(exc, Retry):
            super().on_failure(exc, task_id, args, kwargs, einfo)
            return

        current_retries = int(getattr(self.request, "retries", 0) or 0)
        max_retries = int(getattr(self, "max_retries", 0) or 0)
        if current_retries < max_retries:
            super().on_failure(exc, task_id, args, kwargs, einfo)
            return

        ticket_id = kwargs.get("ticket_id") if kwargs else None
        if not ticket_id and args:
            ticket_id = args[0]

        if ticket_id:
            with SessionLocal() as db:
                insert_dead_letter(
                    db,
                    ticket_id=ticket_id,
                    error=str(exc),
                    payload={
                        "task_id": task_id,
                        "args": list(args),
                        "kwargs": kwargs,
                    },
                )
                set_ticket_retry_reason(db, ticket_id=ticket_id, reason=str(exc))
                set_ticket_status(db, ticket_id=ticket_id, status="dead_letter")
                try:
                    maybe_write_audit_snapshot_if_all_processed(db)
                except Exception as snapshot_exc:  # noqa: BLE001
                    logger.warning("Failed to write audit snapshot JSON: %s", snapshot_exc)

        super().on_failure(exc, task_id, args, kwargs, einfo)
