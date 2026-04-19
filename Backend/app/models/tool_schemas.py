from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field


class ToolBaseOutput(BaseModel):
    ok: bool
    message: str


class GetOrderInput(BaseModel):
    order_id: str


class GetOrderOutput(ToolBaseOutput):
    order: dict[str, Any] | None = None


class GetCustomerInput(BaseModel):
    email: EmailStr


class GetCustomerOutput(ToolBaseOutput):
    customer: dict[str, Any] | None = None


class GetProductInput(BaseModel):
    product_id: str


class GetProductOutput(ToolBaseOutput):
    product: dict[str, Any] | None = None


class CheckRefundEligibilityInput(BaseModel):
    order_id: str


class CheckRefundEligibilityOutput(ToolBaseOutput):
    eligible: bool
    reason: str
    warranty_claim: bool = False


class IssueRefundInput(BaseModel):
    order_id: str
    amount: float = Field(gt=0)


class IssueRefundOutput(ToolBaseOutput):
    refund_id: str | None = None
    amount: float | None = None


class SendReplyInput(BaseModel):
    ticket_id: str
    message: str


class SendReplyOutput(ToolBaseOutput):
    channel: Literal["email", "ticket_queue"]


class SearchKnowledgeBaseInput(BaseModel):
    query: str


class SearchKnowledgeBaseOutput(ToolBaseOutput):
    chunks: list[dict[str, Any]]


class EscalateInput(BaseModel):
    ticket_id: str
    summary: str
    priority: Literal["low", "medium", "high", "critical"]


class EscalateOutput(ToolBaseOutput):
    escalation_id: str


class CancelOrderInput(BaseModel):
    order_id: str


class CancelOrderOutput(ToolBaseOutput):
    cancelled: bool
