# Autonomous Support Resolution Agent

production-minded autonomous support platform that resolves customer tickets with auditable AI decisions, deterministic tool orchestration, and concurrent background processing.

This project is intentionally built to look and behave like an industry-grade system, not just a demo:

- async-first API layer
- resilient agent workflow with retries and dead-letter safety
- strict schema contracts for agent/tool reliability
- hosted database compatibility for low-ops deployment
- complete auditability from first action to final decision

## Live Demo

- Video demo: https://drive.google.com/file/d/1P9SgRv1jhJC-a665a_IbCvusFjqhK09t/view?usp=sharing

The project is fully hosted, so reviewers can directly access the UI and test backend APIs from the hosted FastAPI documentation interface.

## Why This Architecture Matters

Modern support automation requires more than model output quality. It needs architecture maturity, production realism, and operational safety.

This solution demonstrates all three:

- **Business impact**: Automates high-volume support scenarios (refunds, cancellations, policy answers, escalations).
- **Engineering maturity**: Includes retry policy, idempotency, state checkpointing, and dead-letter handling.
- **Trust and transparency**: Every agent step is logged in `audit_logs`, exportable as JSON, and downloadable directly from browser.
- **Scale-ready design**: FastAPI + Celery + Redis decouples request handling from ticket execution.

## Core Advantages and Technology Choices

## Hosted PostgreSQL (Security + Low Maintenance)

We use an external/hosted PostgreSQL instead of bundling a local DB container by default.

Why it matters:

- **Security posture**: Managed providers offer TLS, network controls, backups, and patching.
- **Operational simplicity**: No DB lifecycle burden inside the app stack.
- **Team scalability**: Shared managed database enables collaborative testing and production-like behavior.
- **Real-world fit**: Most production AI applications connect to managed data planes.

## Pydantic in Agentic Systems

Pydantic is critical here, not optional.

Why it matters in agentic AI:

- **Deterministic contracts** for tool inputs/outputs.
- **Schema-safe LLM integration** for structured decisions.
- **Early failure visibility** when malformed responses occur.
- **Predictable retries** because invalid payloads are explicitly classified and handled.

In short: Pydantic converts uncertain AI output into enforceable interfaces.

## FastAPI (Async-First API Layer)

FastAPI was selected for its performance and type-integrated developer experience.

Why it matters:

- **Async-first request handling** supports high concurrency.
- **Strong request/response modeling** aligns with Pydantic contracts.
- **Clear OpenAPI-ready endpoints** for integration and operational demos.
- **Low latency control plane** while heavy processing is delegated to workers.

## LangChain + LangGraph (Tool Orchestration + Agentic Flow)

The system combines LangChain primitives with a LangGraph state-machine workflow.

Why it matters:

- **Structured agent pipeline**: classify -> plan -> execute -> validate -> decide.
- **Tool orchestration** with explicit intermediate state.
- **Checkpoint continuity** across retries using persisted runtime state.
- **Safer decisioning** through validation and policy-based action selection.

## Celery + Redis (Industry-grade Concurrent Processing)

Support ticket traffic is bursty by nature. Celery + Redis handles this reliably.

Why it matters:

- **Concurrent ticket execution** via background workers.
- **Priority routing** for high-tier tickets.
- **Exponential backoff + jitter** for transient failures.
- **Queue decoupling** between API and processing path.
- **Operational resilience** with dead-letter fallback when retries exhaust.

## High-Level Architecture

1. **Frontend** uploads datasets and triggers processing.
2. **FastAPI** validates uploads, stores normalized records, and queues jobs.
3. **Celery workers** execute autonomous ticket workflows.
4. **Support tools** fetch/update business entities and policy context.
5. **Audit logs** capture every step and decision for traceability.
6. **Audit export** writes JSON snapshot to disk and supports browser download.

## Project Structure

- `Backend/`
  - `app/`
    - `api/` FastAPI route handlers
    - `agents/` LangGraph autonomous resolution pipeline
    - `tools/` Tool implementations + failure modeling + idempotent side effects
    - `models/` Pydantic schemas for ingest, APIs, tools, and agent outputs
    - `db/` SQLAlchemy models, repositories, schema evolution, audit export
    - `workers/` Celery tasks, retry/dead-letter behavior
    - `core/` app config, logging, rate limit, knowledge ingestion
  - `Dockerfile`, `.env.example`, `requirements.txt`
- `Frontend/`
  - `index.html`, `styles.css`, `app.js`
- `docker-compose.yml`

## One-Command Run

1. Clone this repository.
2. Configure `Backend/.env` (copy from `Backend/.env.example`).
3. Run one command from repo root:

```bash
docker-compose up --build
```

App will be available at:

- `http://localhost:8000`

## Environment Configuration

Create `Backend/.env` from `Backend/.env.example`:

```env
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4o-mini

DB_HOST=
DB_NAME=
DB_USER=
DB_PASSWORD=
DB_PORT=5432
DB_SSLMODE=require

REDIS_URL=redis://redis:6379/0
TICKET_LOCK_TTL_SECONDS=900

TOOL_FAILURE_SIMULATION=true
TOOL_FAILURE_RATE=0.08
REQUEST_RATE_LIMIT_PER_MINUTE=80
KB_CHUNK_MAX_CHARS=1200
```

## End-to-End Data Flow

1. **Data ingest** (`POST /upload`)
   - JSON files (`tickets`, `customers`, `orders`, `products`) + markdown knowledge base.
   - Records are upserted into PostgreSQL.
   - KB content is chunked and embedded (pgvector if available; fallback search otherwise).

2. **Queueing** (`POST /process`)
   - Pending tickets are dispatched to Celery queues (`priority` vs `default`) based on tier.
   - Worker ownership is claimed atomically at DB level.

3. **Agent execution**
   - Classify ticket intent and urgency.
   - Plan actionable sequence.
   - Execute tools to gather customer/order/product/policy context.
   - Validate tool outputs.
   - Decide final action and execute side effects.

4. **Decision + action**
   - Resolve, retry, or escalate according to confidence and failures.
   - Idempotent action logs prevent duplicate side effects.

5. **Observability**
   - Full reasoning/action trace is stored in `audit_logs`.
   - Status and details are queryable via API and frontend.

6. **Audit export**
   - Snapshot file written to `Backend/audit_json/audit_logs.json`.
   - Export available via API and downloadable from frontend button.

## Database Schema (Operationally Relevant)

- `tickets`
  - identity + payload + lifecycle state
  - includes `status`, `processing_stage`, `task_id`, `retry_count`, `retry_reason`
- `customers`
  - profile/tier/contact/history context
- `orders`
  - mutable order state (`status`, `refund_status`, refund metadata)
- `products`
  - category/policy attributes used in eligibility logic
- `knowledge_base`
  - policy chunks + text search support
- `audit_logs`
  - structured event ledger for every important step
- `action_logs`
  - idempotency fingerprints for side-effect tools
- `dead_letter_queue`
  - terminal failures after retry exhaustion

## Tool Selection Strategy (Condition-Based)

Tool calls are selected based on ticket context and intermediate results:

- Always start with identity/context tools (`get_customer`, `get_order` when resolvable).
- If order is known, fetch product and eligibility (`get_product`, `check_refund_eligibility`).
- Retrieve policy grounding (`search_knowledge_base`) for deterministic rationale.
- Side-effect tools are conditionally applied:
  - `issue_refund` if eligible and not already refunded
  - `cancel_order` for valid cancellation windows
  - `escalate` when confidence/conditions demand specialist intervention
  - `send_reply` for customer communication

This balances automation speed with policy safety and prevents blind tool execution.

## API Endpoints

## Core Processing

- `POST /upload`
  - Ingests business data + knowledge base.
- `POST /process`
  - Queues pending tickets for worker processing.
- `GET /status/{ticket_id}`
  - Current status + decision summary + retry metadata.

## Logs and Investigation

- `GET /logs/{ticket_id}`
  - Full audit trail for a ticket.
- `GET /details/{ticket_id}`
  - Audit + dead-letter details for root-cause analysis.
- `GET /actions/{ticket_id}`
  - Side-effect idempotency/action records.

## Audit Export and Download

- `POST /audit/export`
  - Writes/updates snapshot file and returns row count + path.
- `GET /audit/export/download`
  - Streams `audit_logs.json` as browser download.

Output file location:

- `Backend/audit_json/audit_logs.json`

## Agent Workflow

Graph nodes:

1. `classify_ticket`
2. `plan_actions`
3. `execute_tools`
4. `validate_outputs`
5. `decision_node`

Runtime continuity:

- Agent runtime state is checkpointed in ticket payload.
- Processing stage is persisted to survive retries.

## Decision Policy

- **Retry** when execution/validation failures indicate transient issues.
- **Escalate** when confidence is low or case requires human/specialist handling.
- **Resolve** when sufficient context and policy checks are satisfied.
- **Already-processed guardrails** avoid duplicate refund/cancel side effects.

## Frontend Usage

1. Open `http://4.174.128.13:8000/` (hosted) or `http://localhost:8000` (local Docker run).
2. Upload files (`tickets`, `customers`, `orders`, `products`, `knowledge_base`).
3. Click **Upload Files**.
4. Click **Process Tickets** for concurrent autonomous processing.
5. Monitor ticket states, details, and actions.
6. Click **Export Audit JSON** to export and download latest audit snapshot.

For backend-only API testing, open `http://4.174.128.13:8000/docs`.

## Fault Tolerance and Reliability

- Atomic DB claim for processing ownership prevents duplicate worker execution.
- Redis lock adds additional concurrency protection.
- Celery retries with backoff and jitter for transient faults.
- Dead-letter queue captures terminal failures safely.
- Idempotency fingerprints prevent duplicate side effects (refund/reply/escalate/cancel).
- Structured audit logging enables deterministic postmortems.
- pgvector fallback ensures degraded-but-functional KB retrieval.
- Rate limiting protects critical endpoints under load.

## Why This Is Production Grade Under the Hood

- Separation of control plane (FastAPI) and execution plane (Celery workers).
- Explicit schema contracts across ingest, tools, decisions, and API responses.
- Built-in observability and forensic export path.
- Concurrency, idempotency, and retry controls modeled after real support operations.
- Hosted DB-first deployment model for security and reduced operational overhead.
