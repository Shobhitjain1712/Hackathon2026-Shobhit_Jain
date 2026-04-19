from __future__ import annotations

import logging

from celery.utils.log import get_task_logger
from redis import Redis
from sqlalchemy.exc import OperationalError

from app.agents.langgraph_agent import run_support_resolution_agent
from app.celery_app import celery_app
from app.core.config import get_settings
from app.db.database import SessionLocal
from app.db.repositories import (
    get_ticket,
    has_audit_event,
    insert_audit_log,
    maybe_write_audit_snapshot_if_all_processed,
    set_ticket_pending_for_retry,
    set_ticket_retry_reason,
    set_ticket_status,
    try_claim_ticket_processing,
)
from app.tools.failures import TransientToolError
from app.workers.base_task import DatabaseAwareTask

logger = get_task_logger(__name__)
app_logger = logging.getLogger(__name__)
settings = get_settings()
redis_client = Redis.from_url(settings.redis_url, decode_responses=True)

LOCK_TTL_SECONDS = settings.ticket_lock_ttl_seconds
TERMINAL_STATUSES = {"resolved", "escalated", "dead_letter"}


def _lock_key(ticket_id: str) -> str:
    return f"ticket:{ticket_id}:lock"


def _acquire_ticket_lock(ticket_id: str, owner: str) -> bool:
    try:
        return bool(redis_client.set(_lock_key(ticket_id), owner, ex=LOCK_TTL_SECONDS, nx=True))
    except Exception as exc:  # noqa: BLE001
        app_logger.warning("Redis lock unavailable, proceeding with DB-only idempotency checks: %s", exc)
        return True


def _release_ticket_lock(ticket_id: str, owner: str) -> None:
    try:
        key = _lock_key(ticket_id)
        current_owner = redis_client.get(key)
        if current_owner == owner:
            redis_client.delete(key)
    except Exception as exc:  # noqa: BLE001
        app_logger.warning("Failed to release Redis lock for %s: %s", ticket_id, exc)


@celery_app.task(
    bind=True,
    base=DatabaseAwareTask,
    name="app.workers.tasks.process_ticket",
    autoretry_for=(TransientToolError, OperationalError, TimeoutError),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": 3},
    rate_limit="50/m",
)
def process_ticket(self, ticket_id: str) -> dict:  # noqa: ANN201
    task_id = str(self.request.id)
    idempotency_key = ticket_id
    if not _acquire_ticket_lock(ticket_id, idempotency_key):
        app_logger.info("Skipping duplicate execution for ticket=%s, lock already held", ticket_id)
        return {"ticket_id": ticket_id, "status": "skipped_locked", "decision": None}

    try:
        with SessionLocal() as db:
            ticket = get_ticket(db, ticket_id)
            if not ticket:
                return {"ticket_id": ticket_id, "status": "missing", "decision": None}

            if ticket.status in TERMINAL_STATUSES:
                app_logger.info("Skipping terminal ticket=%s status=%s", ticket_id, ticket.status)
                return {"ticket_id": ticket_id, "status": ticket.status, "decision": None}

            if not try_claim_ticket_processing(db, ticket_id=ticket_id, task_id=task_id):
                refreshed = get_ticket(db, ticket_id)
                is_retry_resume = bool(
                    refreshed
                    and int(getattr(self.request, "retries", 0) or 0) > 0
                    and refreshed.status == "processing"
                    and refreshed.task_id == task_id
                )
                if not is_retry_resume:
                    app_logger.info("Skipping ticket=%s because atomic processing claim failed", ticket_id)
                    return {"ticket_id": ticket_id, "status": "skipped_not_pending", "decision": None}

                app_logger.info("Resuming retry attempt for ticket=%s with existing processing claim", ticket_id)

            if not has_audit_event(
                db,
                ticket_id=ticket_id,
                step="celery_worker",
                action="task_started",
                input_key="idempotency_key",
                input_value=idempotency_key,
            ):
                insert_audit_log(
                    db,
                    ticket_id=ticket_id,
                    step="celery_worker",
                    action="task_started",
                    input_payload={"idempotency_key": idempotency_key, "task_id": task_id},
                    output_payload={"status": "processing"},
                    confidence=1.0,
                )

            try:
                final_state = run_support_resolution_agent(db, ticket_id=ticket_id)
            except (TransientToolError, OperationalError, TimeoutError) as exc:
                retry_error = str(exc)
                if not retry_error:
                    retry_error = "Transient execution error"

                current_retries = int(getattr(self.request, "retries", 0) or 0)
                max_retries = int(getattr(self, "max_retries", 0) or 0)
                can_retry = current_retries < max_retries

                if can_retry:
                    # Move back to pending so Celery retry can reclaim with the same task id.
                    set_ticket_pending_for_retry(
                        db,
                        ticket_id=ticket_id,
                        task_id=task_id,
                        reason=retry_error,
                    )
                else:
                    set_ticket_retry_reason(db, ticket_id=ticket_id, reason=retry_error)
                raise
            final_status = final_state.get("final_status", "resolved")

            if final_status == "retry":
                decision_payload = final_state.get("decision") or {}
                retry_reason = str(decision_payload.get("summary") or "Agent requested retry")

                current_retries = int(getattr(self.request, "retries", 0) or 0)
                max_retries = int(getattr(self, "max_retries", 0) or 0)
                can_retry = current_retries < max_retries

                if can_retry:
                    set_ticket_pending_for_retry(
                        db,
                        ticket_id=ticket_id,
                        task_id=task_id,
                        reason=retry_reason,
                    )
                else:
                    set_ticket_retry_reason(db, ticket_id=ticket_id, reason=retry_reason)
                raise TransientToolError("Agent requested retry based on execution failures")

            set_ticket_status(db, ticket_id=ticket_id, status=final_status)

            if not has_audit_event(
                db,
                ticket_id=ticket_id,
                step="celery_worker",
                action="task_completed",
                input_key="idempotency_key",
                input_value=idempotency_key,
            ):
                insert_audit_log(
                    db,
                    ticket_id=ticket_id,
                    step="celery_worker",
                    action="task_completed",
                    input_payload={"idempotency_key": idempotency_key, "task_id": task_id},
                    output_payload={"status": final_status, "decision": final_state.get("decision")},
                    confidence=final_state.get("decision", {}).get("confidence", 0.75),
                )

            try:
                if maybe_write_audit_snapshot_if_all_processed(db):
                    app_logger.info("Audit snapshot JSON refreshed after all ticket processing completed")
            except Exception as exc:  # noqa: BLE001
                app_logger.warning("Failed to write audit snapshot JSON: %s", exc)

            app_logger.info("Ticket %s completed with status=%s", ticket_id, final_status)

            return {
                "ticket_id": ticket_id,
                "status": final_status,
                "decision": final_state.get("decision"),
            }
    finally:
        _release_ticket_lock(ticket_id, idempotency_key)
