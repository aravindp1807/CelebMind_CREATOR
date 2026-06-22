import hashlib
import logging
import time
import xml.etree.ElementTree as ET
import requests
from src.storage import db

logger = logging.getLogger(__name__)

try:
    from scrapling.fetchers import Fetcher
    HAS_SCRAPLING = True
except ImportError:
    HAS_SCRAPLING = False


def parse_sitemap(sitemap_url: str) -> list[str]:
    """Fetch and parse a sitemap XML to extract all URLs."""
    urls = []
    try:
        r = requests.get(sitemap_url, headers={"User-Agent": "SyntheticBrain/1.0"}, timeout=10)
        if r.status_code == 200:
            root = ET.fromstring(r.content)
            # Handle XML namespaces commonly found in sitemaps
            ns = ""
            if root.tag.startswith("{"):
                ns = root.tag.split("}")[0] + "}"
            
            for loc in root.findall(f".//{ns}loc"):
                if loc.text:
                    urls.append(loc.text.strip())
    except Exception as e:
        logger.error(f"Error parsing sitemap {sitemap_url}: {e}")
    return urls


def extract_article_text(url: str, selectors: list[str]) -> str:
    """Fetch article page and extract body text using provided CSS selectors."""
    try:
        response = None
        if HAS_SCRAPLING:
            try:
                if hasattr(Fetcher, 'get'):
                    response = Fetcher.get(url)
                elif hasattr(Fetcher, 'fetch'):
                    response = Fetcher.fetch(url)
                else:
                    response = Fetcher().fetch(url)
            except Exception as e:
                logger.error(f"Scrapling fetcher failed for {url}: {e}")
        
        # Fallback using requests and BeautifulSoup
        if response is None:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            r.raise_for_status()
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(r.text, 'html.parser')
            for sel in selectors:
                elements = soup.select(sel)
                if elements:
                    text = "\n\n".join([el.get_text().strip() for el in elements if el.get_text().strip()])
                    if text:
                        return text
            # Global fallback
            p_tags = soup.select('p')
            return "\n\n".join([p.get_text().strip() for p in p_tags if p.get_text().strip()])
        
        # If we got a Scrapling response
        for sel in selectors:
            texts = response.css(sel).getall()
            text = "\n\n".join([t.strip() for t in texts if t.strip()])
            if text:
                return text
                
        # Global paragraph fallback on response
        texts = response.css('p').getall()
        return "\n\n".join([t.strip() for t in texts if t.strip()])
    except Exception as e:
        logger.error(f"Failed to extract article text from {url}: {e}")
        return ""


def scrape_news(subject_name: str, subject_id: int, config: dict = None) -> dict:
    """Scrape news sitemaps for articles referencing the subject."""
    if config is None:
        config = {
            "sitemaps": ["https://www.nytimes.com/sitemaps/new/news.xml"],
            "selectors": ["article section", "div.story-body", "div.article-content", ".article-body", "article p", "div.content p"],
            "rate_limit": 1.0,
            "max_articles_per_sitemap": 3,
            "enabled": True
        }
        
    if not config.get("enabled", True):
        logger.info("News scraping is disabled in config.")
        return {"status": "skipped", "reason": "Disabled in config"}
        
    sitemaps = config.get("sitemaps", [])
    selectors = config.get("selectors", ["article p", "p"])
    rate_limit = config.get("rate_limit", 1.0)
    max_articles = config.get("max_articles_per_sitemap", 3)
    
    scraped_count = 0
    skipped_count = 0
    urls_processed = []
    
    subject_words = subject_name.lower().split()
    
    for sitemap_url in sitemaps:
        logger.info(f"Processing sitemap: {sitemap_url}")
        urls = parse_sitemap(sitemap_url)
        
        # Filter urls referencing the subject
        matched_urls = []
        for url in urls:
            # Simple check if all words or just the full name is in URL (hyphenated or directly)
            url_lower = url.lower()
            # E.g. "tom-hanks" in url or "tom" and "hanks" in url
            if any(word in url_lower for word in subject_words):
                matched_urls.append(url)
                
        logger.info(f"Found {len(matched_urls)} matching URLs in sitemap {sitemap_url}")
        
        sitemap_scraped = 0
        for url in matched_urls:
            if sitemap_scraped >= max_articles:
                break
                
            source_id = db.insert_source(subject_id, "news", url)
            
            logger.info(f"Scraping news article: {url}")
            time.sleep(rate_limit)
            
            article_text = extract_article_text(url, selectors)
            if not article_text or len(article_text) < 100:
                logger.warning(f"Empty or too short text for article: {url}")
                db.update_source_status(source_id, "failed")
                continue
                
            content_hash = hashlib.sha256(article_text.encode('utf-8')).hexdigest()
            metadata = {
                "source_type": "news",
                "url": url,
                "sitemap": sitemap_url,
                "scraped_at": time.time()
            }
            
            doc_id = db.insert_raw_document(
                source_id=source_id,
                subject_id=subject_id,
                raw_text=article_text,
                content_hash=content_hash,
                metadata=metadata
            )
            
            if doc_id:
                db.update_source_status(source_id, "success")
                sitemap_scraped += 1
                scraped_count += 1
                urls_processed.append(url)
            else:
                db.update_source_status(source_id, "success") # already exists, mark success anyway
                skipped_count += 1
                
    return {
        "status": "complete",
        "scraped_articles_count": scraped_count,
        "skipped_articles_count": skipped_count,
        "urls": urls_processed
    }
