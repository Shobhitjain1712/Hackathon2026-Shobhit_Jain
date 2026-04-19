from __future__ import annotations

import json
from typing import Any

from celery.result import AsyncResult
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.core.knowledge_base import ingest_knowledge_base_markdown
from app.db.database import get_db
from app.db.repositories import (
    AUDIT_JSON_FILE,
    count_audit_logs,
    export_audit_logs_json_snapshot,
    get_ticket,
    list_processing_tickets_with_task,
    list_action_logs,
    list_audit_logs,
    list_dead_letter_entries,
    list_tickets_for_processing,
    set_ticket_task,
    upsert_customers,
    upsert_orders,
    upsert_products,
    upsert_tickets,
)
from app.models.ingest_schemas import CustomerIngest, OrderIngest, ProductIngest, TicketIngest
from app.models.schemas import (
    ActionLogItem,
    ActionLogResponse,
    AuditLogItem,
    AuditLogResponse,
    DeadLetterItem,
    ProcessResponse,
    TicketDetailsResponse,
    TicketStatusResponse,
    UploadResponse,
)
from app.workers.tasks import process_ticket

router = APIRouter()


_ticket_adapter = TypeAdapter(list[TicketIngest])
_customer_adapter = TypeAdapter(list[CustomerIngest])
_order_adapter = TypeAdapter(list[OrderIngest])
_product_adapter = TypeAdapter(list[ProductIngest])


def _parse_json_array(content: bytes, file_name: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(content.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Invalid JSON in {file_name}: {exc}") from exc

    if not isinstance(parsed, list):
        raise HTTPException(status_code=400, detail=f"{file_name} must contain a JSON array")

    if not all(isinstance(item, dict) for item in parsed):
        raise HTTPException(status_code=400, detail=f"{file_name} must be an array of objects")

    return parsed


async def _parse_optional_json_upload(
    file: UploadFile | None,
    file_name: str,
    adapter: TypeAdapter,
) -> list[dict[str, Any]]:
    if file is None:
        return []

    content = await file.read()
    if not content:
        return []

    parsed = _parse_json_array(content, file.filename or file_name)
    try:
        return [item.model_dump(mode="json") for item in adapter.validate_python(parsed)]
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc


@router.post("/upload", response_model=UploadResponse)
async def upload_data(
    tickets: UploadFile | None = File(None),
    customers: UploadFile | None = File(None),
    orders: UploadFile | None = File(None),
    products: UploadFile | None = File(None),
    knowledge_base: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    tickets_payload = await _parse_optional_json_upload(tickets, "tickets.json", _ticket_adapter)
    customers_payload = await _parse_optional_json_upload(customers, "customers.json", _customer_adapter)
    orders_payload = await _parse_optional_json_upload(orders, "orders.json", _order_adapter)
    products_payload = await _parse_optional_json_upload(products, "products.json", _product_adapter)

    kb_content = ""
    if knowledge_base is not None:
        kb_content = (await knowledge_base.read()).decode("utf-8")

    has_any_payload = any(
        [
            bool(tickets_payload),
            bool(customers_payload),
            bool(orders_payload),
            bool(products_payload),
            bool(kb_content.strip()),
        ]
    )
    if not has_any_payload:
        raise HTTPException(status_code=400, detail="Upload at least one non-empty file.")

    tickets_loaded, tickets_skipped = upsert_tickets(db, tickets_payload)
    customers_loaded, customers_skipped = upsert_customers(db, customers_payload)
    orders_loaded, orders_skipped = upsert_orders(db, orders_payload)
    products_loaded, products_skipped = upsert_products(db, products_payload)

    kb_chunks_loaded = 0
    if kb_content.strip():
        kb_chunks_loaded = ingest_knowledge_base_markdown(db, kb_content)

    files_processed: list[str] = []
    if tickets_payload:
        files_processed.append("tickets")
    if customers_payload:
        files_processed.append("customers")
    if orders_payload:
        files_processed.append("orders")
    if products_payload:
        files_processed.append("products")
    if kb_content.strip():
        files_processed.append("knowledge_base")

    return UploadResponse(
        tickets_loaded=tickets_loaded,
        customers_loaded=customers_loaded,
        orders_loaded=orders_loaded,
        products_loaded=products_loaded,
        kb_chunks_loaded=kb_chunks_loaded,
        tickets_skipped_duplicates=tickets_skipped,
        customers_skipped_duplicates=customers_skipped,
        orders_skipped_duplicates=orders_skipped,
        products_skipped_duplicates=products_skipped,
        files_processed=files_processed,
    )


@router.post("/process", response_model=ProcessResponse)
def process_tickets(db: Session = Depends(get_db)):
    # Recover tickets that remained in processing despite task completion.
    for ticket in list_processing_tickets_with_task(db):
        if not ticket.task_id:
            continue

        task_state = (AsyncResult(ticket.task_id, app=celery_app).state or "").upper()
        if task_state in {"SUCCESS", "FAILURE", "REVOKED"}:
            set_ticket_task(db, ticket_id=ticket.id, task_id=None, status="pending")

    tickets = list_tickets_for_processing(db)
    if not tickets:
        return ProcessResponse(queued=0, tasks={})

    queued_tasks: dict[str, str] = {}

    for ticket in tickets:
        payload = ticket.raw_data
        tier = int(payload.get("tier", 1))
        queue = "priority" if tier >= 3 else "default"

        task = process_ticket.apply_async(args=[ticket.id], queue=queue)
        set_ticket_task(db, ticket_id=ticket.id, task_id=task.id)
        queued_tasks[ticket.id] = task.id

    return ProcessResponse(queued=len(queued_tasks), tasks=queued_tasks)


@router.get("/status/{ticket_id}", response_model=TicketStatusResponse)
def get_ticket_status(ticket_id: str, db: Session = Depends(get_db)):
    ticket = get_ticket(db, ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    status = ticket.status
    decision_action = None
    decision_summary = None
    decision_confidence = None

    ticket_payload = ticket.raw_data if isinstance(ticket.raw_data, dict) else {}
    runtime_payload = ticket_payload.get("agent_runtime", {}) if isinstance(ticket_payload, dict) else {}
    decision_payload = runtime_payload.get("decision", {}) if isinstance(runtime_payload, dict) else {}
    if isinstance(decision_payload, dict):
        decision_action = decision_payload.get("action")
        decision_summary = decision_payload.get("summary")
        decision_confidence = decision_payload.get("confidence")

    if ticket.task_id:
        task_result = AsyncResult(ticket.task_id, app=celery_app)
        if status in {"pending", "processing"}:
            task_state = (task_result.state or "").upper()
            if task_state in {"FAILURE", "REVOKED"}:
                status = "failed"
            elif task_state == "RETRY":
                status = "retrying"
            elif task_state == "STARTED":
                status = "processing"

    if status == "dead_letter" and not decision_summary:
        decision_summary = "Ticket moved to dead letter after retry exhaustion. Check audit logs for the final error."

    return TicketStatusResponse(
        ticket_id=ticket.id,
        status=status,
        task_id=ticket.task_id,
        retry_count=ticket.retry_count or 0,
        retry_reason=ticket.retry_reason,
        decision_action=decision_action,
        decision_summary=decision_summary,
        decision_confidence=decision_confidence,
        last_updated=ticket.updated_at,
    )


@router.get("/logs/{ticket_id}", response_model=AuditLogResponse)
def get_ticket_logs(ticket_id: str, db: Session = Depends(get_db)):
    ticket = get_ticket(db, ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    logs = list_audit_logs(db, ticket_id)
    items = [
        AuditLogItem(
            id=log.id,
            ticket_id=log.ticket_id,
            step=log.step,
            action=log.action,
            input=log.input,
            output=log.output,
            confidence=log.confidence,
            timestamp=log.timestamp,
        )
        for log in logs
    ]
    return AuditLogResponse(ticket_id=ticket_id, logs=items)


@router.get("/details/{ticket_id}", response_model=TicketDetailsResponse)
def get_ticket_details(ticket_id: str, db: Session = Depends(get_db)):
    ticket = get_ticket(db, ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    logs = list_audit_logs(db, ticket_id)
    audit_items = [
        AuditLogItem(
            id=log.id,
            ticket_id=log.ticket_id,
            step=log.step,
            action=log.action,
            input=log.input,
            output=log.output,
            confidence=log.confidence,
            timestamp=log.timestamp,
        )
        for log in logs
    ]

    if ticket.status == "dead_letter":
        entries = list_dead_letter_entries(db, ticket_id)
        dead_letters = [
            DeadLetterItem(
                id=entry.id,
                ticket_id=entry.ticket_id,
                error=entry.error,
                payload=entry.payload,
                timestamp=entry.timestamp,
            )
            for entry in entries
        ]
        return TicketDetailsResponse(
            ticket_id=ticket_id,
            source="dead_letter_queue",
            logs=audit_items,
            dead_letters=dead_letters,
        )

    return TicketDetailsResponse(
        ticket_id=ticket_id,
        source="audit_logs",
        logs=audit_items,
        dead_letters=[],
    )


@router.get("/actions/{ticket_id}", response_model=ActionLogResponse)
def get_ticket_actions(ticket_id: str, db: Session = Depends(get_db)):
    ticket = get_ticket(db, ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    actions = list_action_logs(db, ticket_id)
    items = [
        ActionLogItem(
            id=item.id,
            ticket_id=item.ticket_id,
            action_type=item.action_type,
            payload=item.payload,
            created_at=item.created_at,
        )
        for item in actions
    ]
    return ActionLogResponse(ticket_id=ticket_id, actions=items)


@router.post("/audit/export")
def export_audit_snapshot(db: Session = Depends(get_db)):
    file_path = export_audit_logs_json_snapshot(db)
    return {
        "file_path": file_path,
        "audit_logs_count": count_audit_logs(db),
    }


@router.get("/audit/export/download")
def download_audit_snapshot(db: Session = Depends(get_db)):
    # Ensure the file is refreshed before download.
    export_audit_logs_json_snapshot(db)
    if not AUDIT_JSON_FILE.exists():
        raise HTTPException(status_code=404, detail="Audit JSON file not found")

    return FileResponse(
        path=str(AUDIT_JSON_FILE),
        media_type="application/json",
        filename="audit_logs.json",
    )
