"""
Database layer for the synthetic brain project.
pymongo against a local MongoDB instance.
Full CRUD for all collections.
"""
import math
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING, DESCENDING

load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "mongodb://root:changeme@localhost:27017/synthetic_brain?authSource=admin",
)

_client = MongoClient(DATABASE_URL)
_db = _client.get_default_database()


# ---------------------------------------------------------------------------
# Auto-increment helper (replaces SERIAL PRIMARY KEY)
# ---------------------------------------------------------------------------

def get_next_sequence_value(name: str) -> int:
    """Atomically increment and return the next integer ID for a given sequence name."""
    result = _db.counters.find_one_and_update(
        {"_id": name},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=True,   # return the *updated* document
    )
    return result["seq"]


# ---------------------------------------------------------------------------
# Index creation (called once at import time)
# ---------------------------------------------------------------------------

def _ensure_indexes():
    """Create indexes on subject_id fields and unique constraints."""
    _db.subjects.create_index([("name", ASCENDING)], unique=True)

    _db.sources.create_index([("subject_id", ASCENDING)])
    _db.sources.create_index([("subject_id", ASCENDING), ("url", ASCENDING)], unique=True)

    _db.scraped_documents.create_index([("subject_id", ASCENDING)])
    _db.scraped_documents.create_index([("content_hash", ASCENDING)], unique=True)

    _db.pdf_documents.create_index([("subject_id", ASCENDING)])
    _db.pdf_documents.create_index([("content_hash", ASCENDING)], unique=True)

    _db.chunks.create_index([("subject_id", ASCENDING)])
    _db.chunks.create_index([("document_id", ASCENDING)])

    _db.entities.create_index([("subject_id", ASCENDING)])
    _db.entities.create_index(
        [("subject_id", ASCENDING), ("name", ASCENDING), ("entity_type", ASCENDING)],
        unique=True,
    )

    _db.entity_mentions.create_index([("entity_id", ASCENDING)])
    _db.entity_mentions.create_index([("chunk_id", ASCENDING)])

    _db.clusters.create_index([("subject_id", ASCENDING)])
    _db.cluster_members.create_index([("cluster_id", ASCENDING)])
    _db.cluster_members.create_index(
        [("cluster_id", ASCENDING), ("chunk_id", ASCENDING)], unique=True
    )

    _db.relationships.create_index([("subject_id", ASCENDING)])

    _db.synthesized_notes.create_index([("subject_id", ASCENDING)])

    _db.pipeline_runs.create_index([("subject_id", ASCENDING)])
    _db.pipeline_runs.create_index(
        [("subject_id", ASCENDING), ("stage", ASCENDING)]
    )


_ensure_indexes()


# ---------------------------------------------------------------------------
# Subjects
# ---------------------------------------------------------------------------

def get_or_create_subject(name: str, subject_type: str = "person") -> int:
    """Return the subject id for a name, creating the document if it doesn't exist."""
    existing = _db.subjects.find_one({"name": name})
    if existing:
        return existing["_id"]

    new_id = get_next_sequence_value("subjects")
    try:
        _db.subjects.insert_one({
            "_id": new_id,
            "name": name,
            "canonical_name": name,
            "subject_type": subject_type,
            "created_at": datetime.now(timezone.utc),
        })
    except Exception:
        # Race-condition: another writer inserted first
        existing = _db.subjects.find_one({"name": name})
        if existing:
            return existing["_id"]
        raise
    return new_id


def get_subject(subject_id: int) -> dict | None:
    """Return subject dict or None."""
    doc = _db.subjects.find_one({"_id": subject_id})
    if doc:
        return {
            "id": doc["_id"],
            "name": doc["name"],
            "canonical_name": doc.get("canonical_name"),
            "subject_type": doc.get("subject_type"),
            "created_at": doc.get("created_at"),
        }
    return None


def list_subjects() -> list[dict]:
    """Return all subjects."""
    docs = _db.subjects.find().sort("created_at", DESCENDING)
    return [
        {
            "id": d["_id"],
            "name": d["name"],
            "canonical_name": d.get("canonical_name"),
            "subject_type": d.get("subject_type"),
            "created_at": d.get("created_at"),
        }
        for d in docs
    ]


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def insert_source(subject_id: int, source_type: str, url: str) -> int:
    """Insert or get a source document. Returns source id."""
    existing = _db.sources.find_one({"subject_id": subject_id, "url": url})
    if existing:
        return existing["_id"]

    new_id = get_next_sequence_value("sources")
    try:
        _db.sources.insert_one({
            "_id": new_id,
            "subject_id": subject_id,
            "source_type": source_type,
            "url": url,
            "fetched_at": None,
            "status": "pending",
        })
    except Exception:
        existing = _db.sources.find_one({"subject_id": subject_id, "url": url})
        if existing:
            return existing["_id"]
        raise
    return new_id


def update_source_status(source_id: int, status: str, fetched_at: datetime | None = None):
    """Mark a source as success/failed/blocked."""
    _db.sources.update_one(
        {"_id": source_id},
        {"$set": {"status": status, "fetched_at": fetched_at or datetime.now(timezone.utc)}},
    )


def get_sources(subject_id: int) -> list[dict]:
    """Return all sources for a subject."""
    docs = _db.sources.find({"subject_id": subject_id})
    return [
        {
            "id": d["_id"],
            "subject_id": d["subject_id"],
            "source_type": d["source_type"],
            "url": d["url"],
            "fetched_at": d.get("fetched_at"),
            "status": d.get("status"),
        }
        for d in docs
    ]


# ---------------------------------------------------------------------------
# Raw documents  (split into scraped_documents + pdf_documents)
# ---------------------------------------------------------------------------

_SOURCE_TYPE_WEB = {"wikipedia", "news", "imdb", "social", "ai_search"}


def _pick_collection(source_type: str):
    """Route to the correct collection based on source_type."""
    if source_type == "pdf":
        return _db.pdf_documents
    return _db.scraped_documents


def insert_raw_document(
    source_id: int,
    subject_id: int,
    raw_text: str,
    content_hash: str,
    metadata: dict,
    source_type: str = "wikipedia",
) -> int | None:
    """Insert a scraped/PDF document. Returns None if content_hash already exists (dedup)."""
    coll = _pick_collection(source_type)

    # Dedup check
    if coll.find_one({"content_hash": content_hash}):
        return None

    new_id = get_next_sequence_value("raw_documents")
    try:
        coll.insert_one({
            "_id": new_id,
            "source_id": source_id,
            "subject_id": subject_id,
            "raw_text": raw_text,
            "content_hash": content_hash,
            "scraped_at": datetime.now(timezone.utc),
            "metadata": metadata or {},
            "source_type": source_type,
        })
    except Exception:
        # Duplicate key on content_hash unique index
        return None
    return new_id


def insert_pdf_document(
    subject_id: int,
    raw_text: str,
    content_hash: str,
    metadata: dict,
    filename: str,
) -> int | None:
    """Insert a PDF document. Convenience wrapper that routes to pdf_documents collection."""
    if metadata is None:
        metadata = {}
    metadata.setdefault("filename", filename)
    metadata.setdefault("source_type", "pdf")
    metadata.setdefault("title", filename)

    # Create a synthetic source entry for the PDF
    url = f"file://{filename}"
    source_id = insert_source(subject_id, "pdf", url)
    update_source_status(source_id, "success")

    return insert_raw_document(
        source_id=source_id,
        subject_id=subject_id,
        raw_text=raw_text,
        content_hash=content_hash,
        metadata=metadata,
        source_type="pdf",
    )


def get_raw_documents(subject_id: int) -> list[dict]:
    """Return all raw documents for a subject (both scraped + PDF)."""
    results = []
    for coll in (_db.scraped_documents, _db.pdf_documents):
        for d in coll.find({"subject_id": subject_id}).sort("scraped_at", ASCENDING):
            results.append({
                "id": d["_id"],
                "source_id": d.get("source_id"),
                "raw_text": d["raw_text"],
                "content_hash": d["content_hash"],
                "scraped_at": d.get("scraped_at"),
                "metadata": d.get("metadata", {}),
            })
    return results


def get_unchunked_documents(subject_id: int) -> list[dict]:
    """Return raw_documents that have no chunks yet (both scraped + PDF)."""
    # Get set of document_ids that already have chunks
    chunked_ids = set()
    for c in _db.chunks.find({"subject_id": subject_id}, {"document_id": 1}):
        chunked_ids.add(c["document_id"])

    results = []
    for coll in (_db.scraped_documents, _db.pdf_documents):
        for d in coll.find({"subject_id": subject_id}):
            if d["_id"] not in chunked_ids:
                results.append({
                    "id": d["_id"],
                    "raw_text": d["raw_text"],
                    "content_hash": d["content_hash"],
                    "metadata": d.get("metadata", {}),
                })
    return results


# ---------------------------------------------------------------------------
# Chunks
# ---------------------------------------------------------------------------

def insert_chunk(document_id: int, subject_id: int, chunk_index: int, chunk_text: str) -> int:
    """Insert a chunk without embedding (embedding added later)."""
    new_id = get_next_sequence_value("chunks")
    _db.chunks.insert_one({
        "_id": new_id,
        "document_id": document_id,
        "subject_id": subject_id,
        "chunk_index": chunk_index,
        "chunk_text": chunk_text,
        "embedding": None,
        "created_at": datetime.now(timezone.utc),
    })
    return new_id


def insert_chunk_with_embedding(
    document_id: int, subject_id: int, chunk_index: int, chunk_text: str, embedding: list[float]
) -> int:
    """Insert one chunk with its embedding."""
    new_id = get_next_sequence_value("chunks")
    _db.chunks.insert_one({
        "_id": new_id,
        "document_id": document_id,
        "subject_id": subject_id,
        "chunk_index": chunk_index,
        "chunk_text": chunk_text,
        "embedding": embedding,
        "created_at": datetime.now(timezone.utc),
    })
    return new_id


def update_chunk_embedding(chunk_id: int, embedding: list[float]):
    """Set the embedding on an existing chunk."""
    _db.chunks.update_one(
        {"_id": chunk_id},
        {"$set": {"embedding": embedding}},
    )


def get_chunks_for_subject(subject_id: int, with_embedding: bool = False) -> list[dict]:
    """Return all chunks for a subject. Optionally include embeddings."""
    projection = {"document_id": 1, "chunk_index": 1, "chunk_text": 1}
    if with_embedding:
        projection["embedding"] = 1

    docs = _db.chunks.find(
        {"subject_id": subject_id}, projection
    ).sort([("document_id", ASCENDING), ("chunk_index", ASCENDING)])

    results = []
    for d in docs:
        item = {
            "id": d["_id"],
            "document_id": d["document_id"],
            "chunk_index": d["chunk_index"],
            "chunk_text": d["chunk_text"],
        }
        if with_embedding:
            item["embedding"] = d.get("embedding")
        results.append(item)
    return results


def get_unembedded_chunks(subject_id: int) -> list[dict]:
    """Return chunks without embeddings."""
    docs = _db.chunks.find(
        {"subject_id": subject_id, "embedding": None}
    ).sort("_id", ASCENDING)
    return [{"id": d["_id"], "chunk_text": d["chunk_text"]} for d in docs]


def find_similar_chunks(subject_id: int, query_embedding: list[float], limit: int = 10):
    """Cosine-similarity search over one subject's chunks (in-memory calculation)."""
    docs = _db.chunks.find(
        {"subject_id": subject_id, "embedding": {"$ne": None}},
        {"chunk_text": 1, "embedding": 1},
    )

    def _cosine_distance(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 1.0
        return 1.0 - (dot / (norm_a * norm_b))

    scored = []
    for d in docs:
        emb = d.get("embedding")
        if not emb:
            continue
        dist = _cosine_distance(query_embedding, emb)
        scored.append({"id": d["_id"], "chunk_text": d["chunk_text"], "distance": dist})

    scored.sort(key=lambda x: x["distance"])
    return scored[:limit]


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------

def insert_entity(subject_id: int, name: str, entity_type: str) -> int:
    """Upsert an entity (search-before-create). Returns entity id."""
    existing = _db.entities.find_one({
        "subject_id": subject_id, "name": name, "entity_type": entity_type,
    })
    if existing:
        return existing["_id"]

    new_id = get_next_sequence_value("entities")
    try:
        _db.entities.insert_one({
            "_id": new_id,
            "subject_id": subject_id,
            "name": name,
            "entity_type": entity_type,
            "canonical_entity_id": None,
            "created_at": datetime.now(timezone.utc),
        })
    except Exception:
        # Race condition fallback
        existing = _db.entities.find_one({
            "subject_id": subject_id, "name": name, "entity_type": entity_type,
        })
        if existing:
            return existing["_id"]
        raise
    return new_id


def insert_entity_mention(entity_id: int, chunk_id: int, mention_text: str, confidence: float):
    """Record where an entity was mentioned."""
    new_id = get_next_sequence_value("entity_mentions")
    _db.entity_mentions.insert_one({
        "_id": new_id,
        "entity_id": entity_id,
        "chunk_id": chunk_id,
        "mention_text": mention_text,
        "confidence": confidence,
    })


def get_entities(subject_id: int) -> list[dict]:
    """Return all entities for a subject with mention counts."""
    pipeline = [
        {"$match": {"subject_id": subject_id}},
        {
            "$lookup": {
                "from": "entity_mentions",
                "localField": "_id",
                "foreignField": "entity_id",
                "as": "mentions",
            }
        },
        {
            "$project": {
                "name": 1,
                "entity_type": 1,
                "canonical_entity_id": 1,
                "mention_count": {"$size": "$mentions"},
            }
        },
        {"$sort": {"mention_count": -1}},
    ]
    results = []
    for d in _db.entities.aggregate(pipeline):
        results.append({
            "id": d["_id"],
            "name": d["name"],
            "entity_type": d["entity_type"],
            "canonical_entity_id": d.get("canonical_entity_id"),
            "mention_count": d["mention_count"],
        })
    return results


# ---------------------------------------------------------------------------
# Clusters
# ---------------------------------------------------------------------------

def insert_cluster(subject_id: int, label: str) -> int:
    """Create a cluster group."""
    new_id = get_next_sequence_value("clusters")
    _db.clusters.insert_one({
        "_id": new_id,
        "subject_id": subject_id,
        "label": label,
        "created_at": datetime.now(timezone.utc),
    })
    return new_id


def insert_cluster_members(cluster_id: int, chunk_ids: list[int]):
    """Link chunks to a cluster."""
    for cid in chunk_ids:
        try:
            _db.cluster_members.insert_one({
                "cluster_id": cluster_id,
                "chunk_id": cid,
            })
        except Exception:
            pass  # duplicate – ignore


def get_clusters_with_chunks(subject_id: int) -> list[dict]:
    """Return clusters with their chunk texts for synthesis."""
    clusters = list(_db.clusters.find({"subject_id": subject_id}).sort("_id", ASCENDING))

    result = []
    for cluster in clusters:
        members = _db.cluster_members.find({"cluster_id": cluster["_id"]})
        chunk_ids = [m["chunk_id"] for m in members]

        chunks = list(
            _db.chunks.find({"_id": {"$in": chunk_ids}}).sort("_id", ASCENDING)
        )

        result.append({
            "cluster_id": cluster["_id"],
            "label": cluster["label"],
            "chunks": [
                {"id": ch["_id"], "text": ch["chunk_text"], "document_id": ch["document_id"]}
                for ch in chunks
            ],
        })
    return result


# ---------------------------------------------------------------------------
# Relationships  (entity names denormalized into the document)
# ---------------------------------------------------------------------------

def insert_relationship(
    subject_id: int, entity_a_id: int, entity_b_id: int,
    relationship_type: str, description: str = "", confidence: float = 0.5
) -> int:
    """Insert a relationship between two entities, denormalizing entity names."""
    # Look up entity names for denormalization
    ent_a = _db.entities.find_one({"_id": entity_a_id})
    ent_b = _db.entities.find_one({"_id": entity_b_id})

    new_id = get_next_sequence_value("relationships")
    _db.relationships.insert_one({
        "_id": new_id,
        "subject_id": subject_id,
        "entity_a_id": entity_a_id,
        "entity_b_id": entity_b_id,
        "entity_a": ent_a["name"] if ent_a else "Unknown",
        "type_a": ent_a["entity_type"] if ent_a else "other",
        "entity_b": ent_b["name"] if ent_b else "Unknown",
        "type_b": ent_b["entity_type"] if ent_b else "other",
        "relationship_type": relationship_type,
        "description": description,
        "confidence": confidence,
    })
    return new_id


def get_relationships(subject_id: int) -> list[dict]:
    """Return all relationships for a subject with entity names (denormalized)."""
    docs = _db.relationships.find({"subject_id": subject_id}).sort("confidence", DESCENDING)
    return [
        {
            "id": d["_id"],
            "entity_a": d["entity_a"],
            "type_a": d["type_a"],
            "entity_b": d["entity_b"],
            "type_b": d["type_b"],
            "relationship_type": d["relationship_type"],
            "description": d.get("description", ""),
            "confidence": d.get("confidence", 0.5),
        }
        for d in docs
    ]


# ---------------------------------------------------------------------------
# Synthesized notes
# ---------------------------------------------------------------------------

def insert_synthesized_note(
    subject_id: int, title: str, content_md: str,
    source_doc_ids: list[int], entity_id: int | None = None,
) -> int:
    """Insert a synthesized note."""
    now = datetime.now(timezone.utc)
    new_id = get_next_sequence_value("synthesized_notes")

    # Optionally look up entity name for denormalization
    entity_name = None
    entity_type = None
    if entity_id:
        ent = _db.entities.find_one({"_id": entity_id})
        if ent:
            entity_name = ent["name"]
            entity_type = ent["entity_type"]

    _db.synthesized_notes.insert_one({
        "_id": new_id,
        "subject_id": subject_id,
        "entity_id": entity_id,
        "entity_name": entity_name,
        "entity_type": entity_type,
        "title": title,
        "content_md": content_md,
        "source_doc_ids": source_doc_ids,
        "created_at": now,
        "updated_at": now,
    })
    return new_id


def get_synthesized_notes(subject_id: int) -> list[dict]:
    """Return all synthesized notes for a subject."""
    docs = _db.synthesized_notes.find({"subject_id": subject_id}).sort("created_at", ASCENDING)
    return [
        {
            "id": d["_id"],
            "title": d["title"],
            "content_md": d["content_md"],
            "source_doc_ids": d.get("source_doc_ids", []),
            "entity_id": d.get("entity_id"),
            "entity_name": d.get("entity_name"),
            "entity_type": d.get("entity_type"),
            "created_at": d.get("created_at"),
            "updated_at": d.get("updated_at"),
        }
        for d in docs
    ]


# ---------------------------------------------------------------------------
# Pipeline runs (status tracking)
# ---------------------------------------------------------------------------

PIPELINE_STAGES = ["scrape", "chunk", "embed", "ner", "cluster", "synthesize", "vault"]


def init_pipeline_stages(subject_id: int):
    """Create pending documents for all pipeline stages or reset them if they exist."""
    for stage in PIPELINE_STAGES:
        existing = _db.pipeline_runs.find_one({"subject_id": subject_id, "stage": stage})
        if existing:
            _db.pipeline_runs.update_one(
                {"_id": existing["_id"]},
                {"$set": {
                    "status": "pending",
                    "started_at": None,
                    "completed_at": None,
                    "error_message": None,
                }},
            )
        else:
            new_id = get_next_sequence_value("pipeline_runs")
            _db.pipeline_runs.insert_one({
                "_id": new_id,
                "subject_id": subject_id,
                "stage": stage,
                "status": "pending",
                "started_at": None,
                "completed_at": None,
                "error_message": None,
                "metadata": {},
            })


def update_pipeline_stage(subject_id: int, stage: str, status: str, error_message: str = None):
    """Update a pipeline stage status."""
    now = datetime.now(timezone.utc)
    existing = _db.pipeline_runs.find_one(
        {"subject_id": subject_id, "stage": stage},
        sort=[("_id", DESCENDING)],
    )

    if existing:
        update_fields = {"status": status}
        if status == "running":
            update_fields["started_at"] = now
            update_fields["error_message"] = None
        elif status in ("complete", "failed", "skipped"):
            update_fields["completed_at"] = now

        if error_message:
            update_fields["error_message"] = error_message
        elif status == "complete":
            update_fields["error_message"] = None

        _db.pipeline_runs.update_one({"_id": existing["_id"]}, {"$set": update_fields})
    else:
        new_id = get_next_sequence_value("pipeline_runs")
        _db.pipeline_runs.insert_one({
            "_id": new_id,
            "subject_id": subject_id,
            "stage": stage,
            "status": status,
            "started_at": now,
            "completed_at": None,
            "error_message": error_message,
            "metadata": {},
        })


def get_pipeline_status(subject_id: int) -> list[dict]:
    """Return pipeline stage statuses for a subject."""
    docs = _db.pipeline_runs.find({"subject_id": subject_id}).sort("_id", ASCENDING)
    return [
        {
            "stage": d["stage"],
            "status": d["status"],
            "started_at": d.get("started_at"),
            "completed_at": d.get("completed_at"),
            "error_message": d.get("error_message"),
        }
        for d in docs
    ]


# ---------------------------------------------------------------------------
# Stats (for dashboard)
# ---------------------------------------------------------------------------

def get_subject_stats(subject_id: int) -> dict:
    """Return counts for a subject: documents, chunks, entities, notes."""
    scraped_docs = _db.scraped_documents.count_documents({"subject_id": subject_id})
    pdf_docs = _db.pdf_documents.count_documents({"subject_id": subject_id})
    docs = scraped_docs + pdf_docs
    chunks = _db.chunks.count_documents({"subject_id": subject_id})
    entities = _db.entities.count_documents({"subject_id": subject_id})
    notes = _db.synthesized_notes.count_documents({"subject_id": subject_id})
    return {"documents": docs, "chunks": chunks, "entities": entities, "notes": notes}


def get_global_stats() -> dict:
    """Return global counts across all subjects."""
    subjects = _db.subjects.count_documents({})
    scraped = _db.scraped_documents.count_documents({})
    pdfs = _db.pdf_documents.count_documents({})
    docs = scraped + pdfs
    entities = _db.entities.count_documents({})
    notes = _db.synthesized_notes.count_documents({})
    return {"subjects": subjects, "documents": docs, "entities": entities, "notes": notes}


def check_cancellation(subject_id: int) -> bool:
    """Check if the subject pipeline has been cancelled/reset."""
    doc = _db.subjects.find_one({"_id": subject_id})
    if not doc:
        return True  # Subject is deleted, so stop
    return doc.get("cancelled", False)


def delete_subject_data(subject_id: int):
    """Reset/Delete all data associated with a subject to stop the pipeline and start fresh."""
    # 1. Set cancelled flag to stop active threads
    _db.subjects.update_one({"_id": subject_id}, {"$set": {"cancelled": True}})
    
    # 2. Delete entries in all related collections
    _db.sources.delete_many({"subject_id": subject_id})
    _db.scraped_documents.delete_many({"subject_id": subject_id})
    _db.pdf_documents.delete_many({"subject_id": subject_id})
    _db.chunks.delete_many({"subject_id": subject_id})
    _db.entities.delete_many({"subject_id": subject_id})
    _db.relationships.delete_many({"subject_id": subject_id})
    _db.clusters.delete_many({"subject_id": subject_id})
    _db.synthesized_notes.delete_many({"subject_id": subject_id})
    _db.pipeline_runs.delete_many({"subject_id": subject_id})
    
    # 3. Delete the subject itself
    _db.subjects.delete_one({"_id": subject_id})

