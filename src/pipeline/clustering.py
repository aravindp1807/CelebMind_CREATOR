import logging
import json
import re
import time
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
import numpy as np
import umap
from sklearn.cluster import HDBSCAN
from sklearn.feature_extraction.text import TfidfVectorizer
from openai import OpenAI
from src.storage import db

logger = logging.getLogger(__name__)

# Load OpenRouter API key from environment variables
OPENROUTER_KEY = (os.getenv("OPENROUTER_API_KEYS", "") or os.getenv("OPENROUTER_API_KEY", "")).split(",")[0].strip()
OPENROUTER_MODEL = "nvidia/nemotron-nano-9b-v2:free"
DAILY_QUOTA_EXHAUSTED = False


def parse_embedding(embedding_data) -> list[float]:
    """Parse embedding from DB (which can be a string, list, or pgvector format)."""
    if isinstance(embedding_data, list):
        return embedding_data
    if isinstance(embedding_data, str):
        try:
            return json.loads(embedding_data)
        except:
            try:
                s = embedding_data.strip().lstrip('[').rstrip(']')
                return [float(x) for x in s.split(',') if x.strip()]
            except Exception as e:
                logger.error(f"Failed to parse embedding string: {embedding_data[:100]}... Error: {e}")
    return []


def get_representative_chunks(chunks: list[dict], embeddings: list[list[float]], top_k: int = 3) -> list[dict]:
    """
    Select the representative chunks close to the cluster centroid.
    Calculates the centroid (mean embedding vector) and ranks chunks by cosine similarity to it.
    """
    if not chunks:
        return []
    
    # Calculate cluster centroid
    X = np.array(embeddings)
    centroid = np.mean(X, axis=0)
    
    # Calculate cosine similarities to centroid
    norms = np.linalg.norm(X, axis=1)
    centroid_norm = np.linalg.norm(centroid)
    
    if centroid_norm == 0:
        # Avoid division by zero
        similarities = np.zeros(len(chunks))
    else:
        # Since embeddings are unit vectors, norms are generally ~1.0
        # But we divide by norm to be safe
        similarities = np.dot(X, centroid) / (norms * centroid_norm)
    
    # Add similarities to chunks for ranking
    # Make a shallow copy of chunks to avoid modifying in-place unexpectedly
    copied_chunks = [dict(c) for c in chunks]
    for idx, similarity in enumerate(similarities):
        copied_chunks[idx]["similarity"] = float(similarity)
        
    # Sort descending by similarity
    ranked_chunks = sorted(copied_chunks, key=lambda x: x["similarity"], reverse=True)
    return ranked_chunks[:top_k]


def validate_and_name_cluster_with_retry(subject_name: str, representative_chunks: list[dict], max_retries: int = 3) -> dict:
    """
    Call Nvidia Nemotron-3-Ultra-550b on OpenRouter to validate cluster coherence,
    detect outlier chunks, and generate a topic name. Includes retry logic with backoff.
    """
    global DAILY_QUOTA_EXHAUSTED
    
    fallback_res = {
        "is_coherent": True,
        "topic_name": "",
        "outlier_chunk_ids": []
    }
    
    if DAILY_QUOTA_EXHAUSTED:
        return fallback_res
        
    # Format the chunks for prompt
    chunks_input = ""
    for c in representative_chunks:
        chunks_input += f"--- START CHUNK (ID: {c['id']}) ---\n{c['chunk_text']}\n--- END CHUNK ---\n\n"
        
    prompt = f"""You are a data validation and clustering agent. We are building a structured knowledge graph for "{subject_name}".
Below are representative text chunks grouped into a single semantic cluster.

Your task is to analyze these chunks and determine:
1. Whether they are semantically coherent (i.e. discuss the same event, topic, relationship, or concept).
2. If coherent, generate a concise topic name (3-6 words maximum, e.g. "Early Career and Breakthroughs").
3. If there are any outlier chunks that do NOT fit with the primary topic of the other chunks, list their integer IDs.

You must return your response in raw JSON format, containing exactly the following keys:
- "is_coherent": boolean (true if the majority of chunks form a coherent topic, false otherwise)
- "topic_name": string (if coherent, a short title like "Early Life & Education" or "Career Breakthrough", otherwise empty string "")
- "outlier_chunk_ids": array of integers (IDs of any chunks that are unrelated to the main topic)

Do NOT wrap the output in markdown code blocks. Return ONLY the raw JSON object.

Representative chunks:
{chunks_input}
"""

    for attempt in range(max_retries):
        if DAILY_QUOTA_EXHAUSTED:
            return fallback_res
            
        try:
            client = OpenAI(
                base_url="https://openrouter.ai/api/v1",
                api_key=OPENROUTER_KEY,
                default_headers={
                    "HTTP-Referer": "https://github.com/breferrari/obsidian-mind",
                    "X-Title": "Synthetic Brain Graph Pipeline Clustering"
                }
            )
            response = client.chat.completions.create(
                model=OPENROUTER_MODEL,
                messages=[{"role": "user", "content": prompt}],
                timeout=30.0
            )
            
            if not response or not response.choices:
                raise ValueError("Empty or invalid choices list returned from OpenRouter.")
                
            content = response.choices[0].message.content
            if not content:
                raise ValueError("Empty response message content.")
                
            content = content.strip()
            logger.info(f"OpenRouter LLM Response (attempt {attempt + 1}): {content}")
            
            # Clean response content if model wrapped it in markdown code blocks
            if content.startswith("```"):
                content = re.sub(r"^```(?:json)?\s*", "", content)
                content = re.sub(r"```$", "", content).strip()
                
            # Find first '{' and last '}' to handle any extra text from the LLM
            start_idx = content.find('{')
            end_idx = content.rfind('}')
            if start_idx != -1 and end_idx != -1:
                content = content[start_idx:end_idx + 1]
                
            # Parse JSON
            result = json.loads(content)
            return {
                "is_coherent": bool(result.get("is_coherent", True)),
                "topic_name": str(result.get("topic_name", "")).strip(),
                "outlier_chunk_ids": [int(x) for x in result.get("outlier_chunk_ids", [])]
            }
        except Exception as e:
            err_str = str(e)
            logger.warning(f"Attempt {attempt + 1} failed for cluster validation: {e}")
            
            # Check if this is a daily quota exhaustion
            if "free-models-per-day" in err_str or "free-models-per-min" in err_str or "limit_rpm" in err_str:
                logger.error("OpenRouter free model rate limit or daily quota reached. Short-circuiting future calls to fallback.")
                DAILY_QUOTA_EXHAUSTED = True
                return fallback_res
                
            if attempt < max_retries - 1:
                time.sleep(2 * (attempt + 1))  # Exponential backoff
            else:
                logger.error(f"All retries failed for cluster validation. Using fallback.")
                
    return fallback_res


def get_local_topic_name(chunks: list[dict]) -> str:
    """Extract descriptive keywords locally using TF-IDF when LLM is unavailable."""
    if not chunks:
        return "General Topic"
    
    texts = [c["chunk_text"] for c in chunks]
    
    try:
        # TfidfVectorizer with English stop words
        vectorizer = TfidfVectorizer(stop_words='english', ngram_range=(1, 2))
        tfidf_matrix = vectorizer.fit_transform(texts)
        
        # Sum tf-idf scores for each term across all chunks in the cluster
        sums = tfidf_matrix.sum(axis=0)
        
        # Get feature names and their total scores
        feature_names = vectorizer.get_feature_names_out()
        data = []
        for col, term in enumerate(feature_names):
            data.append((term, sums[0, col]))
            
        # Sort terms by score descending
        ranked = sorted(data, key=lambda x: x[1], reverse=True)
        
        # Select top terms that are not pure digits
        top_terms = []
        for term, score in ranked:
            if not term.isdigit() and len(term) > 2:
                capitalized = " ".join([w.capitalize() for w in term.split()])
                top_terms.append(capitalized)
                if len(top_terms) >= 3:
                    break
                    
        if top_terms:
            return " & ".join(top_terms)
    except Exception as e:
        logger.warning(f"Failed to generate TF-IDF topic name: {e}")
        
    # Fallback to simple truncation if TF-IDF fails
    first_text = chunks[0]["chunk_text"]
    return first_text[:40].replace("\n", " ").strip() + "..."


def run_clustering(subject_id: int) -> int:
    """Run the advanced UMAP + HDBSCAN + Nemotron clustering pipeline."""
    db.update_pipeline_stage(subject_id, "cluster", "running")
    
    try:
        # Get the subject name
        subject = db.get_subject(subject_id)
        subject_name = subject["name"] if subject else "Subject"
        
        # Get chunks with embeddings
        chunks = db.get_chunks_for_subject(subject_id, with_embedding=True)
        logger.info(f"Found {len(chunks)} chunks for subject {subject_id}")
        
        # Filter chunks that actually have embeddings
        valid_chunks = []
        embeddings = []
        for c in chunks:
            if c.get("embedding"):
                emb = parse_embedding(c["embedding"])
                if emb:
                    valid_chunks.append(c)
                    embeddings.append(emb)
                    
        N = len(valid_chunks)
        if N == 0:
            logger.warning("No chunks with embeddings found. Skipping clustering.")
            db.update_pipeline_stage(subject_id, "cluster", "skipped", "No chunks with embeddings available.")
            return 0
            
        # If we have only 1 chunk, HDBSCAN cannot run. Treat as a singleton.
        if N < 2:
            logger.info("Fewer than 2 chunks available. Grouping all as singletons.")
            labels = [-1] * N
        else:
            # Step 1: UMAP reduction if N > 5
            X = np.array(embeddings)
            if N > 5:
                # n_neighbors must be at least 2 and less than N
                n_neighbors = max(2, min(15, N - 1))
                logger.info(f"Running UMAP reduction to 5 components (n_neighbors={n_neighbors}).")
                reducer = umap.UMAP(
                    n_neighbors=n_neighbors,
                    n_components=5,
                    metric='cosine',
                    random_state=42
                )
                X_reduced = reducer.fit_transform(X)
            else:
                logger.info(f"Chunk count {N} <= 5. Bypassing UMAP reduction.")
                X_reduced = X
                
            # Step 2: HDBSCAN Clustering
            logger.info("Running HDBSCAN clustering.")
            # min_cluster_size=2 ensures we form clusters of size >= 2
            hdb = HDBSCAN(min_cluster_size=2, min_samples=1, metric='euclidean')
            labels = hdb.fit_predict(X_reduced).tolist()
            
        # Group chunks by their HDBSCAN label
        # Labels >= 0 are valid cluster candidates. Label -1 is noise.
        cluster_groups = {}
        noise_chunks = []
        
        for idx, label in enumerate(labels):
            chunk = valid_chunks[idx]
            emb = embeddings[idx]
            
            if label == -1:
                noise_chunks.append(chunk)
            else:
                if label not in cluster_groups:
                    cluster_groups[label] = {"chunks": [], "embeddings": []}
                cluster_groups[label]["chunks"].append(chunk)
                cluster_groups[label]["embeddings"].append(emb)
                
        # We will keep track of all final clusters to be created
        final_clusters = []
        
        # Step 3 & 4: Process cluster candidates with LLM validation in parallel
        # OpenRouter free rate limit is 15 RPM. Max 3 concurrent workers to be safe.
        max_workers = 3
        results_map = {}
        
        logger.info(f"Validating {len(cluster_groups)} clusters in parallel using ThreadPoolExecutor (max_workers={max_workers}).")
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_label = {}
            for label_id, group in cluster_groups.items():
                grp_chunks = group["chunks"]
                grp_embs = group["embeddings"]
                
                # Extract representative chunks (top 3 closest to centroid)
                rep_chunks = get_representative_chunks(grp_chunks, grp_embs, top_k=3)
                
                # Submit validation task to pool
                future = executor.submit(
                    validate_and_name_cluster_with_retry,
                    subject_name,
                    rep_chunks
                )
                future_to_label[future] = (label_id, grp_chunks)
                
            # Gather results as they complete
            for future in as_completed(future_to_label):
                label_id, grp_chunks = future_to_label[future]
                try:
                    validation = future.result()
                    results_map[label_id] = validation
                except Exception as e:
                    logger.error(f"Error processing future result for cluster {label_id}: {e}")
                    results_map[label_id] = {
                        "is_coherent": True,
                        "topic_name": "",
                        "outlier_chunk_ids": []
                    }
                    
        # Step 5: Process validated results and partition outliers
        for label_id, group in cluster_groups.items():
            grp_chunks = group["chunks"]
            validation = results_map.get(label_id, {
                "is_coherent": True,
                "topic_name": "",
                "outlier_chunk_ids": []
            })
            
            if not validation["is_coherent"]:
                logger.info(f"Cluster label {label_id} marked as incoherent. Dissolving to singletons.")
                noise_chunks.extend(grp_chunks)
                continue
                
            # Filter out outliers if any
            outliers = set(validation["outlier_chunk_ids"])
            remaining_chunks = []
            for c in grp_chunks:
                if c["id"] in outliers:
                    logger.info(f"Removing outlier chunk ID {c['id']} from cluster.")
                    noise_chunks.append(c)
                else:
                    remaining_chunks.append(c)
                    
            if not remaining_chunks:
                continue
                
            # Use topic name or fallback local TF-IDF keyword extraction
            topic_name = validation["topic_name"]
            if not topic_name:
                topic_name = get_local_topic_name(remaining_chunks)
                
            final_clusters.append({
                "label": topic_name,
                "chunks": remaining_chunks
            })
            
        # Process all singletons (noise chunks + LLM identified outliers)
        for chunk in noise_chunks:
            chunk_text = chunk["chunk_text"]
            label = chunk_text[:60].replace("\n", " ") + "..."
            final_clusters.append({
                "label": label,
                "chunks": [chunk]
            })
            
        # Step 6: Write to MongoDB
        cluster_count = 0
        for cluster in final_clusters:
            cluster_id = db.insert_cluster(subject_id, cluster["label"])
            chunk_ids = [c["id"] for c in cluster["chunks"]]
            db.insert_cluster_members(cluster_id, chunk_ids)
            cluster_count += 1
            
        db.update_pipeline_stage(subject_id, "cluster", "complete")
        logger.info(f"Clustering complete. Created {cluster_count} clusters (including singletons).")
        return cluster_count
        
    except Exception as e:
        logger.error(f"Error during clustering stage for subject {subject_id}: {e}", exc_info=True)
        db.update_pipeline_stage(subject_id, "cluster", "failed", str(e))
        raise e
