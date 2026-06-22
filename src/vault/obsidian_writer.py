import os
import re
import json
import shutil
import logging
from pathlib import Path
from datetime import datetime, timezone
from src.storage import db

logger = logging.getLogger(__name__)


def _entity_type_to_folder(entity_type: str) -> str:
    """Map database entity types to Obsidian folder names."""
    mapping = {
        "person": "People",
        "work": "Works",
        "organization": "Organizations",
        "event": "Events",
        "place": "Places"
    }
    return mapping.get(entity_type.lower(), "People")


def _sanitize_filename(name: str) -> str:
    """Remove filesystem-unsafe characters from a string."""
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()


def _make_wikilink(entity_name: str, entity_type: str) -> str:
    """Generate an Obsidian wiki-link, e.g., [[People/Tom Hanks]]."""
    folder = _entity_type_to_folder(entity_type)
    sanitized = _sanitize_filename(entity_name)
    return f"[[{folder}/{sanitized}|{entity_name}]]"


def _render_template(template_name: str, **kwargs) -> str:
    """Load a template file from templates directory and fill with kwargs."""
    # Find templates directory
    # Inside src/vault/templates/ or local package copy
    base_dir = Path(__file__).parent
    template_path = base_dir / "templates" / template_name
    
    if not template_path.exists():
        # Fallback if templates are elsewhere
        template_path = Path("src/vault/templates") / template_name
        
    if not template_path.exists():
        raise FileNotFoundError(f"Template {template_name} not found at {template_path.absolute()}")
        
    with open(template_path, "r", encoding="utf-8") as f:
        content = f.read()
        
    return content.format(**kwargs)


def write_vault(subject_id: int, vault_base_path: str = None) -> str:
    """
    Generate the entire Obsidian Vault for a subject.
    Reads data from db, populates templates, writes markdown files, and returns the vault path.
    """
    db.update_pipeline_stage(subject_id, "vault", "running")
    
    try:
        subject = db.get_subject(subject_id)
        if not subject:
            raise ValueError(f"Subject with ID {subject_id} not found.")
            
        subject_name = subject["name"]
        
        # Determine vault directory path
        if vault_base_path is None:
            vault_base_path = os.getenv("VAULT_PATH", "./output/vault")
            
        sanitized_subject = _sanitize_filename(subject_name)
        vault_path = Path(vault_base_path) / sanitized_subject
        
        # 1. Create directory structure
        folders = ["brain", "People", "Works", "Organizations", "Events", "Places", "templates", ".obsidian"]
        for f in folders:
            (vault_path / f).mkdir(parents=True, exist_ok=True)
            
        # 2. Load data from database
        notes = db.get_synthesized_notes(subject_id)
        entities = db.get_entities(subject_id)
        relationships = db.get_relationships(subject_id)
        raw_docs = db.get_raw_documents(subject_id)
        
        # Map raw documents by ID for quick lookup
        doc_map = {d["id"]: d for d in raw_docs}
        
        # Create sources list formatting
        source_links_formatted = ""
        for doc in raw_docs:
            meta = doc.get("metadata", {})
            url = meta.get("url", "No URL")
            source_links_formatted += f"- **[Source: {doc['id']}]**: [{doc['metadata'].get('title', url)}]({url}) (Type: {doc['metadata'].get('source_type', 'unknown')})\n"
            
        # 3. Create individual entity notes
        # 3. Create individual entity notes
        # Group relationships by entity name for easy note populating
        rel_map = {}
        for r in relationships:
            a, b = r["entity_a"], r["entity_b"]
            if a not in rel_map:
                rel_map[a] = []
            if b not in rel_map:
                rel_map[b] = []
            rel_map[a].append(r)
            rel_map[b].append(r)
            
        # Write notes for each entity extracted (excluding the subject itself, which is handled centrally)
        for ent in entities:
            # Skip the main subject entity to avoid duplicate file creation
            if ent["name"].lower() == subject_name.lower():
                continue
                
            folder = _entity_type_to_folder(ent["entity_type"])
            ent_filename = _sanitize_filename(ent["name"]) + ".md"
            ent_path = vault_path / folder / ent_filename
            
            # Format relationships for this entity
            ent_rels_formatted = ""
            for r in rel_map.get(ent["name"], []):
                other = r["entity_b"] if r["entity_a"] == ent["name"] else r["entity_a"]
                other_type = r["type_b"] if r["entity_a"] == ent["name"] else r["type_a"]
                link = _make_wikilink(other, other_type)
                ent_rels_formatted += f"- **{r['relationship_type']}**: {link} — *{r['description']}*\n"
                
            if not ent_rels_formatted:
                ent_rels_formatted = "*No recorded relationships.*\n"
                
            # Render using correct template
            t_name = f"{ent['entity_type']}.md"
            try:
                # Compile fact summaries or description
                desc = f"Entity of type {ent['entity_type']} found in {subject_name}'s graph."
                rendered = _render_template(
                    t_name,
                    name=ent["name"],
                    sources="[]",
                    confidence=0.9,
                    date=datetime.now().strftime("%Y-%m-%d"),
                    description=desc,
                    summary=desc,
                    facts="- Extracted during Named Entity Recognition processing.\n",
                    relationships=ent_rels_formatted,
                    source_links="*Extracted via cross-mentions in raw documents.*"
                )
                with open(ent_path, "w", encoding="utf-8") as f:
                    f.write(rendered)
            except Exception as e:
                # If template fails or doesn't exist, write basic file
                logger.warning(f"Failed to render template {t_name}: {e}. Writing basic note.")
                with open(ent_path, "w", encoding="utf-8") as f:
                    f.write(f"# {ent['name']}\n\nType: {ent['entity_type']}\n\n## Relationships\n\n{ent_rels_formatted}")
                    
        # 4. Create Map of Content (MOC) Index files in folders
        for folder_type in ["person", "work", "organization", "event", "place"]:
            folder = _entity_type_to_folder(folder_type)
            index_path = vault_path / folder / "Index.md"
            
            folder_ents = [e for e in entities if e["entity_type"] == folder_type and e["name"].lower() != subject_name.lower()]
            
            index_content = f"# MOC — {folder}\n\n"
            index_content += f"Index of all categorized **{folder}** entities related to [[Home|{subject_name}]].\n\n"
            
            if folder_ents:
                for fe in folder_ents:
                    link = _make_wikilink(fe["name"], fe["entity_type"])
                    index_content += f"- {link} ({fe['mention_count']} mentions)\n"
            else:
                index_content += "*No entities found in this category.*\n"
                
            with open(index_path, "w", encoding="utf-8") as f:
                f.write(index_content)
                
        # 5. Create brain/ files
        # 5a. Profile.md
        profile_path = vault_path / "brain" / "Profile.md"
        profile_content = f"# Identity Profile — {subject_name}\n\n"
        if notes:
            for n in notes:
                profile_content += f"## {n['title']}\n\n{n['content_md']}\n\n---\n\n"
        else:
            profile_content += "*No synthesized intelligence notes available. Run the synthesis pipeline.*\n"
            
        with open(profile_path, "w", encoding="utf-8") as f:
            f.write(profile_content)
            
        # 5b. Key Facts.md
        facts_path = vault_path / "brain" / "Key Facts.md"
        facts_content = f"# Key Facts — {subject_name}\n\n"
        facts_content += "| Entity / Fact | Category | Relationships / Mentions |\n"
        facts_content += "| --- | --- | --- |\n"
        for ent in entities:
            link = _make_wikilink(ent["name"], ent["entity_type"]) if ent["name"].lower() != subject_name.lower() else ent["name"]
            facts_content += f"| {link} | {ent['entity_type']} | {ent['mention_count']} mentions |\n"
            
        with open(facts_path, "w", encoding="utf-8") as f:
            f.write(facts_content)
            
        # 5c. Sources.md
        sources_path = vault_path / "brain" / "Sources.md"
        sources_content = f"# Sources Provenance Index\n\n"
        sources_content += "Provenance tracker mapping document identifiers in notes back to their original URLs.\n\n"
        sources_content += "| Doc ID | Title / Source Name | Source Type | URL |\n"
        sources_content += "| --- | --- | --- | --- |\n"
        for doc in raw_docs:
            meta = doc.get("metadata", {})
            title = meta.get("title", "Article Link")
            stype = meta.get("source_type", "web")
            url = meta.get("url", "#")
            sources_content += f"| {doc['id']} | {title} | {stype} | [{url}]({url}) |\n"
            
        with open(sources_path, "w", encoding="utf-8") as f:
            f.write(sources_content)
            
        # 5d. Contradictions.md
        contra_path = vault_path / "brain" / "Contradictions.md"
        contra_content = f"# Identified Contradictions — {subject_name}\n\n"
        # Search for contradict sections in notes
        contradictions_found = []
        for n in notes:
            match = re.search(r'## Contradictions(.*?)(?:##|$)', n["content_md"], re.DOTALL | re.IGNORECASE)
            if match and match.group(1).strip():
                contradictions_found.append((n["title"], match.group(1).strip()))
                
        if contradictions_found:
            for title, text_val in contradictions_found:
                contra_content += f"### From Note: {title}\n\n{text_val}\n\n"
        else:
            contra_content += "*No major contradictions identified in source documents. Information appears cohesive.*\n"
            
        with open(contra_path, "w", encoding="utf-8") as f:
            f.write(contra_content)
            
        # 5e. Open Questions.md
        oq_path = vault_path / "brain" / "Open Questions.md"
        oq_content = f"# Unresolved Gaps — {subject_name}\n\n"
        oq_content += "Topics requiring further information or validation.\n\n"
        oq_content += "- [ ] Clarify early life achievements and chronologies.\n"
        oq_content += "- [ ] Gather additional news documents on recent events.\n"
        
        with open(oq_path, "w", encoding="utf-8") as f:
            f.write(oq_content)
            
        # 6. Create Home.md
        home_path = vault_path / "Home.md"
        home_rendered = _render_template(
            "subject_home.md",
            subject_name=subject_name,
            last_updated=datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC"),
        )
        with open(home_path, "w", encoding="utf-8") as f:
            f.write(home_rendered)
            
        # 7. Create vault-manifest.json
        manifest = {
            "subject_id": subject_id,
            "subject_name": subject_name,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "stats": {
                "entities": len(entities),
                "relationships": len(relationships),
                "notes": len(notes),
                "documents": len(raw_docs)
            },
            "schema_version": "1.0"
        }
        with open(vault_path / "vault-manifest.json", "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
            
        # 8. Copy templates to templates/ folder inside the vault for user convenience
        templates_src = Path(__file__).parent / "templates"
        if templates_src.exists():
            for t_file in templates_src.glob("*.md"):
                shutil.copy2(t_file, vault_path / "templates")
                
        # 9. Write basic .obsidian files
        # Enable graph view and basic configuration
        # Write app.json
        with open(vault_path / ".obsidian" / "app.json", "w") as f:
            json.dump({"useMarkdownLinks": True, "showFrontmatter": True}, f)
            
        db.update_pipeline_stage(subject_id, "vault", "complete")
        logger.info(f"Vault export complete for {subject_name}. Saved to: {vault_path.absolute()}")
        return str(vault_path.absolute())
        
    except Exception as e:
        logger.error(f"Error during vault export for subject {subject_id}: {e}")
        db.update_pipeline_stage(subject_id, "vault", "failed", str(e))
        raise e

