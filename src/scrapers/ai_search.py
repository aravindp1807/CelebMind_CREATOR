"""
AI-powered URL discovery and scraping for the synthetic brain project.
Uses OpenRouter (google/gemini-2.5-flash) to discover relevant public URLs,
then scrapes the discovered pages and stores them via db.insert_raw_document.
"""
import hashlib
import json
import logging
import os
import time

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI

from src.storage import db

load_dotenv()
logger = logging.getLogger(__name__)


def _get_api_keys() -> list[str]:
    """Return the list of OpenRouter API keys (comma-separated env var)."""
    raw = os.getenv("OPENROUTER_API_KEYS", "") or os.getenv("OPENROUTER_API_KEY", "")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    return keys


def _make_client(api_key: str) -> OpenAI:
    """Create an OpenAI client pointed at OpenRouter."""
    return OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
        default_headers={
            "HTTP-Referer": "https://github.com/breferrari/obsidian-mind",
            "X-Title": "Synthetic Brain AI Search",
        },
    )


def discover_urls(subject_name: str) -> list[dict]:
    """
    Use OpenRouter LLM to discover 5-10 public URLs relevant to the subject.

    Returns a list of dicts:
        [{"url": "...", "description": "...", "source_type": "ai_search"}, ...]
    """
    keys = _get_api_keys()
    if not keys:
        logger.warning("No OPENROUTER_API_KEYS set. Cannot discover URLs via AI.")
        return []

    prompt = f"""You are a research assistant. I need you to find 5 to 10 publicly accessible URLs
that contain substantive information about "{subject_name}".

Rules:
1. Only include real, publicly accessible URLs that are likely to exist.
2. Prefer authoritative sources: Wikipedia, major news outlets, IMDb, official sites.
3. Do NOT invent or hallucinate URLs. Only suggest URLs you are confident exist.
4. Return ONLY a raw JSON array with no other text, no markdown code fences.

Each element must have:
- "url": the full URL string
- "description": a one-sentence description of what this page contains

Example output:
[
  {{"url": "https://en.wikipedia.org/wiki/Example", "description": "Wikipedia article about Example"}}
]
"""

    last_error = None
    for key in keys:
        client = _make_client(key)
        try:
            completion = client.chat.completions.create(
                model="google/gemini-2.5-flash",
                messages=[{"role": "user", "content": prompt}],
            )
            response_text = completion.choices[0].message.content.strip()

            # Parse JSON array from response
            # Strip markdown fences if present
            if response_text.startswith("```"):
                lines = response_text.split("\n")
                lines = [l for l in lines if not l.strip().startswith("```")]
                response_text = "\n".join(lines)

            start = response_text.find("[")
            end = response_text.rfind("]")
            if start != -1 and end != -1:
                response_text = response_text[start : end + 1]

            urls = json.loads(response_text)
            # Enrich each with source_type
            result = []
            for item in urls:
                if isinstance(item, dict) and "url" in item:
                    result.append({
                        "url": item["url"],
                        "description": item.get("description", ""),
                        "source_type": "ai_search",
                    })
            logger.info(f"AI search discovered {len(result)} URLs for '{subject_name}'")
            return result

        except Exception as e:
            last_error = e
            status_code = getattr(e, "status_code", None)
            if status_code == 429:
                logger.warning(f"Rate limited on key ...{key[-6:]}. Trying next key.")
                time.sleep(1)
                continue
            logger.error(f"AI URL discovery error: {e}")
            break

    logger.error(f"AI URL discovery failed for '{subject_name}': {last_error}")
    return []


def scrape_discovered_url(url: str, subject_name: str, subject_id: int) -> dict:
    """
    Fetch a discovered URL, extract article text, store via db.insert_raw_document.

    Returns a status dict.
    """
    source_id = db.insert_source(subject_id, "ai_search", url)

    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        }
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()

        soup = BeautifulSoup(r.text, "html.parser")

        # Remove scripts, styles, navs
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        # Try common article selectors
        article_text = ""
        for selector in ["article p", "div.mw-parser-output > p", "main p", "div.content p", "p"]:
            paragraphs = soup.select(selector)
            if paragraphs:
                texts = [p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True)]
                if texts:
                    article_text = "\n\n".join(texts)
                    break

        if not article_text or len(article_text) < 100:
            # Fallback: get all text
            article_text = soup.get_text(separator="\n", strip=True)

        if not article_text or len(article_text) < 50:
            db.update_source_status(source_id, "failed")
            return {"status": "failed", "error": "No usable text extracted", "url": url}

        content_hash = hashlib.sha256(article_text.encode("utf-8")).hexdigest()

        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else url

        metadata = {
            "source_type": "ai_search",
            "url": url,
            "title": title,
            "subject": subject_name,
        }

        doc_id = db.insert_raw_document(
            source_id=source_id,
            subject_id=subject_id,
            raw_text=article_text,
            content_hash=content_hash,
            metadata=metadata,
            source_type="ai_search",
        )

        db.update_source_status(source_id, "success")

        return {
            "status": "complete",
            "doc_id": doc_id,
            "url": url,
            "chars": len(article_text),
        }

    except Exception as e:
        logger.error(f"Failed to scrape AI-discovered URL {url}: {e}")
        db.update_source_status(source_id, "failed")
        return {"status": "failed", "error": str(e), "url": url}
