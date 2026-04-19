from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class UploadResponse(BaseModel):
    tickets_loaded: int
    customers_loaded: int
    orders_loaded: int
    products_loaded: int
    kb_chunks_loaded: int
    tickets_skipped_duplicates: int = 0
    customers_skipped_duplicates: int = 0
    orders_skipped_duplicates: int = 0
    products_skipped_duplicates: int = 0
    files_processed: list[str] = Field(default_factory=list)


class ProcessResponse(BaseModel):
    queued: int
    tasks: dict[str, str]


class TicketStatusResponse(BaseModel):
    ticket_id: str
    status: str
    task_id: str | None = None
    retry_count: int = 0
    retry_reason: str | None = None
    decision_action: str | None = None
    decision_summary: str | None = None
    decision_confidence: float | None = None
    last_updated: datetime | None = None


class AuditLogItem(BaseModel):
    id: int
    ticket_id: str
    step: str
    action: str
    input: dict[str, Any]
    output: dict[str, Any]
    confidence: float
    timestamp: datetime


class AuditLogResponse(BaseModel):
    ticket_id: str
    logs: list[AuditLogItem] = Field(default_factory=list)


class DeadLetterItem(BaseModel):
    id: int
    ticket_id: str
    error: str
    payload: dict[str, Any]
    timestamp: datetime


class TicketDetailsResponse(BaseModel):
    ticket_id: str
    source: str
    logs: list[AuditLogItem] = Field(default_factory=list)
    dead_letters: list[DeadLetterItem] = Field(default_factory=list)


class ActionLogItem(BaseModel):
    id: int
    ticket_id: str
    action_type: str
    payload: dict[str, Any]
    created_at: datetime


class ActionLogResponse(BaseModel):
    ticket_id: str
    actions: list[ActionLogItem] = Field(default_factory=list)
