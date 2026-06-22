"""
Database layer for the synthetic brain project.
SQLAlchemy + psycopg2 against a local PostgreSQL + pgvector instance.
"""
import os
from contextlib import contextmanager

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://synthetic_brain:changeme@localhost:5432/synthetic_brain"
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


@contextmanager
def get_session():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_or_create_subject(name: str, subject_type: str = "person") -> int:
    """Return the subject id for a name, creating the row if it doesn't exist yet."""
    with get_session() as session:
        existing = session.execute(
            text("SELECT id FROM subjects WHERE name = :name"), {"name": name}
        ).fetchone()
        if existing:
            return existing[0]

        result = session.execute(
            text(
                "INSERT INTO subjects (name, canonical_name, subject_type) "
                "VALUES (:name, :name, :subject_type) RETURNING id"
            ),
            {"name": name, "subject_type": subject_type},
        )
        return result.fetchone()[0]


def insert_raw_document(source_id: int, subject_id: int, raw_text: str, content_hash: str, metadata: dict) -> int | None:
    """Insert a scraped document. Returns None if content_hash already exists (dedup)."""
    with get_session() as session:
        result = session.execute(
            text(
                "INSERT INTO raw_documents (source_id, subject_id, raw_text, content_hash, metadata) "
                "VALUES (:source_id, :subject_id, :raw_text, :content_hash, :metadata) "
                "ON CONFLICT (content_hash) DO NOTHING RETURNING id"
            ),
            {
                "source_id": source_id,
                "subject_id": subject_id,
                "raw_text": raw_text,
                "content_hash": content_hash,
                "metadata": str(metadata).replace("'", '"'),
            },
        )
        row = result.fetchone()
        return row[0] if row else None


def insert_chunk_with_embedding(
    document_id: int, subject_id: int, chunk_index: int, chunk_text: str, embedding: list[float]
) -> int:
    """Insert one chunk with its embedding. `embedding` length must match the VECTOR(384) column."""
    with get_session() as session:
        result = session.execute(
            text(
                "INSERT INTO chunks (document_id, subject_id, chunk_index, chunk_text, embedding) "
                "VALUES (:document_id, :subject_id, :chunk_index, :chunk_text, :embedding) RETURNING id"
            ),
            {
                "document_id": document_id,
                "subject_id": subject_id,
                "chunk_index": chunk_index,
                "chunk_text": chunk_text,
                "embedding": str(embedding),  # pgvector accepts '[0.1,0.2,...]' as text
            },
        )
        return result.fetchone()[0]


def find_similar_chunks(subject_id: int, query_embedding: list[float], limit: int = 10):
    """Cosine-similarity search over one subject's chunks using pgvector's <=> operator."""
    with get_session() as session:
        rows = session.execute(
            text(
                "SELECT id, chunk_text, embedding <=> :query_embedding AS distance "
                "FROM chunks WHERE subject_id = :subject_id "
                "ORDER BY distance ASC LIMIT :limit"
            ),
            {
                "subject_id": subject_id,
                "query_embedding": str(query_embedding),
                "limit": limit,
            },
        ).fetchall()
        return rows
