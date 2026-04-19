from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db import repositories
from app.models.agent_schemas import ActionPlan, AgentDecision, TicketClassification
from app.tools.failures import TransientToolError
from app.tools.tool_definitions import SupportTools

logger = logging.getLogger(__name__)

_STAGE_RANK: dict[str, int] = {
    "pending": 0,
    "classified": 1,
    "planned": 2,
    "executed": 3,
    "validated": 4,
    "decided": 5,
}


class AgentState(TypedDict, total=False):
    ticket_id: str
    ticket: dict[str, Any]
    classification: dict[str, Any]
    plan: dict[str, Any]
    tool_results: dict[str, Any]
    tool_errors: list[str]
    validation_errors: list[str]
    kb_query: str
    state_validation: dict[str, Any]
    decision: dict[str, Any]
    final_status: str


def _extract_order_id(text: str) -> str | None:
    match = re.search(r"\bORD-\d{4,}\b", text, flags=re.IGNORECASE)
    return match.group(0).upper() if match else None


def _fallback_classification(ticket: dict[str, Any]) -> TicketClassification:
    text = f"{ticket.get('subject', '')} {ticket.get('body', '')}".lower()

    if "cancel" in text:
        category = "cancellation"
    elif "where is my order" in text or "tracking" in text:
        category = "status_query"
    elif "policy" in text or "return process" in text:
        category = "policy_question"
    elif any(word in text for word in ["refund", "return", "broken", "defect", "damaged", "wrong"]):
        category = "refund_or_return"
    else:
        category = "ambiguous"

    urgency = "critical" if any(w in text for w in ["lawyer", "bank", "dispute", "urgent"]) else "medium"
    return TicketClassification(category=category, urgency=urgency)


def _fallback_plan(classification: TicketClassification) -> ActionPlan:
    return ActionPlan(
        steps=[
            "Identify customer and related order context.",
            "Check product policy and refund or warranty eligibility.",
            "Consult knowledge base and decide resolve, retry, or escalate.",
        ]
    )


def _fallback_decision(state: AgentState) -> AgentDecision:
    if state.get("validation_errors") or state.get("tool_errors"):
        return AgentDecision(action="retry", confidence=0.5, summary="Transient failures detected; retry required")
    return AgentDecision(action="resolve", confidence=0.78, summary="Sufficient evidence collected for resolution")


def _tracking_from_notes(notes: str | None) -> str | None:
    if not notes:
        return None
    match = re.search(r"\bTRK-[0-9A-Z]+\b", notes)
    return match.group(0) if match else None


def _build_kb_query(ticket: dict[str, Any], category: str | None) -> str:
    text = f"{ticket.get('subject', '')} {ticket.get('body', '')}".lower()
    tokens = re.findall(r"[a-zA-Z]{4,}", text)
    stop_words = {
        "please",
        "hello",
        "thanks",
        "about",
        "order",
        "number",
        "email",
        "would",
        "could",
        "month",
    }
    keywords = [token for token in tokens if token not in stop_words][:8]

    category_terms: dict[str, list[str]] = {
        "refund_or_return": ["refund", "return", "policy", "warranty", "defective"],
        "warranty_claim": ["warranty", "defect", "replacement", "claim"],
        "status_query": ["shipping", "tracking", "delivery", "transit"],
        "cancellation": ["cancel", "processing", "order"],
        "policy_question": ["return", "exchange", "policy", "window"],
    }
    joined = keywords + category_terms.get(category or "", [])
    return " ".join(dict.fromkeys(joined))[:240] or "refund return policy warranty"


class SupportResolutionAgent:
    def __init__(self, db: Session, ticket_id: str) -> None:
        self.db = db
        self.ticket_id = ticket_id
        ticket_row = repositories.get_ticket(db, ticket_id)
        if not ticket_row:
            raise ValueError(f"Ticket not found: {ticket_id}")

        self.ticket = ticket_row.raw_data
        self.runtime_cache = repositories.get_ticket_runtime_state(db, ticket_id)
        self.tools = SupportTools(db=db, ticket_id=ticket_id, ticket_payload=self.ticket)

        settings = get_settings()
        self.llm = ChatOpenAI(
            model=settings.openai_model,
            api_key=settings.openai_api_key,
            temperature=0,
        )

        self.graph = self._build_graph()

    def _is_stage_at_least(self, stage: str) -> bool:
        current_stage = repositories.get_ticket_stage(self.db, self.ticket_id)
        return _STAGE_RANK.get(current_stage, 0) >= _STAGE_RANK.get(stage, 0)

    def _persist_stage(self, stage: str, runtime_updates: dict[str, Any] | None = None) -> None:
        repositories.persist_ticket_stage(
            self.db,
            ticket_id=self.ticket_id,
            stage=stage,
            runtime_updates=runtime_updates,
        )
        if runtime_updates:
            self.runtime_cache.update(runtime_updates)

    def _audit(self, step: str, action: str, input_payload: dict[str, Any], output_payload: dict[str, Any], confidence: float) -> None:
        repositories.insert_audit_log(
            self.db,
            ticket_id=self.ticket_id,
            step=step,
            action=action,
            input_payload=input_payload,
            output_payload=output_payload,
            confidence=confidence,
        )

    def _build_graph(self):
        workflow = StateGraph(AgentState)
        workflow.add_node("classify_ticket", self.classify_ticket)
        workflow.add_node("plan_actions", self.plan_actions)
        workflow.add_node("execute_tools", self.execute_tools)
        workflow.add_node("validate_outputs", self.validate_outputs)
        workflow.add_node("decision_node", self.decision_node)

        workflow.add_edge(START, "classify_ticket")
        workflow.add_edge("classify_ticket", "plan_actions")
        workflow.add_edge("plan_actions", "execute_tools")
        workflow.add_edge("execute_tools", "validate_outputs")
        workflow.add_edge("validate_outputs", "decision_node")
        workflow.add_edge("decision_node", END)

        return workflow.compile()

    def _invoke_structured(self, schema, messages, fallback):
        try:
            return self.llm.with_structured_output(schema).invoke(messages)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Structured LLM call failed for %s: %s", schema.__name__, exc)
            return fallback

    def classify_ticket(self, state: AgentState) -> AgentState:
        if state.get("classification"):
            return state

        if self._is_stage_at_least("classified"):
            cached = self.runtime_cache.get("classification")
            if isinstance(cached, dict):
                state["classification"] = cached
                return state

        ticket = state["ticket"]
        fallback = _fallback_classification(ticket)
        classification = self._invoke_structured(
            TicketClassification,
            [
                SystemMessage(
                    content=(
                        "Classify support tickets into one concise category and urgency. "
                        "Use category values like refund_or_return, cancellation, status_query, "
                        "policy_question, warranty_claim, ambiguous, or social_engineering."
                    )
                ),
                HumanMessage(content=str(ticket)),
            ],
            fallback,
        )
        state["classification"] = classification.model_dump()

        self._audit(
            "classify_ticket",
            "ticket_classification",
            {"ticket_id": self.ticket_id},
            {
                "classification": state["classification"],
                "reasoning_summary": "Ticket classified to drive routing and execution plan.",
            },
            confidence=0.82,
        )
        self._persist_stage("classified", {"classification": state["classification"]})
        return state

    def plan_actions(self, state: AgentState) -> AgentState:
        if state.get("plan"):
            return state

        if self._is_stage_at_least("planned"):
            cached = self.runtime_cache.get("plan")
            if isinstance(cached, dict):
                state["plan"] = cached
                return state

        fallback = _fallback_plan(TicketClassification(**state["classification"]))
        plan = self._invoke_structured(
            ActionPlan,
            [
                SystemMessage(
                    content=(
                        "Generate an action plan for a support automation agent. "
                        "Return at least 3 concrete steps and include data retrieval, policy checks, "
                        "and customer communication."
                    )
                ),
                HumanMessage(content=f"ticket={state['ticket']} classification={state['classification']}"),
            ],
            fallback,
        )
        state["plan"] = plan.model_dump()

        self._audit(
            "plan_actions",
            "plan_generated",
            {"classification": state["classification"]},
            {
                "plan": state["plan"],
                "reasoning_summary": "Action sequence generated for deterministic tool execution.",
            },
            confidence=0.8,
        )
        self._persist_stage("planned", {"plan": state["plan"]})
        return state

    def execute_tools(self, state: AgentState) -> AgentState:
        if state.get("tool_results"):
            existing = state.get("tool_results", {})
            if isinstance(existing, dict) and existing:
                return state

        if self._is_stage_at_least("executed"):
            cached_tool_results = self.runtime_cache.get("tool_results")
            if isinstance(cached_tool_results, dict) and cached_tool_results:
                state["tool_results"] = cached_tool_results
                cached_errors = self.runtime_cache.get("tool_errors")
                if isinstance(cached_errors, list):
                    state["tool_errors"] = cached_errors
                cached_kb = self.runtime_cache.get("kb_query")
                if isinstance(cached_kb, str):
                    state["kb_query"] = cached_kb
                return state

        ticket = state["ticket"]
        subject_body = f"{ticket.get('subject', '')} {ticket.get('body', '')}"

        tool_results: dict[str, Any] = {}
        tool_errors: list[str] = []

        customer_res = self.tools.get_customer(ticket.get("customer_email", ""))
        tool_results["get_customer"] = customer_res.model_dump()

        order_id = _extract_order_id(subject_body)
        if not order_id and customer_res.customer:
            latest_order = repositories.get_latest_order_for_customer(self.db, customer_res.customer.get("customer_id", ""))
            if latest_order:
                order_id = latest_order.get("order_id")

        if order_id:
            order_res = self.tools.get_order(order_id)
            tool_results["get_order"] = order_res.model_dump()
            if order_res.order:
                product_res = self.tools.get_product(order_res.order.get("product_id", ""))
                tool_results["get_product"] = product_res.model_dump()

                eligibility_res = self.tools.check_refund_eligibility(order_id)
                tool_results["check_refund_eligibility"] = eligibility_res.model_dump()
        else:
            fallback_order_res = self.tools.get_order("ORD-UNKNOWN")
            tool_results["get_order"] = fallback_order_res.model_dump()
            tool_errors.append("Unable to determine order_id from ticket context")

        kb_query = _build_kb_query(ticket, state.get("classification", {}).get("category"))
        state["kb_query"] = kb_query
        kb_res = self.tools.search_knowledge_base(kb_query)
        tool_results["search_knowledge_base"] = kb_res.model_dump()

        if len(tool_results) < 3:
            tool_errors.append("Fewer than 3 tool calls completed")

        state["tool_results"] = tool_results
        state["tool_errors"] = tool_errors

        self._audit(
            "execute_tools",
            "tools_executed",
            {"plan": state.get("plan", {})},
            {
                "tool_calls": list(tool_results.keys()),
                "kb_query": kb_query,
                "tool_errors": tool_errors,
                "reasoning_summary": "Collected customer/order/product/policy context for decisioning.",
            },
            confidence=0.85,
        )
        self._persist_stage(
            "executed",
            {
                "tool_results": state["tool_results"],
                "tool_errors": state["tool_errors"],
                "kb_query": state.get("kb_query"),
            },
        )
        return state

    def validate_outputs(self, state: AgentState) -> AgentState:
        if self._is_stage_at_least("validated"):
            cached_errors = self.runtime_cache.get("validation_errors")
            if isinstance(cached_errors, list):
                state["validation_errors"] = cached_errors
                return state

        errors: list[str] = []
        tool_results = state.get("tool_results", {})

        required_keys = ["get_customer", "search_knowledge_base"]
        for key in required_keys:
            if key not in tool_results:
                errors.append(f"Missing required tool result: {key}")

        if len(tool_results) < 3:
            errors.append("Minimum 3 tool calls not achieved")

        for tool_name, payload in tool_results.items():
            if not isinstance(payload, dict):
                errors.append(f"{tool_name} output is not a dictionary")
                continue
            if "ok" not in payload:
                errors.append(f"{tool_name} output missing 'ok' field")
            if "message" not in payload:
                errors.append(f"{tool_name} output missing 'message' field")

        state["validation_errors"] = errors

        self._audit(
            "validate_outputs",
            "output_validation",
            {"tool_count": len(tool_results)},
            {
                "validation_errors": errors,
                "reasoning_summary": "Validated tool outputs for completeness and schema consistency.",
            },
            confidence=0.88,
        )
        self._persist_stage("validated", {"validation_errors": state["validation_errors"]})
        return state

    def decision_node(self, state: AgentState) -> AgentState:
        ticket = state["ticket"]
        order = state.get("tool_results", {}).get("get_order", {}).get("order")
        order_id = order.get("order_id") if order else _extract_order_id(f"{ticket.get('subject', '')} {ticket.get('body', '')}")

        # Re-fetch latest mutable state before taking side-effecting actions.
        latest_order = None
        if order_id:
            latest_order_res = self.tools.get_order(order_id)
            latest_order = latest_order_res.order
            state["tool_results"]["get_order_latest"] = latest_order_res.model_dump()

        state_validation = {
            "order_id": order_id,
            "latest_order_refund_status": (latest_order or {}).get("refund_status") if latest_order else None,
            "latest_order_status": (latest_order or {}).get("status") if latest_order else None,
        }
        state["state_validation"] = state_validation
        self._audit(
            "decision_node",
            "state_validation",
            {"ticket_id": self.ticket_id},
            {
                "state_validation": state_validation,
                "reasoning_summary": "Fetched latest order state before final decision to avoid stale actions.",
            },
            confidence=0.93,
        )

        # Ensure eligibility is aligned with latest mutable state.
        if latest_order and str(latest_order.get("refund_status") or "").lower() == "refunded":
            state.get("tool_results", {}).setdefault("check_refund_eligibility", {})
            state["tool_results"]["check_refund_eligibility"] = {
                "ok": True,
                "message": "already refunded",
                "eligible": False,
                "reason": "Refund already processed for this order",
                "warranty_claim": False,
            }

        fallback = _fallback_decision(state)
        decision = self._invoke_structured(
            AgentDecision,
            [
                SystemMessage(
                    content=(
                        "You are a support operations decision engine. Decide one action: resolve, retry, or escalate. "
                        "Keep confidence calibrated."
                    )
                ),
                HumanMessage(
                    content=(
                        f"ticket={state['ticket']}\n"
                        f"classification={state.get('classification')}\n"
                        f"tool_results={state.get('tool_results')}\n"
                        f"tool_errors={state.get('tool_errors')}\n"
                        f"validation_errors={state.get('validation_errors')}"
                    )
                ),
            ],
            fallback,
        )

        has_failures = bool(state.get("validation_errors") or state.get("tool_errors"))
        if has_failures:
            decision.action = "retry"
            decision.confidence = min(decision.confidence, 0.55)
            if "retry" not in decision.summary.lower():
                decision.summary = f"Retry required due to execution issues: {decision.summary}"

        if not has_failures and decision.confidence < 0.6:
            decision.action = "escalate"

        category = state.get("classification", {}).get("category", "ambiguous")
        order = latest_order or state.get("tool_results", {}).get("get_order", {}).get("order")
        eligibility = state.get("tool_results", {}).get("check_refund_eligibility", {})

        final_status = "processing"
        action_details: dict[str, Any] = {"decision": decision.model_dump()}

        if decision.action == "retry":
            final_status = "retry"

        elif decision.action == "escalate":
            priority = state.get("classification", {}).get("urgency", "high")
            escalation = self.tools.escalate(
                ticket_id=self.ticket_id,
                summary=decision.summary,
                priority=priority if priority in {"low", "medium", "high", "critical"} else "high",
            )
            self.tools.send_reply(
                ticket_id=self.ticket_id,
                message=(
                    "Your request needs specialist review. We have escalated this ticket and "
                    "a support specialist will contact you shortly."
                ),
            )
            final_status = "escalated"
            action_details["escalation"] = escalation.model_dump()

        else:
            response_message = ""
            if category == "cancellation" and order:
                cancel_result = self.tools.cancel_order(order["order_id"])
                action_details["cancel_order"] = cancel_result.model_dump()
                if cancel_result.cancelled:
                    response_message = f"Order {order['order_id']} has been cancelled successfully."
                else:
                    response_message = (
                        "Your order has already progressed beyond processing and cannot be auto-cancelled. "
                        "We have escalated your request."
                    )
                    escalation = self.tools.escalate(
                        ticket_id=self.ticket_id,
                        summary="Cancellation requested after processing stage.",
                        priority="high",
                    )
                    action_details["escalation"] = escalation.model_dump()
                    final_status = "escalated"

            elif category in {"refund_or_return", "warranty_claim"} and order:
                refund_already_processed = str(order.get("refund_status") or "").lower() == "refunded"
                if refund_already_processed:
                    refund_id = order.get("refund_id")
                    processed_at = order.get("refund_processed_at")
                    processed_suffix = ""
                    if processed_at:
                        processed_suffix = f" on {processed_at}"
                    if refund_id:
                        processed_suffix += f" (reference: {refund_id})"

                    decision.summary = (
                        f"Refund was already processed for order {order['order_id']}; no duplicate refund needed."
                    )
                    action_details["decision"] = decision.model_dump()
                    action_details["refund_state"] = {
                        "order_id": order["order_id"],
                        "refund_status": order.get("refund_status"),
                        "refund_id": refund_id,
                        "refund_processed_at": processed_at,
                    }
                    response_message = (
                        f"A refund has already been processed for order {order['order_id']}{processed_suffix}. "
                        "No further refund action is required."
                    )
                elif eligibility.get("eligible"):
                    refund = self.tools.issue_refund(order["order_id"], float(order.get("amount", 0.0)))
                    action_details["refund"] = refund.model_dump()
                    response_message = (
                        f"Refund for order {order['order_id']} has been initiated for ${order.get('amount')}. "
                        "Please allow 5-7 business days for bank processing."
                    )
                elif eligibility.get("warranty_claim"):
                    escalation = self.tools.escalate(
                        ticket_id=self.ticket_id,
                        summary=f"Warranty claim required: {eligibility.get('reason')}",
                        priority="high",
                    )
                    action_details["escalation"] = escalation.model_dump()
                    response_message = (
                        "Your issue appears covered under warranty. We have escalated your case "
                        "to warranty fulfillment for replacement support."
                    )
                    final_status = "escalated"
                else:
                    response_message = (
                        "We cannot approve a refund based on current policy. "
                        f"Reason: {eligibility.get('reason', 'insufficient eligibility')}. "
                        "If you want, we can offer troubleshooting or warranty review."
                    )

            elif category == "status_query" and order:
                tracking = _tracking_from_notes(order.get("notes"))
                if order.get("status") == "shipped":
                    response_message = (
                        f"Order {order['order_id']} is in transit. "
                        f"Tracking: {tracking or 'currently unavailable'}."
                    )
                else:
                    response_message = f"Order {order['order_id']} status is {order.get('status', 'unknown')}."

            elif category == "policy_question":
                kb_chunks = state.get("tool_results", {}).get("search_knowledge_base", {}).get("chunks", [])
                policy_summary = " ".join(chunk.get("content", "") for chunk in kb_chunks[:2])
                response_message = (
                    "Here is our return and exchange guidance based on the policy knowledge base: "
                    f"{policy_summary[:600]}"
                )

            else:
                response_message = (
                    "Thanks for contacting support. Could you share your order ID, the product, and "
                    "a short description of the issue so we can help immediately?"
                )

            send_result = self.tools.send_reply(ticket_id=self.ticket_id, message=response_message)
            action_details["reply"] = send_result.model_dump()
            final_status = final_status if final_status == "escalated" else "resolved"

        state["decision"] = decision.model_dump()
        state["final_status"] = final_status

        self._audit(
            "decision_node",
            "decision_and_action",
            {
                "classification": state.get("classification"),
                "validation_errors": state.get("validation_errors"),
                "tool_errors": state.get("tool_errors"),
            },
            {
                "decision": state["decision"],
                "final_status": final_status,
                "action_details": action_details,
                "reasoning_summary": "Applied confidence rules, failure policy, and executed final actions.",
            },
            confidence=decision.confidence,
        )

        persisted_stage = "decided" if final_status != "retry" else "validated"
        self._persist_stage(
            persisted_stage,
            {
                "state_validation": state.get("state_validation"),
                "decision": state["decision"],
                "final_status": state["final_status"],
            },
        )

        return state

    def run(self) -> AgentState:
        cached_classification = self.runtime_cache.get("classification")
        cached_plan = self.runtime_cache.get("plan")
        cached_tool_results = self.runtime_cache.get("tool_results")
        cached_tool_errors = self.runtime_cache.get("tool_errors")
        cached_validation_errors = self.runtime_cache.get("validation_errors")
        cached_kb_query = self.runtime_cache.get("kb_query")

        initial_state: AgentState = {
            "ticket_id": self.ticket_id,
            "ticket": self.ticket,
            "tool_results": cached_tool_results if isinstance(cached_tool_results, dict) else {},
            "tool_errors": cached_tool_errors if isinstance(cached_tool_errors, list) else [],
            "validation_errors": cached_validation_errors if isinstance(cached_validation_errors, list) else [],
            "kb_query": cached_kb_query if isinstance(cached_kb_query, str) else "",
        }
        if isinstance(cached_classification, dict):
            initial_state["classification"] = cached_classification
        if isinstance(cached_plan, dict):
            initial_state["plan"] = cached_plan
        return self.graph.invoke(initial_state)


def run_support_resolution_agent(db: Session, ticket_id: str) -> AgentState:
    agent = SupportResolutionAgent(db=db, ticket_id=ticket_id)
    return agent.run()
