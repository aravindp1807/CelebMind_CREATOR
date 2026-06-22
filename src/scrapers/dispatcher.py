import concurrent.futures
import logging
import os
import yaml
from src.storage import db
from src.scrapers.wikipedia_spider import scrape_wikipedia
from src.scrapers.news_spider import scrape_news
from src.scrapers.imdb_spider import scrape_imdb
from src.scrapers.social_spider import scrape_social
from src.scrapers.ai_search import discover_urls, scrape_discovered_url

logger = logging.getLogger(__name__)

# Module-level scrape configuration (can be updated at runtime via API)
_scrape_config = {
    "max_sources_per_type": 5,
    "enabled_sources": ["wikipedia", "imdb", "news", "ai_search"],
}


def get_scrape_config() -> dict:
    """Return the current scraping configuration."""
    return dict(_scrape_config)


def set_scrape_config(config: dict):
    """Update the scraping configuration."""
    if "max_sources_per_type" in config:
        _scrape_config["max_sources_per_type"] = int(config["max_sources_per_type"])
    if "enabled_sources" in config:
        _scrape_config["enabled_sources"] = list(config["enabled_sources"])


def load_sources_config() -> dict:
    """Load configuration from config/sources.yaml, falling back to defaults if missing."""
    defaults = {
        "wikipedia": {"enabled": True, "rate_limit": 1.0},
        "imdb": {"enabled": True, "rate_limit": 2.0},
        "news": {
            "enabled": True,
            "sitemaps": [
                {"url": "https://www.bbc.co.uk/news/sitemap.xml", "name": "BBC News"},
                {"url": "https://www.reuters.com/arc/outboundfeeds/sitemap-index/?outputType=xml", "name": "Reuters"}
            ],
            "rate_limit": 2.0,
            "max_articles_per_sitemap": 20
        },
        "social": {"enabled": False, "rate_limit": 5.0},
        "ai_search": {"enabled": True},
    }
    
    config_path = os.path.join("config", "sources.yaml")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
                if isinstance(loaded, dict):
                    for k, v in loaded.items():
                        if k in defaults and isinstance(defaults[k], dict) and isinstance(v, dict):
                            defaults[k].update(v)
                        else:
                            defaults[k] = v
        except Exception as e:
            logger.error(f"Failed to parse config/sources.yaml: {e}. Using default configuration.")
            
    return defaults


def save_sources_config(sources_config: dict):
    """Save the sources configuration back to config/sources.yaml."""
    config_path = os.path.join("config", "sources.yaml")
    try:
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(sources_config, f, default_flow_style=False, sort_keys=False)
        logger.info("Successfully saved configuration to config/sources.yaml")
    except Exception as e:
        logger.error(f"Failed to save config/sources.yaml: {e}")


def _run_ai_search(subject_name: str, subject_id: int, max_sources: int) -> dict:
    """Discover URLs via AI and scrape them."""
    discovered = discover_urls(subject_name)
    if not discovered:
        return {"status": "complete", "doc_count": 0, "message": "No URLs discovered"}

    # Limit to max_sources
    discovered = discovered[:max_sources]

    results = []
    for item in discovered:
        result = scrape_discovered_url(item["url"], subject_name, subject_id)
        results.append(result)

    success_count = sum(1 for r in results if r.get("status") == "complete")
    return {
        "status": "complete" if success_count > 0 else "failed",
        "doc_count": success_count,
        "total_discovered": len(discovered),
        "results": results,
    }


def dispatch_scraping(subject_name: str, sources_config: dict = None, scrape_config: dict = None) -> dict:
    """
    Launch scrapers in parallel for a subject.
    Initializes pipeline stages, creates subject, updates status, and returns execution summary.
    """
    subject_id = db.get_or_create_subject(subject_name, "person")
    db.init_pipeline_stages(subject_id)
    db.update_pipeline_stage(subject_id, "scrape", "running")
    
    if sources_config is None:
        sources_config = load_sources_config()

    # Merge runtime scrape_config if provided
    effective_config = get_scrape_config()
    if scrape_config:
        effective_config.update(scrape_config)

    max_sources = effective_config.get("max_sources_per_type", 5)
    enabled = set(effective_config.get("enabled_sources", ["wikipedia", "imdb", "news", "ai_search"]))

    results = {}
    
    # We will use ThreadPoolExecutor to run scrapers concurrently
    # Map from source name to its function and args
    spiders = {}
    
    if "wikipedia" in enabled and sources_config.get("wikipedia", {}).get("enabled", True):
        spiders["wikipedia"] = lambda: scrape_wikipedia(subject_name, subject_id)
        
    if "imdb" in enabled and sources_config.get("imdb", {}).get("enabled", True):
        spiders["imdb"] = lambda: scrape_imdb(subject_name, subject_id)
        
    if "news" in enabled and sources_config.get("news", {}).get("enabled", True):
        spiders["news"] = lambda: scrape_news(subject_name, subject_id, sources_config.get("news"))
        
    if "social" in enabled and sources_config.get("social", {}).get("enabled", False):
        spiders["social"] = lambda: scrape_social(subject_name, subject_id, sources_config.get("social"))

    if "ai_search" in enabled and sources_config.get("ai_search", {}).get("enabled", True):
        spiders["ai_search"] = lambda: _run_ai_search(subject_name, subject_id, max_sources)

    logger.info(f"Starting parallel scraping for '{subject_name}' (ID: {subject_id}) with scrapers: {list(spiders.keys())}")
    
    # Run the spiders concurrently
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(spiders) if spiders else 1) as executor:
        future_to_source = {executor.submit(func): source for source, func in spiders.items()}
        for future in concurrent.futures.as_completed(future_to_source):
            if db.check_cancellation(subject_id):
                logger.info(f"Scraping cancelled early for subject: {subject_id}")
                executor.shutdown(wait=False, cancel_futures=True)
                db.update_pipeline_stage(subject_id, "scrape", "failed", "Scraping cancelled by user.")
                return {"subject_id": subject_id, "results": results, "cancelled": True}
                
            source = future_to_source[future]
            try:
                data = future.result()
                results[source] = data
                logger.info(f"Scraper '{source}' completed with status: {data.get('status')}")
            except Exception as exc:
                logger.error(f"Scraper '{source}' generated an exception: {exc}")
                results[source] = {"status": "failed", "error": str(exc)}
                
    # Write raw text files to output/scraped_raw/
    raw_dir = os.path.join("output", "scraped_raw", subject_name.replace(" ", "_"))
    os.makedirs(raw_dir, exist_ok=True)
    try:
        raw_docs = db.get_raw_documents(subject_id)
        for doc in raw_docs:
            safe_name = f"doc_{doc['id']}.txt"
            filepath = os.path.join(raw_dir, safe_name)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(doc["raw_text"])
    except Exception as e:
        logger.warning(f"Failed to write raw text files: {e}")

    # Determine overall status of the scraping stage
    # If at least one scraper succeeded, we can say it's complete, but if all failed, it failed
    all_failed = all(res.get("status") == "failed" for res in results.values()) if results else True
    
    if all_failed:
        db.update_pipeline_stage(subject_id, "scrape", "failed", "All configured scrapers failed.")
    else:
        db.update_pipeline_stage(subject_id, "scrape", "complete")
        
    return {
        "subject_id": subject_id,
        "results": results
    }
