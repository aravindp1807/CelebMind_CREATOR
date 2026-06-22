import hashlib
import logging
import re
import urllib.parse
import requests
from src.storage import db

logger = logging.getLogger(__name__)

try:
    from scrapling.fetchers import Fetcher
    HAS_SCRAPLING = True
except ImportError:
    HAS_SCRAPLING = False


def imdb_fetch(url: str) -> str:
    """Fetch URL with custom headers to prevent blocking."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.google.com/"
    }
    
    if HAS_SCRAPLING:
        try:
            # Try to fetch using Scrapling's Fetcher (which handles HTTP fingerprinting)
            if hasattr(Fetcher, 'get'):
                response = Fetcher.get(url)
            elif hasattr(Fetcher, 'fetch'):
                response = Fetcher.fetch(url)
            else:
                response = Fetcher().fetch(url)
            return response.text
        except Exception as e:
            logger.warning(f"Scrapling fetch failed for {url}: {e}. Trying requests.")
            
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    return r.text


def find_imdb_person_id(name: str) -> str | None:
    """Search IMDb for a person and return their nmXXXXXXX ID."""
    query = urllib.parse.quote_plus(name)
    url = f"https://www.imdb.com/find/?q={query}&s=nm"
    try:
        html = imdb_fetch(url)
        # Search for nmXXXXXXX in the links
        match = re.search(r'/name/(nm\d+)', html)
        if match:
            return match.group(1)
    except Exception as e:
        logger.error(f"Error searching IMDb for {name}: {e}")
    return None


def scrape_imdb(subject_name: str, subject_id: int) -> dict:
    """Scrape IMDb biography and filmography for the subject."""
    nm_id = find_imdb_person_id(subject_name)
    if not nm_id:
        logger.warning(f"Could not find IMDb ID for {subject_name}")
        return {"status": "skipped", "reason": "No IMDb match found"}
        
    url = f"https://www.imdb.com/name/{nm_id}/"
    source_id = db.insert_source(subject_id, "imdb", url)
    
    try:
        html = imdb_fetch(url)
        
        # Simple extraction using BeautifulSoup
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, 'html.parser')
        
        # Extract biography snippet
        bio_section = soup.select_one('[data-testid="bio-content"]')
        bio_text = bio_section.get_text().strip() if bio_section else ""
        
        if not bio_text:
            # Alternative selector
            bio_section = soup.select_one('.name-trivia-bio-text')
            bio_text = bio_section.get_text().strip() if bio_section else ""
            
        # Extract Known For works
        known_for = []
        kf_elements = soup.select('[data-testid="known-for-title-text"]')
        for el in kf_elements:
            title_el = el.select_one('a')
            if title_el:
                known_for.append(title_el.get_text().strip())
                
        # Extract metadata from JSON-LD
        import json
        json_ld_data = {}
        json_ld_tags = soup.select('script[type="application/ld+json"]')
        for tag in json_ld_tags:
            try:
                data = json.loads(tag.string)
                if data.get("@type") == "Person":
                    json_ld_data = data
                    break
            except:
                pass
                
        birth_date = json_ld_data.get("birthDate", "")
        birth_place = json_ld_data.get("birthPlace", {}).get("name", "") if isinstance(json_ld_data.get("birthPlace"), dict) else ""
        occupations = json_ld_data.get("jobTitle", [])
        if isinstance(occupations, str):
            occupations = [occupations]
            
        # Compile article text
        article_parts = []
        article_parts.append(f"{subject_name} is an artist cataloged on IMDb under ID {nm_id}.")
        if birth_date:
            article_parts.append(f"Born on {birth_date}" + (f" in {birth_place}." if birth_place else "."))
        if occupations:
            article_parts.append(f"Primary roles: {', '.join(occupations)}.")
        if bio_text:
            article_parts.append(f"Biography:\n{bio_text}")
        if known_for:
            article_parts.append(f"Known for works: {', '.join(known_for)}.")
            
        full_text = "\n\n".join(article_parts)
        content_hash = hashlib.sha256(full_text.encode('utf-8')).hexdigest()
        
        metadata = {
            "source_type": "imdb",
            "url": url,
            "imdb_id": nm_id,
            "known_for": known_for,
            "birth_date": birth_date,
            "birth_place": birth_place,
            "occupations": occupations
        }
        
        doc_id = db.insert_raw_document(
            source_id=source_id,
            subject_id=subject_id,
            raw_text=full_text,
            content_hash=content_hash,
            metadata=metadata
        )
        
        db.update_source_status(source_id, "success")
        
        return {
            "status": "complete",
            "imdb_id": nm_id,
            "doc_count": 1 if doc_id else 0,
            "url": url
        }
        
    except Exception as e:
        logger.error(f"Failed to scrape IMDb for {subject_name}: {e}")
        db.update_source_status(source_id, "failed")
        return {
            "status": "failed",
            "error": str(e),
            "url": url
        }
