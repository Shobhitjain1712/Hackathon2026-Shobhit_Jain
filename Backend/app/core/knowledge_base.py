from __future__ import annotations

import logging
import re
from typing import Iterable

from openai import OpenAI
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.repositories import clear_knowledge_base, insert_knowledge_chunk, knowledge_base_supports_vector

logger = logging.getLogger(__name__)


def chunk_markdown(content: str, max_chars: int) -> list[str]:
    normalized = content.replace("\r\n", "\n").strip()
    if not normalized:
        return []

    blocks = re.split(r"\n\s*\n", normalized)
    chunks: list[str] = []
    current = ""

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        # Keep section headings attached to nearby text where possible.
        if current and len(current) + len(block) + 2 > max_chars:
            chunks.append(current.strip())
            current = block
        else:
            current = f"{current}\n\n{block}".strip()

    if current:
        chunks.append(current.strip())

    return chunks


def _embed_chunks(chunks: Iterable[str]) -> list[list[float]]:
    settings = get_settings()
    client = OpenAI(api_key=settings.openai_api_key)
    embeddings: list[list[float]] = []

    for chunk in chunks:
        response = client.embeddings.create(model=settings.embedding_model, input=chunk)
        embeddings.append(response.data[0].embedding)

    return embeddings


def ingest_knowledge_base_markdown(db: Session, markdown_content: str) -> int:
    settings = get_settings()
    chunks = chunk_markdown(markdown_content, max_chars=settings.kb_chunk_max_chars)

    clear_knowledge_base(db)
    if not chunks:
        return 0

    if knowledge_base_supports_vector(db):
        try:
            embeddings = _embed_chunks(chunks)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Embedding generation failed; continuing with text-only fallback: %s", exc)
            embeddings = [None for _ in chunks]
    else:
        embeddings = [None for _ in chunks]

    inserted = 0
    for chunk, embedding in zip(chunks, embeddings):
        insert_knowledge_chunk(db, content=chunk, embedding=embedding)
        inserted += 1

    return inserted
