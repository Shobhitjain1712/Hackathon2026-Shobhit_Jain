from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.db_models import ActionLog, AuditLog, Customer, DeadLetterQueue, Order, Product, Ticket

logger = logging.getLogger(__name__)

AUDIT_JSON_DIR = Path(__file__).resolve().parents[2] / "audit_json"
AUDIT_JSON_FILE = AUDIT_JSON_DIR / "audit_logs.json"


def _vector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(f"{x:.8f}" for x in embedding) + "]"


def _embedding_is_pgvector(db: Session) -> bool:
    row = db.execute(
        text(
            """
            SELECT udt_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'knowledge_base'
              AND column_name = 'embedding'
            """
        )
    ).fetchone()
    if not row:
        return False
    return row.udt_name == "vector"


def knowledge_base_supports_vector(db: Session) -> bool:
    try:
        return _embedding_is_pgvector(db)
    except SQLAlchemyError:
        return False


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def ensure_structured_tables_schema(db: Session) -> None:
    statements = [
        "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS customer_email VARCHAR(255)",
        "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS subject TEXT",
        "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS body TEXT",
        "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS source VARCHAR(50)",
        "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS ticket_created_at TIMESTAMPTZ",
        "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS tier INTEGER",
        "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS expected_action TEXT",
        "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS processing_stage VARCHAR(32) DEFAULT 'pending'",
        "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS retry_count INTEGER DEFAULT 0",
        "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS retry_reason TEXT",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS name VARCHAR(255)",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS tier VARCHAR(50)",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS email VARCHAR(255)",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS notes TEXT",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS phone VARCHAR(100)",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS address_json JSONB",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS address_street VARCHAR(255)",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS address_city VARCHAR(100)",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS address_state VARCHAR(100)",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS address_zip VARCHAR(20)",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS total_spent DOUBLE PRECISION",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS member_since DATE",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS total_orders INTEGER",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS raw_data JSONB",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now()",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now()",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS customer_id VARCHAR(64)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS product_id VARCHAR(64)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS quantity INTEGER",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS amount DOUBLE PRECISION",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS status VARCHAR(64)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS order_date DATE",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivery_date DATE",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS return_deadline DATE",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS refund_status VARCHAR(64)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS notes TEXT",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS raw_data JSONB",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now()",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now()",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS name VARCHAR(255)",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS category VARCHAR(100)",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS price DOUBLE PRECISION",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS warranty_months INTEGER",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS return_window_days INTEGER",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS returnable BOOLEAN",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS notes TEXT",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS raw_data JSONB",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now()",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now()",
        "CREATE INDEX IF NOT EXISTS idx_customers_email ON customers(email)",
        "CREATE INDEX IF NOT EXISTS idx_orders_customer_id ON orders(customer_id)",
        "CREATE INDEX IF NOT EXISTS idx_orders_product_id ON orders(product_id)",
        "CREATE INDEX IF NOT EXISTS idx_tickets_customer_email ON tickets(customer_email)",
        """
        CREATE TABLE IF NOT EXISTS action_logs (
            id BIGSERIAL PRIMARY KEY,
            ticket_id VARCHAR(64) NOT NULL,
            action_type VARCHAR(128) NOT NULL,
            fingerprint VARCHAR(128) NOT NULL,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_action_logs_ticket_action_fp UNIQUE (ticket_id, action_type, fingerprint)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_action_logs_ticket_id ON action_logs(ticket_id)",
    ]

    for stmt in statements:
        db.execute(text(stmt))

    db.commit()

    db.execute(text("UPDATE tickets SET status = 'pending' WHERE status IN ('uploaded', 'queued', 'retry')"))
    db.execute(text("UPDATE tickets SET processing_stage = 'pending' WHERE processing_stage IS NULL"))
    db.execute(text("UPDATE tickets SET retry_count = 0 WHERE retry_count IS NULL"))
    db.commit()

    # Legacy compatibility for older schema that used a required JSONB `data` column.
    for stmt in [
        "ALTER TABLE customers ALTER COLUMN data DROP NOT NULL",
        "ALTER TABLE orders ALTER COLUMN data DROP NOT NULL",
        "ALTER TABLE products ALTER COLUMN data DROP NOT NULL",
    ]:
        try:
            db.execute(text(stmt))
            db.commit()
        except SQLAlchemyError:
            db.rollback()

    # Backfill for earlier JSONB-only schema.
    for stmt in [
        "UPDATE customers SET raw_data = data WHERE raw_data IS NULL AND data IS NOT NULL",
        "UPDATE orders SET raw_data = data WHERE raw_data IS NULL AND data IS NOT NULL",
        "UPDATE products SET raw_data = data WHERE raw_data IS NULL AND data IS NOT NULL",
    ]:
        try:
            db.execute(text(stmt))
            db.commit()
        except SQLAlchemyError:
            db.rollback()

    db.execute(text("UPDATE customers SET raw_data = '{}'::jsonb WHERE raw_data IS NULL"))
    db.execute(text("UPDATE orders SET raw_data = '{}'::jsonb WHERE raw_data IS NULL"))
    db.execute(text("UPDATE products SET raw_data = '{}'::jsonb WHERE raw_data IS NULL"))

    # Keep legacy `data` column aligned when it exists, so old constraints/queries don't fail.
    for stmt in [
        "UPDATE customers SET data = raw_data WHERE data IS NULL AND raw_data IS NOT NULL",
        "UPDATE orders SET data = raw_data WHERE data IS NULL AND raw_data IS NOT NULL",
        "UPDATE products SET data = raw_data WHERE data IS NULL AND raw_data IS NOT NULL",
    ]:
        try:
            db.execute(text(stmt))
            db.commit()
        except SQLAlchemyError:
            db.rollback()

    db.commit()


def ensure_knowledge_base_table(db: Session) -> None:
    settings = get_settings()

    pgvector_enabled = True
    try:
        db.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        pgvector_enabled = False
        logger.warning("pgvector extension unavailable, enabling fallback search mode: %s", exc)

    settings.pgvector_enabled = pgvector_enabled

    if pgvector_enabled:
        db.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS knowledge_base (
                    id BIGSERIAL PRIMARY KEY,
                    content TEXT NOT NULL,
                    embedding VECTOR(1536),
                    content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
                )
                """
            )
        )
        db.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS knowledge_base_content_tsv_idx
                ON knowledge_base USING GIN (content_tsv)
                """
            )
        )
    else:
        db.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS knowledge_base (
                    id BIGSERIAL PRIMARY KEY,
                    content TEXT NOT NULL,
                    embedding DOUBLE PRECISION[],
                    content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
                )
                """
            )
        )
        db.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS knowledge_base_content_tsv_idx
                ON knowledge_base USING GIN (content_tsv)
                """
            )
        )

    db.commit()


def clear_knowledge_base(db: Session) -> None:
    db.execute(text("DELETE FROM knowledge_base"))
    db.commit()


def insert_knowledge_chunk(db: Session, content: str, embedding: list[float] | None) -> None:
    if embedding is not None and _embedding_is_pgvector(db):
        db.execute(
            text("INSERT INTO knowledge_base (content, embedding) VALUES (:content, :embedding)"),
            {"content": content, "embedding": _vector_literal(embedding)},
        )
    else:
        db.execute(
            text("INSERT INTO knowledge_base (content, embedding) VALUES (:content, :embedding)"),
            {"content": content, "embedding": embedding},
        )

    db.commit()


def search_knowledge_base(
    db: Session,
    query: str,
    query_embedding: list[float] | None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    if query_embedding is not None and _embedding_is_pgvector(db):
        try:
            rows = db.execute(
                text(
                    """
                    SELECT id, content
                    FROM knowledge_base
                    ORDER BY embedding <-> :query_embedding
                    LIMIT :limit
                    """
                ),
                {"query_embedding": _vector_literal(query_embedding), "limit": limit},
            ).fetchall()

            return [{"id": row.id, "content": row.content} for row in rows]
        except SQLAlchemyError as exc:
            logger.warning("Vector similarity search failed, using fallback search: %s", exc)

    rows = db.execute(
        text(
            """
            SELECT id, content
            FROM knowledge_base
            WHERE content_tsv @@ plainto_tsquery('english', :query)
            ORDER BY ts_rank(content_tsv, plainto_tsquery('english', :query)) DESC
            LIMIT :limit
            """
        ),
        {"query": query, "limit": limit},
    ).fetchall()

    if rows:
        return [{"id": row.id, "content": row.content} for row in rows]

    keywords = [
        token
        for token in re.findall(r"[a-zA-Z]{4,}", query.lower())
        if token not in {"with", "that", "this", "have", "from", "your", "about", "please", "order"}
    ][:8]

    fallback_rows = []
    if keywords:
        where_sql = " OR ".join([f"content ILIKE :kw{i}" for i in range(len(keywords))])
        params: dict[str, Any] = {"limit": limit}
        for i, keyword in enumerate(keywords):
            params[f"kw{i}"] = f"%{keyword}%"

        fallback_rows = db.execute(
            text(
                f"""
                SELECT id, content
                FROM knowledge_base
                WHERE {where_sql}
                LIMIT :limit
                """
            ),
            params,
        ).fetchall()

    if not fallback_rows:
        fallback_rows = db.execute(
            text(
                """
                SELECT id, content
                FROM knowledge_base
                WHERE content ILIKE :query
                LIMIT :limit
                """
            ),
            {"query": f"%{query}%", "limit": limit},
        ).fetchall()

    return [{"id": row.id, "content": row.content} for row in fallback_rows]


def _filter_unchanged_records(
    db: Session,
    model,
    records: list[dict[str, Any]],
    source_id_key: str,
) -> tuple[list[dict[str, Any]], int]:
    if not records:
        return [], 0

    latest_by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        record_id = str(record.get(source_id_key, "")).strip()
        if not record_id:
            continue
        latest_by_id[record_id] = record

    if not latest_by_id:
        return [], 0

    existing_rows = db.execute(
        select(model.id, model.raw_data).where(model.id.in_(list(latest_by_id.keys())))
    ).all()
    existing_raw = {str(row.id): row.raw_data for row in existing_rows}

    filtered: list[dict[str, Any]] = []
    skipped = 0
    for record_id, record in latest_by_id.items():
        if existing_raw.get(record_id) == record:
            skipped += 1
            continue
        filtered.append(record)

    return filtered, skipped


def upsert_tickets(db: Session, tickets: list[dict[str, Any]]) -> tuple[int, int]:
    if not tickets:
        return 0, 0

    ticket_records, duplicates_skipped = _filter_unchanged_records(db, Ticket, tickets, "ticket_id")
    if not ticket_records:
        return 0, duplicates_skipped

    values = []
    for ticket in ticket_records:
        values.append(
            {
                "id": ticket["ticket_id"],
                "customer_email": ticket.get("customer_email"),
                "subject": ticket.get("subject"),
                "body": ticket.get("body"),
                "source": ticket.get("source"),
                "ticket_created_at": _parse_datetime(ticket.get("created_at")),
                "tier": int(ticket["tier"]) if ticket.get("tier") is not None else None,
                "expected_action": ticket.get("expected_action"),
                "raw_data": ticket,
                "status": "pending",
                "processing_stage": "pending",
                "retry_count": 0,
                "retry_reason": None,
                "task_id": None,
            }
        )

    stmt = insert(Ticket).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Ticket.id],
        set_={
            "customer_email": stmt.excluded.customer_email,
            "subject": stmt.excluded.subject,
            "body": stmt.excluded.body,
            "source": stmt.excluded.source,
            "ticket_created_at": stmt.excluded.ticket_created_at,
            "tier": stmt.excluded.tier,
            "expected_action": stmt.excluded.expected_action,
            "raw_data": stmt.excluded.raw_data,
            "status": "pending",
            "processing_stage": "pending",
            "retry_count": 0,
            "retry_reason": None,
            "task_id": None,
            "updated_at": datetime.utcnow(),
        },
    )
    db.execute(stmt)
    db.commit()
    return len(values), duplicates_skipped


def upsert_customers(db: Session, customers: list[dict[str, Any]]) -> tuple[int, int]:
    if not customers:
        return 0, 0

    customer_records, duplicates_skipped = _filter_unchanged_records(db, Customer, customers, "customer_id")
    if not customer_records:
        return 0, duplicates_skipped

    values = []
    for customer in customer_records:
        address = customer.get("address") or {}
        values.append(
            {
                "id": customer["customer_id"],
                "name": customer.get("name"),
                "tier": customer.get("tier"),
                "email": customer.get("email"),
                "notes": customer.get("notes"),
                "phone": customer.get("phone"),
                "address_json": address,
                "address_street": address.get("street"),
                "address_city": address.get("city"),
                "address_state": address.get("state"),
                "address_zip": address.get("zip"),
                "total_spent": float(customer["total_spent"]) if customer.get("total_spent") is not None else None,
                "member_since": _parse_date(customer.get("member_since")),
                "total_orders": int(customer["total_orders"]) if customer.get("total_orders") is not None else None,
                "raw_data": customer,
            }
        )

    stmt = insert(Customer).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Customer.id],
        set_={
            "name": stmt.excluded.name,
            "tier": stmt.excluded.tier,
            "email": stmt.excluded.email,
            "notes": stmt.excluded.notes,
            "phone": stmt.excluded.phone,
            "address_json": stmt.excluded.address_json,
            "address_street": stmt.excluded.address_street,
            "address_city": stmt.excluded.address_city,
            "address_state": stmt.excluded.address_state,
            "address_zip": stmt.excluded.address_zip,
            "total_spent": stmt.excluded.total_spent,
            "member_since": stmt.excluded.member_since,
            "total_orders": stmt.excluded.total_orders,
            "raw_data": stmt.excluded.raw_data,
            "updated_at": datetime.utcnow(),
        },
    )
    db.execute(stmt)
    db.commit()
    return len(values), duplicates_skipped


def upsert_orders(db: Session, orders: list[dict[str, Any]]) -> tuple[int, int]:
    if not orders:
        return 0, 0

    order_records, duplicates_skipped = _filter_unchanged_records(db, Order, orders, "order_id")
    if not order_records:
        return 0, duplicates_skipped

    values = []
    for order in order_records:
        values.append(
            {
                "id": order["order_id"],
                "customer_id": order.get("customer_id"),
                "product_id": order.get("product_id"),
                "quantity": int(order["quantity"]) if order.get("quantity") is not None else None,
                "amount": float(order["amount"]) if order.get("amount") is not None else None,
                "status": order.get("status"),
                "order_date": _parse_date(order.get("order_date")),
                "delivery_date": _parse_date(order.get("delivery_date")),
                "return_deadline": _parse_date(order.get("return_deadline")),
                "refund_status": order.get("refund_status"),
                "notes": order.get("notes"),
                "raw_data": order,
            }
        )

    stmt = insert(Order).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Order.id],
        set_={
            "customer_id": stmt.excluded.customer_id,
            "product_id": stmt.excluded.product_id,
            "quantity": stmt.excluded.quantity,
            "amount": stmt.excluded.amount,
            "status": stmt.excluded.status,
            "order_date": stmt.excluded.order_date,
            "delivery_date": stmt.excluded.delivery_date,
            "return_deadline": stmt.excluded.return_deadline,
            "refund_status": stmt.excluded.refund_status,
            "notes": stmt.excluded.notes,
            "raw_data": stmt.excluded.raw_data,
            "updated_at": datetime.utcnow(),
        },
    )
    db.execute(stmt)
    db.commit()
    return len(values), duplicates_skipped


def upsert_products(db: Session, products: list[dict[str, Any]]) -> tuple[int, int]:
    if not products:
        return 0, 0

    product_records, duplicates_skipped = _filter_unchanged_records(db, Product, products, "product_id")
    if not product_records:
        return 0, duplicates_skipped

    values = []
    for product in product_records:
        values.append(
            {
                "id": product["product_id"],
                "name": product.get("name"),
                "category": product.get("category"),
                "price": float(product["price"]) if product.get("price") is not None else None,
                "warranty_months": int(product["warranty_months"]) if product.get("warranty_months") is not None else None,
                "return_window_days": int(product["return_window_days"]) if product.get("return_window_days") is not None else None,
                "returnable": bool(product["returnable"]) if product.get("returnable") is not None else None,
                "notes": product.get("notes"),
                "raw_data": product,
            }
        )

    stmt = insert(Product).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Product.id],
        set_={
            "name": stmt.excluded.name,
            "category": stmt.excluded.category,
            "price": stmt.excluded.price,
            "warranty_months": stmt.excluded.warranty_months,
            "return_window_days": stmt.excluded.return_window_days,
            "returnable": stmt.excluded.returnable,
            "notes": stmt.excluded.notes,
            "raw_data": stmt.excluded.raw_data,
            "updated_at": datetime.utcnow(),
        },
    )
    db.execute(stmt)
    db.commit()
    return len(values), duplicates_skipped


def _customer_row_to_dict(customer: Customer) -> dict[str, Any]:
    if customer.raw_data:
        return customer.raw_data

    return {
        "customer_id": customer.id,
        "name": customer.name,
        "tier": customer.tier,
        "email": customer.email,
        "notes": customer.notes,
        "phone": customer.phone,
        "address": customer.address_json,
        "total_spent": customer.total_spent,
        "member_since": customer.member_since.isoformat() if customer.member_since else None,
        "total_orders": customer.total_orders,
    }


def _order_row_to_dict(order: Order) -> dict[str, Any]:
    if order.raw_data:
        return order.raw_data

    return {
        "order_id": order.id,
        "customer_id": order.customer_id,
        "product_id": order.product_id,
        "quantity": order.quantity,
        "amount": order.amount,
        "status": order.status,
        "order_date": order.order_date.isoformat() if order.order_date else None,
        "delivery_date": order.delivery_date.isoformat() if order.delivery_date else None,
        "return_deadline": order.return_deadline.isoformat() if order.return_deadline else None,
        "refund_status": order.refund_status,
        "notes": order.notes,
    }


def _product_row_to_dict(product: Product) -> dict[str, Any]:
    if product.raw_data:
        return product.raw_data

    return {
        "product_id": product.id,
        "name": product.name,
        "category": product.category,
        "price": product.price,
        "warranty_months": product.warranty_months,
        "return_window_days": product.return_window_days,
        "returnable": product.returnable,
        "notes": product.notes,
    }


def get_ticket(db: Session, ticket_id: str) -> Ticket | None:
    return db.get(Ticket, ticket_id)


def list_tickets_for_processing(db: Session) -> list[Ticket]:
    stmt = select(Ticket).where(Ticket.status == "pending", Ticket.task_id.is_(None))
    return list(db.scalars(stmt).all())


def list_processing_tickets_with_task(db: Session) -> list[Ticket]:
    stmt = select(Ticket).where(Ticket.status == "processing", Ticket.task_id.is_not(None))
    return list(db.scalars(stmt).all())


def set_ticket_task(db: Session, ticket_id: str, task_id: str | None, status: str | None = None) -> None:
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return

    ticket.task_id = task_id
    if status is not None:
        ticket.status = status
    db.commit()


def try_claim_ticket_processing(db: Session, ticket_id: str, task_id: str) -> bool:
    result = db.execute(
        text(
            """
            UPDATE tickets
            SET status = 'processing',
                task_id = :task_id,
                updated_at = now()
            WHERE id = :ticket_id
              AND status = 'pending'
            """
        ),
        {"ticket_id": ticket_id, "task_id": task_id},
    )
    db.commit()
    return int(result.rowcount or 0) == 1


def set_ticket_status(db: Session, ticket_id: str, status: str) -> None:
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return

    ticket.status = status
    db.commit()


def set_ticket_retry_reason(db: Session, ticket_id: str, reason: str | None) -> None:
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return

    ticket.retry_reason = reason
    db.commit()


def set_ticket_pending_for_retry(
    db: Session,
    ticket_id: str,
    task_id: str | None = None,
    reason: str | None = None,
) -> bool:
    where_clause = "id = :ticket_id AND status = 'processing'"
    params: dict[str, Any] = {"ticket_id": ticket_id}
    if task_id is not None:
        where_clause += " AND task_id = :task_id"
        params["task_id"] = task_id
    params["reason"] = reason

    result = db.execute(
        text(
            f"""
            UPDATE tickets
            SET status = 'pending',
                retry_count = COALESCE(retry_count, 0) + 1,
                retry_reason = :reason,
                updated_at = now()
            WHERE {where_clause}
            """
        ),
        params,
    )
    db.commit()
    return int(result.rowcount or 0) == 1


def get_ticket_stage(db: Session, ticket_id: str) -> str:
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return "pending"
    return ticket.processing_stage or "pending"


def get_ticket_runtime_state(db: Session, ticket_id: str) -> dict[str, Any]:
    ticket = db.get(Ticket, ticket_id)
    if not ticket or not ticket.raw_data:
        return {}
    runtime = ticket.raw_data.get("agent_runtime", {})
    return runtime if isinstance(runtime, dict) else {}


def persist_ticket_stage(db: Session, ticket_id: str, stage: str, runtime_updates: dict[str, Any] | None = None) -> None:
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return

    payload = dict(ticket.raw_data or {})
    runtime = payload.get("agent_runtime", {})
    if not isinstance(runtime, dict):
        runtime = {}

    if runtime_updates:
        runtime.update(runtime_updates)

    payload["agent_runtime"] = runtime
    payload["processing_stage"] = stage

    ticket.raw_data = payload
    ticket.processing_stage = stage
    db.commit()


def update_ticket_data(db: Session, ticket_id: str, raw_data: dict[str, Any]) -> bool:
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return False

    ticket.customer_email = raw_data.get("customer_email")
    ticket.subject = raw_data.get("subject")
    ticket.body = raw_data.get("body")
    ticket.source = raw_data.get("source")
    ticket.ticket_created_at = _parse_datetime(raw_data.get("created_at"))
    ticket.tier = int(raw_data["tier"]) if raw_data.get("tier") is not None else ticket.tier
    ticket.expected_action = raw_data.get("expected_action")
    ticket.raw_data = raw_data
    db.commit()
    return True


def get_customer_by_email(db: Session, email: str) -> dict[str, Any] | None:
    stmt = select(Customer).where(Customer.email == email)
    row = db.scalars(stmt).first()
    return _customer_row_to_dict(row) if row else None


def get_order_by_id(db: Session, order_id: str) -> dict[str, Any] | None:
    row = db.get(Order, order_id)
    return _order_row_to_dict(row) if row else None


def get_latest_order_for_customer(db: Session, customer_id: str) -> dict[str, Any] | None:
    stmt = (
        select(Order)
        .where(Order.customer_id == customer_id)
        .order_by(desc(Order.order_date), desc(Order.created_at))
    )
    row = db.scalars(stmt).first()
    return _order_row_to_dict(row) if row else None


def get_product_by_id(db: Session, product_id: str) -> dict[str, Any] | None:
    row = db.get(Product, product_id)
    return _product_row_to_dict(row) if row else None


def update_order_data(db: Session, order_id: str, data: dict[str, Any]) -> bool:
    row = db.get(Order, order_id)
    if not row:
        return False

    row.customer_id = data.get("customer_id")
    row.product_id = data.get("product_id")
    row.quantity = int(data["quantity"]) if data.get("quantity") is not None else row.quantity
    row.amount = float(data["amount"]) if data.get("amount") is not None else row.amount
    row.status = data.get("status")
    row.order_date = _parse_date(data.get("order_date"))
    row.delivery_date = _parse_date(data.get("delivery_date"))
    row.return_deadline = _parse_date(data.get("return_deadline"))
    row.refund_status = data.get("refund_status")
    row.notes = data.get("notes")
    row.raw_data = data
    db.commit()
    return True


def insert_audit_log(
    db: Session,
    ticket_id: str,
    step: str,
    action: str,
    input_payload: dict[str, Any] | None,
    output_payload: dict[str, Any] | None,
    confidence: float,
) -> None:
    audit = AuditLog(
        ticket_id=ticket_id,
        step=step,
        action=action,
        input=input_payload or {},
        output=output_payload or {},
        confidence=confidence,
    )
    db.add(audit)
    db.commit()


def list_audit_logs(db: Session, ticket_id: str) -> list[AuditLog]:
    stmt = select(AuditLog).where(AuditLog.ticket_id == ticket_id).order_by(AuditLog.timestamp.asc())
    return list(db.scalars(stmt).all())


def count_audit_logs(db: Session) -> int:
    return int(db.execute(text("SELECT COUNT(1) FROM audit_logs")).scalar() or 0)


def export_audit_logs_json_snapshot(db: Session) -> str:
    stmt = select(AuditLog).order_by(AuditLog.timestamp.asc(), AuditLog.id.asc())
    logs = list(db.scalars(stmt).all())

    payload = {
        "audit_logs": [
            {
                "id": log.id,
                "ticket_id": log.ticket_id,
                "step": log.step,
                "action": log.action,
                "input": log.input,
                "output": log.output,
                "confidence": log.confidence,
                "timestamp": log.timestamp.isoformat() if log.timestamp else None,
            }
            for log in logs
        ]
    }

    AUDIT_JSON_DIR.mkdir(parents=True, exist_ok=True)
    temp_file = AUDIT_JSON_FILE.with_suffix(".json.tmp")
    with temp_file.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)

    temp_file.replace(AUDIT_JSON_FILE)
    return str(AUDIT_JSON_FILE)


def maybe_write_audit_snapshot_if_all_processed(db: Session) -> bool:
    active_count = int(
        db.execute(
            text(
                """
                SELECT COUNT(1)
                FROM tickets
                WHERE status IN ('pending', 'processing')
                """
            )
        ).scalar()
        or 0
    )

    if active_count > 0:
        return False

    export_audit_logs_json_snapshot(db)
    return True


def list_dead_letter_entries(db: Session, ticket_id: str) -> list[DeadLetterQueue]:
    stmt = (
        select(DeadLetterQueue)
        .where(DeadLetterQueue.ticket_id == ticket_id)
        .order_by(DeadLetterQueue.timestamp.asc())
    )
    return list(db.scalars(stmt).all())


def list_action_logs(db: Session, ticket_id: str) -> list[ActionLog]:
    stmt = select(ActionLog).where(ActionLog.ticket_id == ticket_id).order_by(ActionLog.created_at.asc())
    return list(db.scalars(stmt).all())


def has_audit_event(
    db: Session,
    ticket_id: str,
    step: str,
    action: str,
    input_key: str | None = None,
    input_value: str | None = None,
) -> bool:
    stmt = (
        select(AuditLog)
        .where(AuditLog.ticket_id == ticket_id)
        .where(AuditLog.step == step)
        .where(AuditLog.action == action)
    )
    records = list(db.scalars(stmt).all())
    if input_key is None:
        return bool(records)

    for record in records:
        record_input = record.input or {}
        if str(record_input.get(input_key)) == str(input_value):
            return True
    return False


def insert_dead_letter(db: Session, ticket_id: str, error: str, payload: dict[str, Any]) -> None:
    dlq = DeadLetterQueue(ticket_id=ticket_id, error=error, payload=payload)
    db.add(dlq)
    db.commit()


def has_action_fingerprint(db: Session, ticket_id: str, action_type: str, fingerprint: str) -> bool:
    stmt = (
        select(ActionLog)
        .where(ActionLog.ticket_id == ticket_id)
        .where(ActionLog.action_type == action_type)
        .where(ActionLog.fingerprint == fingerprint)
    )
    return db.scalars(stmt).first() is not None


def record_action_fingerprint(
    db: Session,
    ticket_id: str,
    action_type: str,
    fingerprint: str,
    payload: dict[str, Any] | None = None,
) -> bool:
    stmt = insert(ActionLog).values(
        ticket_id=ticket_id,
        action_type=action_type,
        fingerprint=fingerprint,
        payload=payload or {},
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=[ActionLog.ticket_id, ActionLog.action_type, ActionLog.fingerprint]
    )
    result = db.execute(stmt)
    db.commit()
    return int(result.rowcount or 0) == 1


def remove_action_fingerprint(db: Session, ticket_id: str, action_type: str, fingerprint: str) -> None:
    db.execute(
        text(
            """
            DELETE FROM action_logs
            WHERE ticket_id = :ticket_id
              AND action_type = :action_type
              AND fingerprint = :fingerprint
            """
        ),
        {
            "ticket_id": ticket_id,
            "action_type": action_type,
            "fingerprint": fingerprint,
        },
    )
    db.commit()


def json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)
