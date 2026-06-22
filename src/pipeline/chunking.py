import logging
from src.storage import db

logger = logging.getLogger(__name__)

def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """
    Recursively split text into chunks of at most chunk_size characters.
    Order of delimiters: paragraphs (\n\n), lines (\n), sentences (. ), words ( ).
    If all else fails, uses hard character limit with overlap.
    """
    if not text:
        return []

    def split_recursive(subtext: str, delimiters: list[str]) -> list[str]:
        if len(subtext) <= chunk_size:
            return [subtext]
            
        if not delimiters:
            # Character splitting fallback with overlap
            chunks = []
            start = 0
            while start < len(subtext):
                end = min(start + chunk_size, len(subtext))
                chunks.append(subtext[start:end])
                if end == len(subtext):
                    break
                start = end - overlap
                if start >= end:
                    start = end - 1
            return chunks

        delim = delimiters[0]
        parts = subtext.split(delim)
        
        current_chunk = ""
        chunks = []
        for part in parts:
            if not part:
                continue
                
            if len(part) > chunk_size:
                # Flush what we have
                if current_chunk:
                    chunks.append(current_chunk.strip())
                    current_chunk = ""
                # Recursively split the long part
                chunks.extend(split_recursive(part, delimiters[1:]))
            else:
                separator = delim if current_chunk else ""
                if len(current_chunk) + len(separator) + len(part) <= chunk_size:
                    current_chunk += separator + part
                else:
                    if current_chunk:
                        chunks.append(current_chunk.strip())
                    current_chunk = part

        if current_chunk:
            chunks.append(current_chunk.strip())
            
        return chunks

    return split_recursive(text, ["\n\n", "\n", ". ", " "])


def run_chunking(subject_id: int) -> int:
    """Run text chunking on all unchunked documents for a subject."""
    db.update_pipeline_stage(subject_id, "chunk", "running")
    
    try:
        unchunked_docs = db.get_unchunked_documents(subject_id)
        logger.info(f"Found {len(unchunked_docs)} unchunked documents for subject {subject_id}")
        
        total_chunks = 0
        for doc in unchunked_docs:
            chunks = chunk_text(doc["raw_text"])
            for idx, chunk in enumerate(chunks):
                db.insert_chunk(doc["id"], subject_id, idx, chunk)
                total_chunks += 1
                
        db.update_pipeline_stage(subject_id, "chunk", "complete")
        logger.info(f"Chunking stage complete. Created {total_chunks} chunks.")
        return total_chunks
        
    except Exception as e:
        logger.error(f"Error during chunking for subject {subject_id}: {e}")
        db.update_pipeline_stage(subject_id, "chunk", "failed", str(e))
        raise e
