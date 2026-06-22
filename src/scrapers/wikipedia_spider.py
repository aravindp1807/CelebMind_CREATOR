import hashlib
import logging
import requests
from src.storage import db

logger = logging.getLogger(__name__)

# Try to import scrapling Fetcher, fallback to requests if not installed or fails
try:
    from scrapling.fetchers import Fetcher
    HAS_SCRAPLING = True
except ImportError:
    HAS_SCRAPLING = False
    logger.warning("Scrapling not installed or fetcher not found, using requests fallback for Wikipedia spider.")


def get_wikidata_label(qid: str) -> str:
    """Fetch label for a Wikidata ID."""
    url = "https://www.wikidata.org/w/api.php"
    params = {
        "action": "wbgetentities",
        "ids": qid,
        "format": "json",
        "props": "labels",
        "languages": "en"
    }
    try:
        r = requests.get(url, params=params, headers={"User-Agent": "SyntheticBrain/1.0"}, timeout=5)
        if r.status_code == 200:
            data = r.json()
            return data.get("entities", {}).get(qid, {}).get("labels", {}).get("en", {}).get("value", qid)
    except Exception as e:
        logger.debug(f"Failed to get label for Wikidata ID {qid}: {e}")
    return qid


def get_wikidata_info(subject_name: str) -> dict:
    """Fetch structured metadata from Wikidata API by subject name (using Wikipedia title match)."""
    url = "https://www.wikidata.org/w/api.php"
    params = {
        "action": "wbgetentities",
        "sites": "enwiki",
        "titles": subject_name,
        "format": "json",
        "languages": "en"
    }
    info = {}
    try:
        r = requests.get(url, params=params, headers={"User-Agent": "SyntheticBrain/1.0"}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            entities = data.get("entities", {})
            for qid, entity in entities.items():
                if qid == "-1":
                    continue
                info["wikidata_id"] = qid
                claims = entity.get("claims", {})
                
                # Birth date (P569)
                birth_claims = claims.get("P569", [])
                if birth_claims:
                    val = birth_claims[0].get("mainsnak", {}).get("datavalue", {}).get("value", {})
                    if isinstance(val, dict) and "time" in val:
                        info["birth_date"] = val["time"].lstrip('+').split('T')[0]
                
                # Occupation (P106)
                occ_claims = claims.get("P106", [])
                occupations = []
                for c in occ_claims[:3]:
                    val = c.get("mainsnak", {}).get("datavalue", {}).get("value", {})
                    if isinstance(val, dict) and "id" in val:
                        occupations.append(get_wikidata_label(val["id"]))
                if occupations:
                    info["occupation"] = ", ".join(occupations)

                # Nationality (P27)
                nat_claims = claims.get("P27", [])
                nationalities = []
                for c in nat_claims[:2]:
                    val = c.get("mainsnak", {}).get("datavalue", {}).get("value", {})
                    if isinstance(val, dict) and "id" in val:
                        nationalities.append(get_wikidata_label(val["id"]))
                if nationalities:
                    info["nationality"] = ", ".join(nationalities)
    except Exception as e:
        logger.error(f"Error fetching Wikidata info for {subject_name}: {e}")
    return info


def scrape_wikipedia(subject_name: str, subject_id: int) -> dict:
    """Scrape Wikipedia and Wikidata for a subject."""
    url_name = subject_name.replace(' ', '_')
    url = f"https://en.wikipedia.org/wiki/{url_name}"
    
    source_id = db.insert_source(subject_id, "wikipedia", url)
    
    try:
        response = None
        if HAS_SCRAPLING:
            try:
                # Use Fetcher as class methods or instance fallback
                if hasattr(Fetcher, 'get'):
                    response = Fetcher.get(url)
                elif hasattr(Fetcher, 'fetch'):
                    response = Fetcher.fetch(url)
                else:
                    response = Fetcher().fetch(url)
            except Exception as e:
                logger.error(f"Scrapling Fetcher failed: {e}. Falling back to requests.")
        
        # Fallback using requests if scrapling failed or wasn't installed
        if response is None:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            r = requests.get(url, headers=headers, timeout=15)
            r.raise_for_status()
            
            # Simple wrapper to match css extraction
            from bs4 import BeautifulSoup
            class BSWrapper:
                def __init__(self, soup):
                    self.soup = soup
                def css(self, selector):
                    items = self.soup.select(selector)
                    class ElementList:
                        def getall(self):
                            return [el.get_text() for el in items]
                        def get(self):
                            return items[0].get_text() if items else None
                        def __iter__(self):
                            return iter([BSWrapper(el) for el in items])
                    return ElementList()
            soup = BeautifulSoup(r.text, 'html.parser')
            response = BSWrapper(soup)
        
        # Extract article paragraph texts
        paragraphs = response.css('div.mw-parser-output > p').getall()
        body_text = "\n\n".join([p.strip() for p in paragraphs if p.strip()])
        
        if not body_text:
            raise ValueError(f"No content found in Wikipedia page: {url}")
            
        # Extract infobox data
        infobox = {}
        for row in response.css('table.infobox tr'):
            th = row.css('th').get()
            td = row.css('td').get()
            if th and td:
                k = th.strip().rstrip(':').strip()
                v = td.strip()
                if k and v:
                    infobox[k] = v
        
        # Get Wikidata info
        wikidata_info = get_wikidata_info(subject_name)
        
        metadata = {
            "source_type": "wikipedia",
            "url": url,
            "title": f"Wikipedia page for {subject_name}",
            "infobox": infobox,
            "wikidata": wikidata_info
        }
        
        content_hash = hashlib.sha256(body_text.encode('utf-8')).hexdigest()
        
        doc_id = db.insert_raw_document(
            source_id=source_id,
            subject_id=subject_id,
            raw_text=body_text,
            content_hash=content_hash,
            metadata=metadata
        )
        
        db.update_source_status(source_id, "success")
        
        return {
            "status": "complete",
            "doc_count": 1 if doc_id else 0,
            "url": url,
            "wikidata": wikidata_info
        }
        
    except Exception as e:
        logger.error(f"Failed to scrape Wikipedia for {subject_name}: {e}")
        db.update_source_status(source_id, "failed")
        return {
            "status": "failed",
            "error": str(e),
            "url": url
        }
