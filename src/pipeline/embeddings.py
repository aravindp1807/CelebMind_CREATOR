import os
import hashlib
import random
import logging
from src.storage import db

logger = logging.getLogger(__name__)

_model = None

def get_model():
    """Lazily load the SentenceTransformer model."""
    global _model
    if _model is not None:
        return _model

    model_name = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
    try:
        from sentence_transformers import SentenceTransformer
        logger.info(f"Loading SentenceTransformer model: {model_name}")
        _model = SentenceTransformer(model_name)
    except Exception as e:
        logger.warning(f"Could not load SentenceTransformer ({e}). Using deterministic mock embeddings (384-dim) fallback.")
        _model = "mock"
    return _model


def get_mock_embedding(text: str) -> list[float]:
    """Generate a deterministic 384-dimensional normalized unit vector based on the text hash."""
    h = int(hashlib.sha256(text.encode('utf-8')).hexdigest(), 16) % (10**8)
    rng = random.Random(h)
    # Generate 384 dimensions
    vec = [rng.uniform(-1, 1) for _ in range(384)]
    # Normalize to unit vector for cosine distance
    norm = sum(x**2 for x in vec) ** 0.5
    if norm > 0:
        vec = [x / norm for x in vec]
    return vec


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Generate embeddings for a list of texts."""
    model = get_model()
    if model == "mock":
        return [get_mock_embedding(t) for t in texts]
        
    try:
        embeddings = model.encode(texts, convert_to_numpy=True)
        return [vec.tolist() for vec in embeddings]
    except Exception as e:
        logger.error(f"Error encoding texts: {e}. Falling back to mock embeddings.")
        return [get_mock_embedding(t) for t in texts]


def run_embeddings(subject_id: int) -> int:
    """Embed all unembedded chunks for a subject."""
    db.update_pipeline_stage(subject_id, "embed", "running")
    
    try:
        unembedded = db.get_unembedded_chunks(subject_id)
        logger.info(f"Found {len(unembedded)} unembedded chunks for subject {subject_id}")
        
        if not unembedded:
            db.update_pipeline_stage(subject_id, "embed", "complete")
            return 0
            
        # Batch process in sizes of 64
        batch_size = 64
        count = 0
        
        for i in range(0, len(unembedded), batch_size):
            if db.check_cancellation(subject_id):
                logger.info(f"Embeddings stage cancelled early for subject: {subject_id}")
                db.update_pipeline_stage(subject_id, "embed", "failed", "Embedding stage cancelled by user.")
                return count
                
            batch = unembedded[i:i+batch_size]
            texts = [c["chunk_text"] for c in batch]
            
            embeddings = embed_texts(texts)
            
            for chunk, emb in zip(batch, embeddings):
                db.update_chunk_embedding(chunk["id"], emb)
                count += 1
                
        db.update_pipeline_stage(subject_id, "embed", "complete")
        logger.info(f"Embeddings stage complete. Processed {count} chunks.")
        return count
        
    except Exception as e:
        logger.error(f"Error during embedding stage for subject {subject_id}: {e}")
        db.update_pipeline_stage(subject_id, "embed", "failed", str(e))
        raise e
