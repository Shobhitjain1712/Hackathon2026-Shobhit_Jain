from pydantic import BaseModel, Field


class TicketClassification(BaseModel):
    category: str = Field(description="Ticket intent category")
    urgency: str = Field(description="low | medium | high | critical")


class ActionPlan(BaseModel):
    steps: list[str] = Field(min_length=3, description="At least 3 concrete tool/action steps")


class AgentDecision(BaseModel):
    action: str = Field(description="resolve | retry | escalate")
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str
