import argparse
import hashlib
import time
import sys
import logging
from dotenv import load_dotenv

# Initialize logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("cli")

load_dotenv()

# Try to import rich for beautiful terminal output, fallback to standard prints if missing
try:
    from rich.console import Console
    from rich.table import Table
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

# Import pipeline steps
from src.storage import db
from src.scrapers.dispatcher import dispatch_scraping
from src.pipeline.chunking import run_chunking
from src.pipeline.embeddings import run_embeddings
from src.pipeline.ner import run_ner
from src.pipeline.clustering import run_clustering
from src.pipeline.synthesis import run_synthesis
from src.vault.obsidian_writer import write_vault


def ingest_pdf(pdf_path: str, subject_name: str, subject_id: int) -> int | None:
    """Read a PDF file, extract text, and store via db.insert_pdf_document."""
    try:
        from PyPDF2 import PdfReader
    except ImportError:
        logger.error("PyPDF2 is not installed. Install it with: pip install PyPDF2")
        return None

    import os
    if not os.path.isfile(pdf_path):
        logger.error(f"PDF file not found: {pdf_path}")
        return None

    reader = PdfReader(pdf_path)
    pages_text = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            pages_text.append(text)

    if not pages_text:
        logger.error(f"No text could be extracted from PDF: {pdf_path}")
        return None

    raw_text = "\n\n".join(pages_text)
    content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    filename = os.path.basename(pdf_path)

    doc_id = db.insert_pdf_document(
        subject_id=subject_id,
        raw_text=raw_text,
        content_hash=content_hash,
        metadata={"filename": filename, "pages": len(reader.pages)},
        filename=filename,
    )

    if doc_id:
        logger.info(f"PDF ingested: {filename} -> doc_id={doc_id} ({len(raw_text)} chars, {len(reader.pages)} pages)")
    else:
        logger.warning(f"PDF already ingested (duplicate content hash): {filename}")

    return doc_id


def run_pipeline(subject_name: str, target_stage: str = "all", vault_path: str = None, pdf_path: str = None) -> list[dict]:
    """Execute the pipeline stages sequentially."""
    stages = ["scrape", "chunk", "embed", "ner", "cluster", "synthesize", "vault"]
    
    # Map stage name to their execution function (takes subject_id and returns result count/message)
    # The first stage 'scrape' is different because it creates the subject and returns results
    stage_funcs = {
        "chunk": run_chunking,
        "embed": run_embeddings,
        "ner": run_ner,
        "cluster": run_clustering,
        "synthesize": run_synthesis,
        "vault": lambda sid: write_vault(sid, vault_path)
    }
    
    # Get or create subject id
    subject_id = db.get_or_create_subject(subject_name, "person")
    db.init_pipeline_stages(subject_id)

    # If a PDF is provided, ingest it before starting the pipeline
    if pdf_path:
        print(f"\n>>> Ingesting PDF: {pdf_path}")
        ingest_pdf(pdf_path, subject_name, subject_id)

    results = []
    
    # Determine which stages to run
    if target_stage == "all":
        stages_to_run = stages
        # If we ingested a PDF, skip the scrape stage (the PDF is the source)
        if pdf_path:
            stages_to_run = [s for s in stages if s != "scrape"]
    else:
        if target_stage not in stages:
            print(f"Error: Unknown stage '{target_stage}'. Choose from {stages} or 'all'")
            sys.exit(1)
        # Just run the single requested stage
        stages_to_run = [target_stage]
        
    for stage in stages_to_run:
        start_time = time.time()
        status = "complete"
        details = ""
        
        print(f"\n>>> Running stage: {stage.upper()}...")
        
        try:
            if stage == "scrape":
                # Special handling for scraping which creates the subject and starts parallel fetches
                scrape_results = dispatch_scraping(subject_name)
                # Count success
                success_count = sum(1 for r in scrape_results["results"].values() if r.get("status") == "complete")
                details = f"Scraped {success_count} sources successfully"
            else:
                func = stage_funcs[stage]
                res = func(subject_id)
                if stage == "vault":
                    details = f"Vault written to: {res}"
                else:
                    details = f"Processed/Created {res} items"
        except Exception as e:
            status = "failed"
            details = str(e)
            logger.error(f"Stage {stage} failed: {e}")
            
        elapsed = time.time() - start_time
        results.append({
            "stage": stage,
            "status": status,
            "details": details,
            "time": elapsed
        })
        
        if status == "failed":
            print(f"ERROR: Stage {stage} failed. Stopping pipeline.")
            break
            
    return results


def print_summary(results: list[dict]):
    """Print execution summary as a table."""
    if HAS_RICH:
        console = Console()
        table = Table(title="Synthetic Brain Pipeline Summary")
        table.add_column("Stage", style="cyan")
        table.add_column("Status", style="bold")
        table.add_column("Details", style="magenta")
        table.add_column("Time (s)", justify="right", style="green")
        
        for r in results:
            status_style = "green" if r["status"] == "complete" else ("yellow" if r["status"] == "skipped" else "red")
            table.add_row(
                r["stage"].upper(),
                f"[{status_style}]{r['status']}[/{status_style}]",
                r["details"],
                f"{r['time']:.2f}"
            )
        console.print(table)
    else:
        # Simple plain text fallback
        print("\n" + "="*70)
        print("PIPELINE EXECUTION SUMMARY")
        print("="*70)
        print(f"{'STAGE':<12} | {'STATUS':<10} | {'TIME (s)':<8} | DETAILS")
        print("-"*70)
        for r in results:
            print(f"{r['stage'].upper():<12} | {r['status']:<10} | {r['time']:<8.2f} | {r['details']}")
        print("="*70)


def main():
    parser = argparse.ArgumentParser(description="Synthetic Brain Graph Generation Pipeline CLI")
    parser.add_argument("name", help="Name of the celebrity / subject to process")
    parser.add_argument("--stage", default="all", help="Pipeline stage to run (scrape, chunk, embed, ner, cluster, synthesize, vault, all)")
    parser.add_argument("--vault-path", help="Base path to export the Obsidian vault")
    parser.add_argument("--pdf", help="Path to a PDF file to ingest as a source document")
    
    args = parser.parse_args()
    
    print(f"Starting Synthetic Brain pipeline for subject: '{args.name}'")
    
    results = run_pipeline(args.name, args.stage, args.vault_path, args.pdf)
    print_summary(results)


if __name__ == "__main__":
    main()
