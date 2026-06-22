import os
import re
import logging
from src.storage import db

logger = logging.getLogger(__name__)

_nlp = None

SPACY_LABEL_MAP = {
    "PERSON": "person",
    "ORG": "organization",
    "GPE": "place",
    "LOC": "place",
    "WORK_OF_ART": "work",
    "EVENT": "event",
    "FAC": "place",
    "NORP": "organization"
}


def get_nlp():
    """Lazily load the spaCy model."""
    global _nlp
    if _nlp is not None:
        return _nlp

    model_name = os.getenv("SPACY_MODEL", "en_core_web_sm")
    try:
        import spacy
        logger.info(f"Loading spaCy model: {model_name}")
        _nlp = spacy.load(model_name)
    except Exception as e:
        logger.warning(f"Could not load spaCy model {model_name} ({e}). Using regex fallback for NER.")
        _nlp = "fallback"
    return _nlp


def regex_fallback_ner(text: str) -> list[dict]:
    """A heuristic regex-based NER as fallback for when spaCy is unavailable."""
    entities = []
    # Match sequences of capitalized words, e.g. "John Smith", "New York City", "Warner Bros"
    # Excluding lowercase joiners like 'of', 'and'
    pattern = r'\b([A-Z][a-zA-Z]+(?:\s+(?:of|and|the|in)\s+[A-Z][a-zA-Z]+|\s+[A-Z][a-zA-Z]+)*)\b'
    
    stop_words = {
        "the", "a", "an", "this", "that", "these", "those", "here", "there", 
        "when", "where", "why", "how", "he", "she", "it", "they", "we", "i", "you",
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
        "january", "february", "march", "april", "may", "june", "july", "august", "september",
        "october", "november", "december"
    }
    
    for match in re.finditer(pattern, text):
        name = match.group(1).strip()
        if name.lower() in stop_words or len(name) <= 2:
            continue
            
        # Basic heuristic to categorize
        name_lower = name.lower()
        if any(x in name_lower for x in ["corp", "inc", "ltd", "co.", "company", "studio", "records", "university", "association", "band"]):
            etype = "organization"
            label = "ORG"
        elif any(x in name_lower for x in ["city", "state", "country", "river", "ocean", "street", "road", "london", "york", "paris", "california", "america", "united"]):
            etype = "place"
            label = "GPE"
        elif any(x in name_lower for x in ["album", "movie", "film", "book", "song", "novel", "symphony", "painting"]):
            etype = "work"
            label = "WORK_OF_ART"
        elif any(x in name_lower for x in ["war", "battle", "festival", "show", "awards", "olympics", "conspiracy"]):
            etype = "event"
            label = "EVENT"
        else:
            etype = "person"
            label = "PERSON"
            
        entities.append({
            "name": name,
            "entity_type": etype,
            "start": match.start(),
            "end": match.end(),
            "label": label
        })
    return entities


def extract_entities(text: str) -> list[dict]:
    """Extract entities from text and return a list of entity dicts."""
    nlp = get_nlp()
    if nlp == "fallback":
        return regex_fallback_ner(text)
        
    try:
        doc = nlp(text)
        entities = []
        for ent in doc.ents:
            if ent.label_ in SPACY_LABEL_MAP:
                entities.append({
                    "name": ent.text.strip(),
                    "entity_type": SPACY_LABEL_MAP[ent.label_],
                    "start": ent.start_char,
                    "end": ent.end_char,
                    "label": ent.label_
                })
        return entities
    except Exception as e:
        logger.error(f"Error running spaCy NER: {e}. Falling back to regex.")
        return regex_fallback_ner(text)


def run_ner(subject_id: int) -> int:
    """Run named entity recognition on all chunks of a subject."""
    db.update_pipeline_stage(subject_id, "ner", "running")
    
    try:
        # Get chunks for the subject
        chunks = db.get_chunks_for_subject(subject_id, with_embedding=False)
        logger.info(f"Processing NER for {len(chunks)} chunks of subject {subject_id}")
        
        entity_mentions_count = 0
        inserted_entities = set() # Track unique within this run for logging
        
        for chunk in chunks:
            if db.check_cancellation(subject_id):
                logger.info(f"NER stage cancelled early for subject: {subject_id}")
                db.update_pipeline_stage(subject_id, "ner", "failed", "NER stage cancelled by user.")
                return len(inserted_entities)
                
            entities = extract_entities(chunk["chunk_text"])
            for ent in entities:
                # Insert the entity (search-before-create upsert)
                entity_id = db.insert_entity(
                    subject_id=subject_id,
                    name=ent["name"],
                    entity_type=ent["entity_type"]
                )
                
                # Insert mention
                db.insert_entity_mention(
                    entity_id=entity_id,
                    chunk_id=chunk["id"],
                    mention_text=ent["name"],
                    confidence=0.9 if get_nlp() != "fallback" else 0.6
                )
                entity_mentions_count += 1
                inserted_entities.add((ent["name"], ent["entity_type"]))
                
        db.update_pipeline_stage(subject_id, "ner", "complete")
        logger.info(f"NER stage complete. Recorded {entity_mentions_count} mentions for {len(inserted_entities)} unique entities.")
        return len(inserted_entities)
        
    except Exception as e:
        logger.error(f"Error during NER stage for subject {subject_id}: {e}")
        db.update_pipeline_stage(subject_id, "ner", "failed", str(e))
        raise e
