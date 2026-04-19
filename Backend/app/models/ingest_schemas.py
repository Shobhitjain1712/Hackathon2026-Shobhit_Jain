from datetime import date, datetime

from pydantic import BaseModel, EmailStr, Field


class AddressIngest(BaseModel):
    street: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None


class CustomerIngest(BaseModel):
    customer_id: str
    name: str
    tier: str
    email: EmailStr
    notes: str | None = None
    phone: str | None = None
    address: AddressIngest | None = None
    total_spent: float = 0
    member_since: date | None = None
    total_orders: int = 0


class OrderIngest(BaseModel):
    order_id: str
    customer_id: str
    product_id: str
    quantity: int = Field(ge=1)
    amount: float = Field(ge=0)
    status: str
    order_date: date | None = None
    delivery_date: date | None = None
    return_deadline: date | None = None
    refund_status: str | None = None
    notes: str | None = None


class ProductIngest(BaseModel):
    product_id: str
    name: str
    category: str
    price: float = Field(ge=0)
    warranty_months: int = Field(ge=0)
    return_window_days: int = Field(ge=0)
    returnable: bool
    notes: str | None = None


class TicketIngest(BaseModel):
    ticket_id: str
    customer_email: EmailStr
    subject: str
    body: str
    source: str
    created_at: datetime
    tier: int = Field(ge=1)
    expected_action: str | None = None
