import os
import re
import json
import time
import logging
from openai import OpenAI
from src.storage import db

logger = logging.getLogger(__name__)

FALLBACK_MODELS = [
    'nvidia/nemotron-3-ultra-550b-a55b:free',
    'google/gemini-2.5-flash',
    'meta-llama/llama-3-70b-instruct',
]


def _get_api_keys() -> list[str]:
    """Return the list of OpenRouter API keys (comma-separated env var)."""
    raw = os.getenv("OPENROUTER_API_KEYS", "") or os.getenv("OPENROUTER_API_KEY", "")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    return keys


def get_llm_client() -> OpenAI | None:
    """Initialize OpenAI client configured for OpenRouter using the first available key."""
    keys = _get_api_keys()
    if not keys:
        return None
    return _make_client(keys[0])


def _make_client(api_key: str) -> OpenAI:
    """Create an OpenAI client pointed at OpenRouter."""
    return OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
        default_headers={
            "HTTP-Referer": "https://github.com/breferrari/obsidian-mind",
            "X-Title": "Synthetic Brain Graph Pipeline"
        }
    )


CREDITS_EXHAUSTED = False


def _call_with_key_rotation(messages: list[dict], model: str | None = None) -> str | None:
    """
    Call OpenRouter with automatic key rotation on 429 rate-limit errors.
    Returns the response text or None on total failure.
    """
    global CREDITS_EXHAUSTED
    if CREDITS_EXHAUSTED:
        return None

    if model is None:
        model = os.getenv("SYNTHESIS_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free")

    keys = _get_api_keys()
    if not keys:
        logger.warning("No API keys available for LLM call.")
        return None

    last_error = None
    for key in keys:
        client = _make_client(key)
        try:
            completion = client.chat.completions.create(
                model=model,
                messages=messages,
                extra_body={
                    "models": FALLBACK_MODELS,
                    "route": "fallback"
                }
            )
            return completion.choices[0].message.content.strip()
        except Exception as e:
            last_error = e
            status_code = getattr(e, "status_code", None)
            if status_code == 429:
                logger.warning(f"Rate limited on key ...{key[-6:]}. Trying next key.")
                time.sleep(1)
                continue
            elif status_code == 402:
                logger.error(f"Credits exhausted (402) on key ...{key[-6:]}. Short-circuiting synthesis.")
                CREDITS_EXHAUSTED = True
                break
            logger.error(f"LLM call error: {e}")
            break

    logger.error(f"All LLM keys exhausted or error: {last_error}")
    return None


def parse_json_array(text: str) -> list:
    """Extract and parse the first JSON array found in a string."""
    try:
        start = text.find('[')
        end = text.rfind(']')
        if start != -1 and end != -1:
            json_str = text[start:end+1]
            return json.loads(json_str)
        return json.loads(text)
    except Exception as e:
        logger.warning(f"JSON parsing error: {e}. Raw response: {text}")
        return []


def parse_synthesis_response(response_text: str) -> tuple[str, str]:
    """Parse title and content from synthesized response."""
    title = "Synthesized Facts"
    content = response_text
    
    # Try parsing "Title: ..." and "Content: ..." formats
    if "Title:" in response_text:
        try:
            parts = response_text.split("Content:", 1)
            title_part = parts[0]
            if len(parts) > 1:
                content = parts[1].strip()
            else:
                content = response_text
                
            title_match = re.search(r'Title:\s*(.*)', title_part)
            if title_match:
                title = title_match.group(1).strip().replace('"', '').replace('*', '')
        except Exception as e:
            logger.debug(f"Failed to split Title/Content: {e}")
            
    # Clean markdown block wrappers if LLM returned them
    content = re.sub(r'^```markdown\s*', '', content)
    content = re.sub(r'```$', '', content).strip()
    return title, content


def synthesize_cluster(client: OpenAI, chunks: list[dict], subject_name: str) -> dict:
    """Ask LLM to synthesize a cluster of chunks into a single coherent note with citations."""
    # Format chunks with their source document IDs
    chunks_input = ""
    for c in chunks:
        chunks_input += f"--- START CHUNK (Doc ID: {c['document_id']}) ---\n{c['text']}\n--- END CHUNK ---\n\n"
        
    prompt = f"""You are an expert information synthesizer. We are building a knowledge graph about the subject "{subject_name}".
Below are several overlapping, raw text chunks scraped from different sources.
Your task is to merge them into a single, clean, and structured markdown note.

Rules:
1. Merge duplicate facts and present them clearly.
2. Maintain strict factuality. Do not invent details.
3. Every claim MUST cite the source document ID(s) it came from using the format "[Source: doc_id]", e.g. "[Source: 12]" or "[Source: 12, 15]".
4. If there are contradictions or conflicting claims among the sources, explicitly highlight them under a "## Contradictions" section.
5. Provide a concise, descriptive title for this note.

Your output must be formatted EXACTLY as:
Title: [Concise descriptive title of this note]
Content:
[Markdown contents with headings, bullet points, and citations]

Here are the text chunks:
{chunks_input}
"""

    try:
        response_text = _call_with_key_rotation(
            messages=[{"role": "user", "content": prompt}],
        )
        if response_text:
            title, content_md = parse_synthesis_response(response_text)
            return {"title": title, "content_md": content_md}
        raise RuntimeError("LLM returned no response")
    except Exception as e:
        logger.error(f"Error synthesizing cluster: {e}")
        # Return fallback note
        first_text = chunks[0]["text"]
        title = f"Synthesis of Cluster (Doc {chunks[0]['document_id']})"
        content_md = f"### Automatically Synthesized Note\n\n{first_text}\n\n[Source: {chunks[0]['document_id']}]"
        return {"title": title, "content_md": content_md}


def get_local_relationships(content: str, entities: list[dict], subject_name: str) -> list[dict]:
    """Fallback local relationship extractor when LLM is unavailable."""
    relationships = []
    seen = set()
    
    # Split content into sentences/lines
    sentences = []
    for line in content.split('\n'):
        line_clean = line.strip()
        if not line_clean:
            continue
        # simple sentence splitting
        for s in re.split(r'(?<=[.!?])\s+', line_clean):
            if s.strip():
                sentences.append(s.strip())
                
    for sentence in sentences:
        # Find which entities are mentioned in this sentence
        mentioned = []
        for ent in entities:
            name = ent["name"]
            if len(name) > 2 and name.lower() in sentence.lower():
                mentioned.append(name)
                
        # 1. Subject to Entity relationships
        for name in mentioned:
            # Skip if same as subject
            if name.lower() == subject_name.lower():
                continue
                
            key = tuple(sorted([subject_name.lower(), name.lower()]))
            if key not in seen:
                seen.add(key)
                relationships.append({
                    "entity_a": subject_name,
                    "entity_b": name,
                    "relationship_type": "associated",
                    "description": f"Mentioned in: \"{sentence[:100]}...\"",
                    "confidence": 0.5
                })
                
        # 2. Entity to Entity relationships (multi-node)
        if len(mentioned) >= 2:
            for i in range(len(mentioned)):
                for j in range(i + 1, len(mentioned)):
                    ent_a = mentioned[i]
                    ent_b = mentioned[j]
                    if ent_a.lower() == ent_b.lower():
                        continue
                    key = tuple(sorted([ent_a.lower(), ent_b.lower()]))
                    if key not in seen:
                        seen.add(key)
                        relationships.append({
                            "entity_a": ent_a,
                            "entity_b": ent_b,
                            "relationship_type": "associated",
                            "description": f"Co-mentioned in: \"{sentence[:100]}...\"",
                            "confidence": 0.5
                        })
                        
    return relationships


def extract_relationships(client: OpenAI, content: str, entities: list[dict], subject_name: str) -> list[dict]:
    """Ask LLM to extract relationships between the subject and listed entities based on synthesized text."""
    entities_list = "\n".join([f"- {e['name']} ({e['entity_type']})" for e in entities])
    
    prompt = f"""You are a relationship extraction agent.
Below is a synthesized document about "{subject_name}".
Your goal is to extract relationships between "{subject_name}" and the other entities listed below that are mentioned in the text, AS WELL AS relationships between the other entities themselves based on the text.

Document content:
{content}

Entities of interest:
{entities_list}

For each relationship found, output a JSON array of objects with the fields:
- entity_a: The name of the first entity (could be "{subject_name}" or any entity from the list)
- entity_b: The name of the second entity (could be "{subject_name}" or any entity from the list)
- relationship_type: One word category (e.g. spouse, costar, teammate, opponent, coach, manager, partner, parent, child, employer, colleague, creator, member, award, location)
- description: Brief description of their relation (1 sentence)
- confidence: Float between 0.0 and 1.0

Return ONLY the raw JSON array. No markdown code blocks, no headers, no intro text.
"""

    try:
        response_text = _call_with_key_rotation(
            messages=[{"role": "user", "content": prompt}],
        )
        if response_text:
            parsed = parse_json_array(response_text)
            if parsed:
                return parsed
        logger.warning("LLM relationship extraction returned no response. Falling back to local mention-based extraction.")
        return get_local_relationships(content, entities, subject_name)
    except Exception as e:
        logger.error(f"Error extracting relationships: {e}. Falling back to local extraction.")
        return get_local_relationships(content, entities, subject_name)


def run_synthesis(subject_id: int) -> int:
    """Run synthesis stage to merge clusters and extract relationships."""
    db.update_pipeline_stage(subject_id, "synthesize", "running")
    
    client = get_llm_client()
    if not client:
        logger.warning("OPENROUTER_API_KEYS is not set. Skipping synthesis stage.")
        db.update_pipeline_stage(subject_id, "synthesize", "skipped", "OPENROUTER_API_KEYS is missing.")
        return 0
        
    try:
        subject = db.get_subject(subject_id)
        if not subject:
            raise ValueError(f"Subject with ID {subject_id} not found.")
            
        subject_name = subject["name"]
        clusters = db.get_clusters_with_chunks(subject_id)
        logger.info(f"Found {len(clusters)} clusters to synthesize for subject {subject_name} ({subject_id})")
        
        if not clusters:
            db.update_pipeline_stage(subject_id, "synthesize", "complete")
            return 0
            
        # Get all entities for context mapping
        entities = db.get_entities(subject_id)
        
        note_count = 0
        for idx, cluster in enumerate(clusters):
            if db.check_cancellation(subject_id):
                logger.info(f"Synthesis stage cancelled early for subject: {subject_id}")
                db.update_pipeline_stage(subject_id, "synthesize", "failed", "Synthesis stage cancelled by user.")
                return note_count
                
            chunks = cluster["chunks"]
            if not chunks:
                continue

            # Sleep 2s between cluster synthesis calls (except before the first)
            if idx > 0 and not CREDITS_EXHAUSTED:
                time.sleep(2)

            # Perform synthesis
            synthesized = synthesize_cluster(client, chunks, subject_name)
            
            # Source doc IDs collection
            source_doc_ids = list(set(c["document_id"] for c in chunks))
            
            # Check if this cluster is closely related to a specific entity
            entity_id = None
            for ent in entities:
                # If entity name is in the title, associate it
                if ent["name"].lower() in synthesized["title"].lower():
                    entity_id = ent["id"]
                    break
            
            # Save synthesized note
            note_id = db.insert_synthesized_note(
                subject_id=subject_id,
                title=synthesized["title"],
                content_md=synthesized["content_md"],
                source_doc_ids=source_doc_ids,
                entity_id=entity_id
            )
            note_count += 1
            
            # Extract relationships from the note
            relationships = extract_relationships(client, synthesized["content_md"], entities, subject_name)
            
            for rel in relationships:
                try:
                    ent_a_name = rel.get("entity_a")
                    ent_b_name = rel.get("entity_b")
                    if not ent_a_name or not ent_b_name:
                        continue
                        
                    # Find type for entity_a (normalize case and entity type)
                    ent_a_type = "person"
                    if ent_a_name.lower() == subject_name.lower():
                        ent_a_name = subject_name
                    else:
                        for ent in entities:
                            if ent["name"].lower() == ent_a_name.lower():
                                ent_a_type = ent["entity_type"]
                                ent_a_name = ent["name"]
                                break
                                
                    # Find type for entity_b (normalize case and entity type)
                    ent_b_type = "person"
                    if ent_b_name.lower() == subject_name.lower():
                        ent_b_name = subject_name
                    else:
                        for ent in entities:
                            if ent["name"].lower() == ent_b_name.lower():
                                ent_b_type = ent["entity_type"]
                                ent_b_name = ent["name"]
                                break
                                
                    ent_a_id = db.insert_entity(subject_id, ent_a_name, ent_a_type)
                    ent_b_id = db.insert_entity(subject_id, ent_b_name, ent_b_type)
                    
                    db.insert_relationship(
                        subject_id=subject_id,
                        entity_a_id=ent_a_id,
                        entity_b_id=ent_b_id,
                        relationship_type=rel.get("relationship_type", "associated"),
                        description=rel.get("description", ""),
                        confidence=float(rel.get("confidence", 0.5))
                    )
                except Exception as ex:
                    logger.warning(f"Failed to record relationship: {ex}")
                    
        db.update_pipeline_stage(subject_id, "synthesize", "complete")
        logger.info(f"Synthesis stage complete. Created {note_count} synthesized notes.")
        return note_count
        
    except Exception as e:
        logger.error(f"Error in synthesis stage for subject {subject_id}: {e}")
        db.update_pipeline_stage(subject_id, "synthesize", "failed", str(e))
        raise e
