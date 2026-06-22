import hashlib
import logging
import os
import re
import threading
import tempfile
from pathlib import Path
from flask import Blueprint, render_template, request, redirect, url_for, jsonify, flash
from src.storage import db
from src.scrapers.dispatcher import dispatch_scraping, get_scrape_config, set_scrape_config, load_sources_config, save_sources_config
from src.pipeline.chunking import run_chunking
from src.pipeline.embeddings import run_embeddings
from src.pipeline.ner import run_ner
from src.pipeline.clustering import run_clustering
from src.pipeline.synthesis import run_synthesis
from src.vault.obsidian_writer import write_vault

logger = logging.getLogger(__name__)
bp = Blueprint("web", __name__)


def run_full_pipeline_thread(subject_name: str, subject_id: int, scrape_config: dict = None):
    """Target function for background thread execution of all pipeline stages."""
    try:
        logger.info(f"Background thread starting pipeline for: {subject_name} (ID: {subject_id})")
        if db.check_cancellation(subject_id):
            logger.info(f"Pipeline execution cancelled early for subject: {subject_id}")
            return
            
        # Step 1: Scrape
        dispatch_scraping(subject_name, scrape_config=scrape_config)
        if db.check_cancellation(subject_id): return
        
        # Step 2: Chunking
        run_chunking(subject_id)
        if db.check_cancellation(subject_id): return
        
        # Step 3: Embedding
        run_embeddings(subject_id)
        if db.check_cancellation(subject_id): return
        
        # Step 4: NER
        run_ner(subject_id)
        if db.check_cancellation(subject_id): return
        
        # Step 5: Clustering
        run_clustering(subject_id)
        if db.check_cancellation(subject_id): return
        
        # Step 6: Synthesis
        run_synthesis(subject_id)
        if db.check_cancellation(subject_id): return
        
        # Step 7: Vault Export
        write_vault(subject_id)
        logger.info(f"Background thread successfully completed pipeline for: {subject_name}")
    except Exception as e:
        logger.error(f"Error in background pipeline thread for subject {subject_id}: {e}", exc_info=True)


def run_pdf_pipeline_thread(subject_id: int, subject_name: str):
    """Background thread for running pipeline after PDF ingestion (skip scrape stage)."""
    try:
        logger.info(f"Background PDF pipeline starting for: {subject_name} (ID: {subject_id})")
        if db.check_cancellation(subject_id): return
        
        # Skip scrape – PDF already ingested. Mark scrape as skipped.
        db.update_pipeline_stage(subject_id, "scrape", "skipped", "PDF upload — scraping not needed.")
        if db.check_cancellation(subject_id): return

        run_chunking(subject_id)
        if db.check_cancellation(subject_id): return
        
        run_embeddings(subject_id)
        if db.check_cancellation(subject_id): return
        
        run_ner(subject_id)
        if db.check_cancellation(subject_id): return
        
        run_clustering(subject_id)
        if db.check_cancellation(subject_id): return
        
        run_synthesis(subject_id)
        if db.check_cancellation(subject_id): return
        
        write_vault(subject_id)
        logger.info(f"Background PDF pipeline completed for: {subject_name}")
    except Exception as e:
        logger.error(f"Error in background PDF pipeline for subject {subject_id}: {e}", exc_info=True)


@bp.route("/")
def index():
    """Render dashboard displaying global stats and subjects list."""
    subjects = db.list_subjects()
    global_stats = db.get_global_stats()
    
    # Enrich subjects with stats and overall pipeline status
    enriched_subjects = []
    for s in subjects:
        stats = db.get_subject_stats(s["id"])
        stages = db.get_pipeline_status(s["id"])
        
        # Compute overall status
        status = "pending"
        if any(st["status"] == "running" for st in stages):
            status = "running"
        elif any(st["status"] == "failed" for st in stages):
            status = "failed"
        elif all(st["status"] == "complete" or st in ("complete", "skipped") for st in stages):
            # check if at least one complete or skipped
            status_list = [st["status"] for st in stages]
            if "running" not in status_list and "pending" not in status_list:
                status = "complete"
                
        enriched_subjects.append({
            "id": s["id"],
            "name": s["name"],
            "canonical_name": s["canonical_name"],
            "created_at": s["created_at"],
            "stats": stats,
            "status": status,
            "stages": stages
        })
        
    return render_template("index.html", subjects=enriched_subjects, global_stats=global_stats)


@bp.route("/subjects", methods=["POST"])
def create_subject():
    """Create a new subject and start the async pipeline thread."""
    name = request.form.get("name", "").strip()
    if not name:
        flash("Subject name cannot be empty.")
        return redirect(url_for("web.index"))
        
    subject_id = db.get_or_create_subject(name, "person")
    db.init_pipeline_stages(subject_id)

    # Collect scrape_config from form (if provided)
    scrape_config = None
    max_sources = request.form.get("max_sources_per_type")
    enabled_sources = request.form.getlist("enabled_sources")
    if max_sources or enabled_sources:
        scrape_config = {}
        if max_sources:
            scrape_config["max_sources_per_type"] = int(max_sources)
        if enabled_sources:
            scrape_config["enabled_sources"] = enabled_sources

    # Launch pipeline in a background thread
    t = threading.Thread(target=run_full_pipeline_thread, args=(name, subject_id, scrape_config))
    t.daemon = True
    t.start()
    
    flash(f"Started pipeline background thread for: {name}")
    return redirect(url_for("web.subject_detail", id=subject_id))


@bp.route("/subjects/<int:id>/reset", methods=["POST"])
def reset_subject(id):
    """Cancel active pipeline, delete all subject data, and return to dashboard."""
    db.delete_subject_data(id)
    flash("Pipeline stopped and subject reset successfully.")
    return redirect(url_for("web.index"))


@bp.route("/subjects/<int:id>")
def subject_detail(id):
    """Display detailed progress and parsed entities for a single subject."""
    subject = db.get_subject(id)
    if not subject:
        return "Subject not found", 404
        
    stats = db.get_subject_stats(id)
    stages = db.get_pipeline_status(id)
    entities = db.get_entities(id)
    relationships = db.get_relationships(id)
    notes = db.get_synthesized_notes(id)
    raw_docs = db.get_raw_documents(id)
    
    # Calculate overall status
    is_running = any(st["status"] == "running" for st in stages)
    
    return render_template(
        "subject.html",
        subject=subject,
        stats=stats,
        stages=stages,
        entities=entities,
        relationships=relationships,
        notes=notes,
        raw_docs=raw_docs,
        is_running=is_running
    )


@bp.route("/subjects/<int:id>/notes")
def notes_viewer(id):
    """Standalone notes viewer for reading synthesized information."""
    subject = db.get_subject(id)
    if not subject:
        return "Subject not found", 404
    notes = db.get_synthesized_notes(id)
    return render_template("notes.html", subject=subject, notes=notes)


@bp.route("/api/status/<int:id>")
def api_status(id):
    """AJAX JSON endpoint for pipeline status polling."""
    stages = db.get_pipeline_status(id)
    stats = db.get_subject_stats(id)
    return jsonify({
        "stages": stages,
        "stats": stats
    })


@bp.route("/api/subjects/<int:id>/graph-data")
def graph_data(id):
    """Retrieve nodes and links for D3 network visualization."""
    subject = db.get_subject(id)
    if not subject:
        return jsonify({"nodes": [], "links": []}), 404
        
    entities = db.get_entities(id)
    relationships = db.get_relationships(id)
    
    # Construct Nodes
    nodes = []
    # Insert subject as the core central node
    nodes.append({
        "id": f"subject_{id}",
        "name": subject["name"],
        "type": "person",
        "is_subject": True,
        "size": 30
    })
    
    node_ids = {f"subject_{id}"}
    
    for ent in entities:
        # Ignore duplicate of subject if present in entities
        if ent["name"].lower() == subject["name"].lower():
            continue
            
        node_id = f"entity_{ent['id']}"
        if node_id not in node_ids:
            nodes.append({
                "id": node_id,
                "name": ent["name"],
                "type": ent["entity_type"],
                "is_subject": False,
                "size": min(10 + ent["mention_count"] * 2, 25)
            })
            node_ids.add(node_id)
            
    # Construct Links
    links = []
    for rel in relationships:
        source_id = None
        target_id = None
        
        # Check matching subject
        if rel["entity_a"].lower() == subject["name"].lower():
            source_id = f"subject_{id}"
        if rel["entity_b"].lower() == subject["name"].lower():
            target_id = f"subject_{id}"
            
        # Check matching entity IDs
        for ent in entities:
            if not source_id and ent["name"].lower() == rel["entity_a"].lower():
                source_id = f"entity_{ent['id']}"
            if not target_id and ent["name"].lower() == rel["entity_b"].lower():
                target_id = f"entity_{ent['id']}"
                
        if source_id and target_id:
            links.append({
                "source": source_id,
                "target": target_id,
                "type": rel["relationship_type"],
                "description": rel["description"],
                "confidence": rel["confidence"]
            })
            
    return jsonify({"nodes": nodes, "links": links})


@bp.route("/subjects/<int:id>/export", methods=["POST"])
def export_vault(id):
    """Trigger Obsidian Vault compilation for the subject."""
    try:
        path = write_vault(id)
        return jsonify({"status": "success", "vault_path": path})
    except Exception as e:
        logger.error(f"Manual export failed for subject {id}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@bp.route("/subjects/<int:id>/vault-zip")
def vault_zip(id):
    """Generate and download a ZIP file of the subject's vault folder."""
    subject = db.get_subject(id)
    if not subject:
        return jsonify({"status": "error", "message": "Subject not found"}), 404

    vault_base = os.getenv("VAULT_PATH", "./output/vault")
    sanitized = subject["name"].replace("/", "").replace("\\", "").replace(":", "").strip()
    vault_dir = os.path.realpath(os.path.join(vault_base, sanitized))

    if not os.path.isdir(vault_dir):
        try:
            write_vault(id)
        except Exception as e:
            return jsonify({"status": "error", "message": f"Vault does not exist and auto-export failed: {e}"}), 404

    if not os.path.isdir(vault_dir):
        return jsonify({"status": "error", "message": "Vault folder does not exist. Please complete the pipeline."}), 404

    import zipfile
    import io
    from flask import send_file

    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, _dirs, files in os.walk(vault_dir):
            for file in files:
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, vault_dir)
                zipf.write(full_path, rel_path)

    memory_file.seek(0)
    zip_filename = f"{sanitized}_obsidian_vault.zip"

    return send_file(
        memory_file,
        mimetype="application/zip",
        as_attachment=True,
        download_name=zip_filename
    )


# ---------------------------------------------------------------------------
# PDF Upload
# ---------------------------------------------------------------------------

@bp.route("/upload-pdf", methods=["POST"])
def upload_pdf():
    """Accept a PDF file upload, extract text, store, and launch pipeline."""
    if "pdf_file" not in request.files:
        return jsonify({"status": "error", "message": "No file part in request"}), 400

    pdf_file = request.files["pdf_file"]
    subject_name = request.form.get("name", "").strip()

    if not pdf_file or pdf_file.filename == "":
        return jsonify({"status": "error", "message": "No file selected"}), 400

    if not subject_name:
        return jsonify({"status": "error", "message": "Subject name is required"}), 400

    # Save to a temp location
    upload_dir = os.path.join("output", "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    tmp_path = os.path.join(upload_dir, pdf_file.filename)
    pdf_file.save(tmp_path)

    try:
        from PyPDF2 import PdfReader

        reader = PdfReader(tmp_path)
        pages_text = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages_text.append(text)

        if not pages_text:
            return jsonify({"status": "error", "message": "No text could be extracted from the PDF"}), 400

        raw_text = "\n\n".join(pages_text)
        content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

        subject_id = db.get_or_create_subject(subject_name, "person")
        db.init_pipeline_stages(subject_id)

        doc_id = db.insert_pdf_document(
            subject_id=subject_id,
            raw_text=raw_text,
            content_hash=content_hash,
            metadata={"filename": pdf_file.filename, "pages": len(reader.pages)},
            filename=pdf_file.filename,
        )

        # Launch full pipeline in background thread (skip scrape)
        t = threading.Thread(target=run_pdf_pipeline_thread, args=(subject_id, subject_name))
        t.daemon = True
        t.start()

        flash(f"PDF '{pdf_file.filename}' uploaded and pipeline started for: {subject_name}")
        return redirect(url_for("web.subject_detail", id=subject_id))

    except ImportError:
        return jsonify({"status": "error", "message": "PyPDF2 is not installed on the server"}), 500
    except Exception as e:
        logger.error(f"PDF upload failed: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


# ---------------------------------------------------------------------------
# Scrape Config API
# ---------------------------------------------------------------------------

@bp.route("/api/scrape-config", methods=["GET"])
def api_get_scrape_config():
    """Return current scraper settings."""
    return jsonify({"status": "ok", "config": get_scrape_config()})


@bp.route("/api/scrape-config", methods=["POST"])
def api_set_scrape_config():
    """Update scraper settings (max sources, enabled sources)."""
    data = request.get_json(silent=True) or {}
    set_scrape_config(data)
    return jsonify({"status": "ok", "config": get_scrape_config()})


# ---------------------------------------------------------------------------
# Vault File Endpoints
# ---------------------------------------------------------------------------

@bp.route("/subjects/<int:id>/vault-files")
def vault_files(id):
    """List all files in the vault output directory for a subject."""
    subject = db.get_subject(id)
    if not subject:
        return jsonify({"status": "error", "message": "Subject not found"}), 404

    vault_base = os.getenv("VAULT_PATH", "./output/vault")
    sanitized = subject["name"].replace("/", "").replace("\\", "").replace(":", "").strip()
    vault_dir = os.path.join(vault_base, sanitized)

    if not os.path.isdir(vault_dir):
        return jsonify({"status": "ok", "files": [], "vault_path": vault_dir})

    files = []
    for root, _dirs, filenames in os.walk(vault_dir):
        for fname in filenames:
            full_path = os.path.join(root, fname)
            rel_path = os.path.relpath(full_path, vault_dir)
            files.append({
                "path": rel_path.replace("\\", "/"),
                "size": os.path.getsize(full_path),
            })

    return jsonify({"status": "ok", "files": files, "vault_path": vault_dir})


@bp.route("/subjects/<int:id>/vault-file/<path:path>")
def vault_file_content(id, path):
    """Return the content of a specific vault file."""
    subject = db.get_subject(id)
    if not subject:
        return jsonify({"status": "error", "message": "Subject not found"}), 404

    vault_base = os.getenv("VAULT_PATH", "./output/vault")
    sanitized = subject["name"].replace("/", "").replace("\\", "").replace(":", "").strip()
    vault_dir = os.path.join(vault_base, sanitized)
    file_path = os.path.join(vault_dir, path)

    # Security: ensure the resolved path is under vault_dir
    real_vault = os.path.realpath(vault_dir)
    real_file = os.path.realpath(file_path)
    if not real_file.startswith(real_vault):
        return jsonify({"status": "error", "message": "Invalid path"}), 403

    if not os.path.isfile(file_path):
        return jsonify({"status": "error", "message": "File not found"}), 404

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        return jsonify({"status": "ok", "path": path, "content": content})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ---------------------------------------------------------------------------
# Standalone Scraper Control Dashboard
# ---------------------------------------------------------------------------

@bp.route("/scraper-dashboard")
def scraper_dashboard():
    """Render standalone scraper dashboard."""
    sources_config = load_sources_config()
    scrape_config = get_scrape_config()
    global_stats = db.get_global_stats()
    
    stats = {
        "total_sources": db._db.sources.count_documents({}),
        "success_sources": db._db.sources.count_documents({"status": "success"}),
        "failed_sources": db._db.sources.count_documents({"status": "failed"}),
        "pending_sources": db._db.sources.count_documents({"status": "pending"}),
        "scraped_docs": db._db.scraped_documents.count_documents({}),
    }
    
    total_completed = stats["success_sources"] + stats["failed_sources"]
    stats["success_rate"] = int((stats["success_sources"] / total_completed * 100)) if total_completed > 0 else 100
    stats["active_crawls"] = db._db.pipeline_runs.count_documents({"stage": "scrape", "status": "running"})

    return render_template(
        "scraper_dashboard.html",
        sources_config=sources_config,
        scrape_config=scrape_config,
        global_stats=global_stats,
        stats=stats
    )


@bp.route("/api/scrape-stats")
def api_scrape_stats():
    """Return JSON scraper stats and log audits."""
    try:
        from datetime import datetime
        total_sources = db._db.sources.count_documents({})
        success_sources = db._db.sources.count_documents({"status": "success"})
        failed_sources = db._db.sources.count_documents({"status": "failed"})
        pending_sources = db._db.sources.count_documents({"status": "pending"})
        scraped_docs = db._db.scraped_documents.count_documents({})
        
        total_completed = success_sources + failed_sources
        success_rate = int((success_sources / total_completed * 100)) if total_completed > 0 else 100
        active_crawls = db._db.pipeline_runs.count_documents({"stage": "scrape", "status": "running"})
        
        pipeline = [{"$group": {"_id": "$source_type", "count": {"$sum": 1}}}]
        dist = list(db._db.scraped_documents.aggregate(pipeline))
        distribution = {item["_id"]: item["count"] for item in dist if item["_id"]}
        
        recent_sources = []
        for s in db._db.sources.find().sort([("fetched_at", -1), ("_id", -1)]).limit(20):
            subj = db._db.subjects.find_one({"_id": s["subject_id"]})
            subj_name = subj["name"] if subj else f"ID {s['subject_id']}"
            
            fetched_str = ""
            if s.get("fetched_at"):
                dt = s["fetched_at"]
                if isinstance(dt, datetime):
                    fetched_str = dt.strftime("%Y-%m-%d %H:%M:%S")
                else:
                    fetched_str = str(dt)
                    
            recent_sources.append({
                "id": s["_id"],
                "subject_name": subj_name,
                "source_type": s["source_type"],
                "url": s["url"],
                "status": s.get("status", "pending"),
                "fetched_at": fetched_str,
            })
            
        return jsonify({
            "status": "ok",
            "stats": {
                "total_sources": total_sources,
                "success_sources": success_sources,
                "failed_sources": failed_sources,
                "pending_sources": pending_sources,
                "scraped_docs": scraped_docs,
                "success_rate": success_rate,
                "active_crawls": active_crawls,
                "distribution": distribution
            },
            "recent_sources": recent_sources
        })
    except Exception as e:
        logger.error(f"Failed to fetch scraper stats: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


@bp.route("/api/scraper-dashboard/save-config", methods=["POST"])
def api_save_dashboard_config():
    """Save config from scraper dashboard settings page."""
    try:
        data = request.get_json(silent=True) or {}
        sources_config = load_sources_config()
        
        for stype in ["wikipedia", "imdb", "news", "social", "ai_search"]:
            if "sources" in data and stype in data["sources"]:
                sources_config.setdefault(stype, {})["enabled"] = bool(data["sources"][stype])
                
        for stype in ["wikipedia", "imdb", "news", "social"]:
            limit_key = f"rate_limit_{stype}"
            if limit_key in data:
                try:
                    val = float(data[limit_key])
                    sources_config.setdefault(stype, {})["rate_limit"] = val
                except (ValueError, TypeError):
                    pass
                    
        if "news_sitemaps" in data:
            sitemaps_data = data["news_sitemaps"]
            if isinstance(sitemaps_data, list):
                clean_sitemaps = []
                for item in sitemaps_data:
                    if isinstance(item, dict) and "url" in item and "name" in item:
                        clean_sitemaps.append({
                            "url": item["url"].strip(),
                            "name": item["name"].strip()
                        })
                sources_config.setdefault("news", {})["sitemaps"] = clean_sitemaps

        save_sources_config(sources_config)
        
        runtime_config = {}
        if "max_sources_per_type" in data:
            runtime_config["max_sources_per_type"] = int(data["max_sources_per_type"])
            
        enabled_list = []
        for stype, enabled in data.get("sources", {}).items():
            if enabled:
                enabled_list.append(stype)
        if enabled_list:
            runtime_config["enabled_sources"] = enabled_list
            
        set_scrape_config(runtime_config)
        
        return jsonify({"status": "ok", "message": "Scraper configuration saved successfully."})
    except Exception as e:
        logger.error(f"Failed to save scraper config: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


# Thread-safe container for test scraping logs
_test_scrape_logs = []
_test_scrape_lock = threading.Lock()

def _add_test_log(msg: str):
    with _test_scrape_lock:
        _test_scrape_logs.append(msg)
        if len(_test_scrape_logs) > 200:
            _test_scrape_logs.pop(0)


def _run_test_scrape_thread(url: str):
    global _test_scrape_logs
    with _test_scrape_lock:
        _test_scrape_logs.clear()
        
    _add_test_log(f"[*] INITIALIZING TEST SCRAPE FOR: {url}")
    
    import requests
    from bs4 import BeautifulSoup
    import time
    
    try:
        _add_test_log(f"[*] Sending GET request to {url}...")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        start_time = time.time()
        resp = requests.get(url, headers=headers, timeout=10)
        duration = time.time() - start_time
        
        _add_test_log(f"[+] Status Code: {resp.status_code}")
        _add_test_log(f"[+] Response time: {duration:.2f} seconds")
        _add_test_log(f"[+] Content-Type: {resp.headers.get('Content-Type', 'Unknown')}")
        
        if resp.status_code != 200:
            _add_test_log(f"[!] Error: Server responded with status code {resp.status_code}")
            return
            
        _add_test_log("[*] Parsing HTML body using BeautifulSoup4...")
        soup = BeautifulSoup(resp.content, "html.parser")
        
        title_el = soup.find("title") or soup.find("h1")
        title = title_el.text.strip() if title_el else "Unknown Title"
        _add_test_log(f"[+] Found page title: '{title}'")
        
        paragraphs = soup.find_all("p")
        text_content = "\n\n".join([p.text.strip() for p in paragraphs if p.text.strip()])
        char_len = len(text_content)
        word_count = len(text_content.split())
        
        _add_test_log(f"[+] Extracted {len(paragraphs)} paragraph elements")
        _add_test_log(f"[+] Text length: {char_len} characters ({word_count} words)")
        
        if char_len > 0:
            _add_test_log("[+] Sample Content:")
            _add_test_log("----------------------------------------")
            _add_test_log(text_content[:300] + ("..." if char_len > 300 else ""))
            _add_test_log("----------------------------------------")
            _add_test_log("[+] SUCCESS: The scraping pipeline is fully operational for this web source.")
        else:
            _add_test_log("[!] Warning: Webpage was successfully fetched, but no paragraph text could be extracted. The site may be using dynamic rendering or strict bot protection.")
            
    except Exception as e:
        _add_test_log(f"[!] EXCEPTION OCCURRED: {str(e)}")
        _add_test_log("[!] FAILED: Scraping failed. Verify target URL, network connection, or proxy configurations.")


@bp.route("/api/scraper-dashboard/test-scrape", methods=["POST"])
def api_test_scrape():
    """Trigger a background ad-hoc scrape test for a single URL."""
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    
    if not url:
        return jsonify({"status": "error", "message": "Target URL is required"}), 400
        
    if not (url.startswith("http://") or url.startswith("https://")):
        return jsonify({"status": "error", "message": "Invalid URL format. Must start with http:// or https://"}), 400
        
    t = threading.Thread(target=_run_test_scrape_thread, args=(url,))
    t.daemon = True
    t.start()
    
    return jsonify({"status": "ok", "message": "Test scrape started."})


@bp.route("/api/scraper-dashboard/test-log")
def api_test_scrape_log():
    """Retrieve current logs from the ad-hoc test scraper."""
    with _test_scrape_lock:
        return jsonify({"status": "ok", "logs": list(_test_scrape_logs)})


# ---------------------------------------------------------------------------
# Standalone Obsidian Vault Dashboard
# ---------------------------------------------------------------------------

@bp.route("/vault-dashboard")
def vault_dashboard():
    """Render standalone Vault Dashboard."""
    vault_base = os.getenv("VAULT_PATH", "./output/vault")
    vaults = []
    
    if os.path.isdir(vault_base):
        for name in os.listdir(vault_base):
            vault_path = os.path.join(vault_base, name)
            if os.path.isdir(vault_path):
                # Count markdown files
                md_files = [f for f in os.listdir(vault_path) if f.endswith(".md")]
                if md_files:
                    # Retrieve database subject mapping for matching info if it exists
                    subj = db._db.subjects.find_one({"name": {"$regex": f"^{re.escape(name.replace('_', ' '))}$", "$options": "i"}})
                    subject_id = subj["_id"] if subj else None
                    
                    vaults.append({
                        "name": name,
                        "file_count": len(md_files),
                        "subject_id": subject_id
                    })
                    
    return render_template("vault_dashboard.html", vaults=vaults)


@bp.route("/api/vault-explorer/<subject_name>/files")
def api_vault_explorer_files(subject_name):
    """List markdown files inside a subject's vault folder."""
    vault_base = os.getenv("VAULT_PATH", "./output/vault")
    vault_dir = os.path.realpath(os.path.join(vault_base, subject_name))
    
    # Security: ensure directory is inside vault_base
    real_base = os.path.realpath(vault_base)
    if not vault_dir.startswith(real_base) or not os.path.isdir(vault_dir):
        return jsonify({"status": "error", "message": "Access denied or vault not found"}), 403
        
    files = []
    for root, _dirs, filenames in os.walk(vault_dir):
        for fname in filenames:
            if fname.endswith(".md"):
                full_path = os.path.join(root, fname)
                rel_path = os.path.relpath(full_path, vault_dir)
                files.append({
                    "path": rel_path.replace("\\", "/"),
                    "size": os.path.getsize(full_path),
                })
                
    return jsonify({"status": "ok", "files": files})


@bp.route("/api/vault-explorer/<subject_name>/file/<path:filename>")
def api_vault_explorer_file_content(subject_name, filename):
    """Retrieve content of a specific markdown file in a subject's vault."""
    vault_base = os.getenv("VAULT_PATH", "./output/vault")
    vault_dir = os.path.realpath(os.path.join(vault_base, subject_name))
    file_path = os.path.realpath(os.path.join(vault_dir, filename))
    
    # Security: ensure file resides under the vault directory
    real_base = os.path.realpath(vault_base)
    if not file_path.startswith(real_base) or not os.path.isfile(file_path):
        return jsonify({"status": "error", "message": "Access denied or file not found"}), 403
        
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        return jsonify({"status": "ok", "content": content})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@bp.route("/api/vault-explorer/<subject_name>/graph")
def api_vault_explorer_graph(subject_name):
    """Parse subject's vault markdown files and return node-link Obsidian graph data."""
    import re
    vault_base = os.getenv("VAULT_PATH", "./output/vault")
    vault_dir = os.path.realpath(os.path.join(vault_base, subject_name))
    
    # Security check
    real_base = os.path.realpath(vault_base)
    if not vault_dir.startswith(real_base) or not os.path.isdir(vault_dir):
        return jsonify({"status": "error", "message": "Access denied or vault not found"}), 403
        
    nodes = []
    links = []
    note_map = {}
    md_files = []
    
    # Collect markdown files
    for root, _dirs, filenames in os.walk(vault_dir):
        for fname in filenames:
            if fname.endswith(".md"):
                full_path = os.path.join(root, fname)
                rel_path = os.path.relpath(full_path, vault_dir).replace("\\", "/")
                md_files.append((full_path, rel_path))
                note_title = os.path.splitext(fname)[0]
                note_map[note_title.lower()] = rel_path

    # Define nodes
    for _, rel in md_files:
        nodes.append({
            "id": rel,
            "label": os.path.splitext(os.path.basename(rel))[0],
            "type": "note"
        })

    # Regex search for wikilinks: [[Target Note]]
    wikilink_re = re.compile(r"\[\[(.*?)\]\]")

    # Connect nodes based on links
    for full_path, rel in md_files:
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                content = f.read()
            matches = wikilink_re.findall(content)
            for m in matches:
                target = m.split("|")[0].strip()
                if target.lower() in note_map:
                    links.append({
                        "source": rel,
                        "target": note_map[target.lower()],
                        "type": "wikilink"
                    })
        except Exception as e:
            logger.error(f"Error parsing wikilinks in {full_path}: {e}")

    return jsonify({"status": "ok", "nodes": nodes, "links": links})
