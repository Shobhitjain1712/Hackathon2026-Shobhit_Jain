from __future__ import annotations

import hashlib
import logging
import random
import uuid
from datetime import datetime
from typing import Any

from openai import OpenAI
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db import repositories
from app.db.db_models import Order
from app.models.tool_schemas import (
    CancelOrderInput,
    CancelOrderOutput,
    CheckRefundEligibilityInput,
    CheckRefundEligibilityOutput,
    EscalateInput,
    EscalateOutput,
    GetCustomerInput,
    GetCustomerOutput,
    GetOrderInput,
    GetOrderOutput,
    GetProductInput,
    GetProductOutput,
    IssueRefundInput,
    IssueRefundOutput,
    SearchKnowledgeBaseInput,
    SearchKnowledgeBaseOutput,
    SendReplyInput,
    SendReplyOutput,
)
from app.tools.failures import (
    ToolMalformedResponseError,
    ToolPartialDataError,
    ToolTimeoutError,
    TransientToolError,
)

logger = logging.getLogger(__name__)


class SupportTools:
    def __init__(self, db: Session, ticket_id: str, ticket_payload: dict[str, Any]) -> None:
        self.db = db
        self.ticket_id = ticket_id
        self.ticket_payload = ticket_payload
        self.settings = get_settings()
        self.openai_client = OpenAI(api_key=self.settings.openai_api_key)

    def _audit(
        self,
        step: str,
        action: str,
        input_payload: dict[str, Any],
        output_payload: dict[str, Any],
        confidence: float,
    ) -> None:
        repositories.insert_audit_log(
            self.db,
            ticket_id=self.ticket_id,
            step=step,
            action=action,
            input_payload=repositories.json_safe(input_payload),
            output_payload=repositories.json_safe(output_payload),
            confidence=confidence,
        )

    def _simulate_failure(self, tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.tool_failure_simulation:
            return payload

        roll = random.random()
        if roll < self.settings.tool_failure_rate / 3:
            raise ToolTimeoutError(f"{tool_name} timed out")
        if roll < 2 * self.settings.tool_failure_rate / 3:
            raise ToolMalformedResponseError(f"{tool_name} returned malformed payload")
        if roll < self.settings.tool_failure_rate:
            partial = dict(payload)
            partial.pop(next(iter(partial.keys())), None)
            raise ToolPartialDataError(f"{tool_name} returned partial data: {partial}")

        return payload

    def _fingerprint(self, value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _claim_action_once(self, action_type: str, fingerprint: str, payload: dict[str, Any]) -> bool:
        return repositories.record_action_fingerprint(
            self.db,
            ticket_id=self.ticket_id,
            action_type=action_type,
            fingerprint=fingerprint,
            payload=payload,
        )

    def _release_action_claim(self, action_type: str, fingerprint: str) -> None:
        repositories.remove_action_fingerprint(
            self.db,
            ticket_id=self.ticket_id,
            action_type=action_type,
            fingerprint=fingerprint,
        )

    def get_order(self, order_id: str) -> GetOrderOutput:
        request = GetOrderInput(order_id=order_id)
        raw = repositories.get_order_by_id(self.db, request.order_id)
        payload = {
            "ok": raw is not None,
            "message": "order found" if raw else "order not found",
            "order": raw,
        }
        try:
            payload = self._simulate_failure("get_order", payload)
            response = GetOrderOutput(**payload)
        except (ValidationError, ToolMalformedResponseError, ToolPartialDataError, ToolTimeoutError) as exc:
            raise TransientToolError(str(exc)) from exc

        self._audit("tool", "get_order", request.model_dump(), response.model_dump(), confidence=0.95)
        return response

    def get_customer(self, email: str) -> GetCustomerOutput:
        request = GetCustomerInput(email=email)
        raw = repositories.get_customer_by_email(self.db, request.email)
        payload = {
            "ok": raw is not None,
            "message": "customer found" if raw else "customer not found",
            "customer": raw,
        }
        try:
            payload = self._simulate_failure("get_customer", payload)
            response = GetCustomerOutput(**payload)
        except (ValidationError, ToolMalformedResponseError, ToolPartialDataError, ToolTimeoutError) as exc:
            raise TransientToolError(str(exc)) from exc

        self._audit("tool", "get_customer", request.model_dump(), response.model_dump(), confidence=0.95)
        return response

    def get_product(self, product_id: str) -> GetProductOutput:
        request = GetProductInput(product_id=product_id)
        raw = repositories.get_product_by_id(self.db, request.product_id)
        payload = {
            "ok": raw is not None,
            "message": "product found" if raw else "product not found",
            "product": raw,
        }
        try:
            payload = self._simulate_failure("get_product", payload)
            response = GetProductOutput(**payload)
        except (ValidationError, ToolMalformedResponseError, ToolPartialDataError, ToolTimeoutError) as exc:
            raise TransientToolError(str(exc)) from exc

        self._audit("tool", "get_product", request.model_dump(), response.model_dump(), confidence=0.95)
        return response

    def check_refund_eligibility(self, order_id: str) -> CheckRefundEligibilityOutput:
        request = CheckRefundEligibilityInput(order_id=order_id)
        order = repositories.get_order_by_id(self.db, request.order_id)
        if not order:
            response = CheckRefundEligibilityOutput(
                ok=False,
                message="order not found",
                eligible=False,
                reason="No matching order found",
                warranty_claim=False,
            )
            self._audit(
                "tool",
                "check_refund_eligibility",
                request.model_dump(),
                response.model_dump(),
                confidence=0.9,
            )
            return response

        product = repositories.get_product_by_id(self.db, order.get("product_id", ""))
        customer = repositories.get_customer_by_email(self.db, self.ticket_payload.get("customer_email", ""))

        body = (self.ticket_payload.get("body") or "").lower()
        has_defect_signal = any(keyword in body for keyword in ["defect", "broken", "stopped", "cracked", "damaged"])
        is_wrong_item = any(keyword in body for keyword in ["wrong", "size", "colour", "color"])

        reference_iso = self.ticket_payload.get("created_at")
        reference_date = datetime.utcnow().date()
        if reference_iso:
            try:
                reference_date = datetime.fromisoformat(reference_iso.replace("Z", "+00:00")).date()
            except ValueError:
                logger.warning("Could not parse ticket created_at: %s", reference_iso)

        if str(order.get("refund_status") or "").lower() == "refunded":
            response = CheckRefundEligibilityOutput(
                ok=True,
                message="already refunded",
                eligible=False,
                reason="Refund already processed for this order",
                warranty_claim=False,
            )
            self._audit(
                "tool",
                "check_refund_eligibility",
                request.model_dump(),
                response.model_dump(),
                confidence=0.99,
            )
            return response

        deadline = order.get("return_deadline")
        in_window = False
        if deadline:
            try:
                in_window = reference_date <= datetime.fromisoformat(deadline).date()
            except ValueError:
                in_window = False

        vip_exception = bool(customer and "standing exception" in (customer.get("notes", "").lower()))
        registered_non_returnable = bool(
            order.get("notes") and "registered online" in order.get("notes", "").lower()
        )

        warranty_claim = False
        eligible = False
        reason = ""

        if in_window:
            eligible = True
            reason = "Order is within return window"
        elif vip_exception:
            eligible = True
            reason = "VIP exception allows extended return"
        elif registered_non_returnable:
            eligible = False
            reason = "Device registered online; product is non-returnable"
        elif has_defect_signal and product and product.get("warranty_months", 0) > 0:
            eligible = False
            warranty_claim = True
            reason = "Return window expired; route as warranty claim"
        elif is_wrong_item:
            eligible = True
            reason = "Wrong item received; eligible for exchange/refund"
        else:
            eligible = False
            reason = "Return window expired"

        payload = {
            "ok": True,
            "message": "eligibility evaluated",
            "eligible": eligible,
            "reason": reason,
            "warranty_claim": warranty_claim,
        }
        try:
            payload = self._simulate_failure("check_refund_eligibility", payload)
            response = CheckRefundEligibilityOutput(**payload)
        except (ValidationError, ToolMalformedResponseError, ToolPartialDataError, ToolTimeoutError) as exc:
            raise TransientToolError(str(exc)) from exc

        self._audit(
            "tool",
            "check_refund_eligibility",
            request.model_dump(),
            response.model_dump(),
            confidence=0.92,
        )
        return response

    def issue_refund(self, order_id: str, amount: float) -> IssueRefundOutput:
        request = IssueRefundInput(order_id=order_id, amount=amount)
        action_fp = self._fingerprint(f"{request.order_id}:{request.amount:.2f}")
        if not self._claim_action_once("issue_refund", action_fp, request.model_dump()):
            order = repositories.get_order_by_id(self.db, request.order_id)
            refunded_amount = float((order or {}).get("amount", request.amount))
            response = IssueRefundOutput(
                ok=True,
                message="duplicate refund request skipped",
                refund_id=(order or {}).get("refund_id") or "existing_refund",
                amount=refunded_amount,
            )
            self._audit("tool", "issue_refund", request.model_dump(), response.model_dump(), confidence=0.99)
            return response

        try:
            locked_order = self.db.execute(
                select(Order).where(Order.id == request.order_id).with_for_update()
            ).scalar_one_or_none()
            if not locked_order:
                self._release_action_claim("issue_refund", action_fp)
                response = IssueRefundOutput(ok=False, message="order not found", refund_id=None, amount=None)
                self._audit("tool", "issue_refund", request.model_dump(), response.model_dump(), confidence=0.95)
                return response

            if locked_order.refund_status == "refunded":
                response = IssueRefundOutput(
                    ok=True,
                    message="refund already processed; skipping duplicate",
                    refund_id=(locked_order.raw_data or {}).get("refund_id") or "existing_refund",
                    amount=float((locked_order.raw_data or {}).get("amount", locked_order.amount or 0.0)),
                )
                self._audit("tool", "issue_refund", request.model_dump(), response.model_dump(), confidence=0.99)
                return response

            refund_id = f"RF-{uuid.uuid4().hex[:12].upper()}"
            payload = {
                "ok": True,
                "message": "refund issued",
                "refund_id": refund_id,
                "amount": request.amount,
            }
            payload = self._simulate_failure("issue_refund", payload)
            response = IssueRefundOutput(**payload)

            order_payload = dict(locked_order.raw_data or {})
            order_payload["refund_status"] = "refunded"
            order_payload["refund_processed_at"] = datetime.utcnow().isoformat()
            order_payload["refund_id"] = refund_id

            locked_order.refund_status = "refunded"
            locked_order.raw_data = order_payload
            self.db.commit()

            self._audit("tool", "issue_refund", request.model_dump(), response.model_dump(), confidence=0.98)
            return response
        except (ValidationError, ToolMalformedResponseError, ToolPartialDataError, ToolTimeoutError) as exc:
            self.db.rollback()
            self._release_action_claim("issue_refund", action_fp)
            raise TransientToolError(str(exc)) from exc
        except Exception:
            self.db.rollback()
            self._release_action_claim("issue_refund", action_fp)
            raise

    def send_reply(self, ticket_id: str, message: str) -> SendReplyOutput:
        request = SendReplyInput(ticket_id=ticket_id, message=message)
        channel = "email" if self.ticket_payload.get("source") == "email" else "ticket_queue"

        reply_key = self._fingerprint(f"{ticket_id}:{message.strip()}")
        if not self._claim_action_once("send_reply", reply_key, request.model_dump()):
            response = SendReplyOutput(ok=True, message="reply already sent; skipped", channel=channel)
            self._audit("tool", "send_reply", request.model_dump(), response.model_dump(), confidence=0.99)
            return response

        payload = {
            "ok": True,
            "message": "reply queued for delivery",
            "channel": channel,
        }
        try:
            payload = self._simulate_failure("send_reply", payload)
            response = SendReplyOutput(**payload)

            ticket = repositories.get_ticket(self.db, ticket_id)
            if ticket:
                data = dict(ticket.raw_data)
                history = data.get("reply_history", [])
                history.append({"timestamp": datetime.utcnow().isoformat(), "message": message})
                data["reply_history"] = history
                repositories.update_ticket_data(self.db, ticket_id, data)

            self._audit("tool", "send_reply", request.model_dump(), response.model_dump(), confidence=0.9)
            return response
        except (ValidationError, ToolMalformedResponseError, ToolPartialDataError, ToolTimeoutError) as exc:
            self._release_action_claim("send_reply", reply_key)
            raise TransientToolError(str(exc)) from exc
        except Exception:
            self._release_action_claim("send_reply", reply_key)
            raise

    def search_knowledge_base(self, query: str) -> SearchKnowledgeBaseOutput:
        request = SearchKnowledgeBaseInput(query=query)

        embedding: list[float] | None = None
        if repositories.knowledge_base_supports_vector(self.db):
            try:
                result = self.openai_client.embeddings.create(
                    model=self.settings.embedding_model,
                    input=request.query,
                )
                embedding = result.data[0].embedding
            except Exception as exc:  # noqa: BLE001
                logger.warning("Query embedding failed; fallback search only: %s", exc)

        chunks = repositories.search_knowledge_base(self.db, request.query, embedding, limit=3)

        payload = {"ok": True, "message": "knowledge chunks retrieved", "chunks": chunks}
        try:
            payload = self._simulate_failure("search_knowledge_base", payload)
            response = SearchKnowledgeBaseOutput(**payload)
        except (ValidationError, ToolMalformedResponseError, ToolPartialDataError, ToolTimeoutError) as exc:
            raise TransientToolError(str(exc)) from exc

        self._audit(
            "tool",
            "search_knowledge_base",
            request.model_dump(),
            response.model_dump(),
            confidence=0.88,
        )
        return response

    def escalate(self, ticket_id: str, summary: str, priority: str) -> EscalateOutput:
        request = EscalateInput(ticket_id=ticket_id, summary=summary, priority=priority)
        escalation_key = self._fingerprint(f"{ticket_id}:{priority}:{summary.strip()}")
        if not self._claim_action_once("escalate", escalation_key, request.model_dump()):
            response = EscalateOutput(
                ok=True,
                message="duplicate escalation skipped",
                escalation_id=f"ESC-{escalation_key[:10].upper()}",
            )
            self._audit("tool", "escalate", request.model_dump(), response.model_dump(), confidence=0.99)
            return response

        escalation_id = f"ESC-{uuid.uuid4().hex[:10].upper()}"

        payload = {
            "ok": True,
            "message": "ticket escalated",
            "escalation_id": escalation_id,
        }
        try:
            payload = self._simulate_failure("escalate", payload)
            response = EscalateOutput(**payload)
        except (ValidationError, ToolMalformedResponseError, ToolPartialDataError, ToolTimeoutError) as exc:
            self._release_action_claim("escalate", escalation_key)
            raise TransientToolError(str(exc)) from exc

        self._audit("tool", "escalate", request.model_dump(), response.model_dump(), confidence=0.93)
        return response

    def cancel_order(self, order_id: str) -> CancelOrderOutput:
        request = CancelOrderInput(order_id=order_id)
        action_fp = self._fingerprint(request.order_id)
        if not self._claim_action_once("cancel_order", action_fp, request.model_dump()):
            response = CancelOrderOutput(ok=True, message="order cancellation already processed", cancelled=True)
            self._audit("tool", "cancel_order", request.model_dump(), response.model_dump(), confidence=0.99)
            return response

        order = repositories.get_order_by_id(self.db, request.order_id)
        if not order:
            self._release_action_claim("cancel_order", action_fp)
            response = CancelOrderOutput(ok=False, message="order not found", cancelled=False)
            self._audit("tool", "cancel_order", request.model_dump(), response.model_dump(), confidence=0.95)
            return response

        if order.get("status") == "cancelled":
            response = CancelOrderOutput(ok=True, message="order already cancelled", cancelled=True)
            self._audit("tool", "cancel_order", request.model_dump(), response.model_dump(), confidence=0.99)
            return response

        if order.get("status") != "processing":
            self._release_action_claim("cancel_order", action_fp)
            response = CancelOrderOutput(
                ok=False,
                message="order cannot be cancelled at current status",
                cancelled=False,
            )
            self._audit("tool", "cancel_order", request.model_dump(), response.model_dump(), confidence=0.95)
            return response

        payload = {"ok": True, "message": "order cancelled", "cancelled": True}
        try:
            payload = self._simulate_failure("cancel_order", payload)
            response = CancelOrderOutput(**payload)
        except (ValidationError, ToolMalformedResponseError, ToolPartialDataError, ToolTimeoutError) as exc:
            self._release_action_claim("cancel_order", action_fp)
            raise TransientToolError(str(exc)) from exc

        order["status"] = "cancelled"
        order["cancelled_at"] = datetime.utcnow().isoformat()
        repositories.update_order_data(self.db, request.order_id, order)

        self._audit("tool", "cancel_order", request.model_dump(), response.model_dump(), confidence=0.98)
        return response
