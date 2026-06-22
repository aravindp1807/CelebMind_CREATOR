# 🧠 Synthetic Brain

Scrape, chunk, embed, cluster, synthesize, and export celebrity knowledge as an
Obsidian vault — one markdown note per entity, `[[wikilinked]]` to related
people, works, and events. The graph view becomes the visual "synthetic brain"
forming itself from whatever got scraped.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Acquisition          │  Intelligence          │  Output     │
│  ──────────           │  ────────────          │  ──────     │
│  Wikipedia spider     │  Chunking              │  Obsidian   │
│  News spider          │  SentenceTransformer   │  vault with │
│  IMDb spider          │  spaCy NER             │  [[wiki-    │
│  Social spider (v2)   │  DBSCAN clustering     │  links]]    │
│                       │  LLM synthesis         │             │
└───────────┬───────────┴───────────┬────────────┴──────┬──────┘
            │                       │                   │
            └───────────────────────┘                   │
                        │                               │
              ┌─────────▼──────────┐          ┌─────────▼──────┐
              │  PostgreSQL        │          │  Obsidian Vault │
              │  + pgvector        │          │  brain/         │
              │  11 tables         │          │  People/        │
              │  VECTOR(384)       │          │  Works/         │
              └────────────────────┘          │  Events/        │
                                              │  Home.md        │
                                              └────────────────┘
```

## Quick Start

### 1. Start the database

```bash
docker-compose up -d
```

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
python -m spacy download en_core_web_sm
```

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env — add your OPENROUTER_API_KEY for LLM synthesis
```

### 4. Run the CLI

```bash
# Full pipeline for one person
python -m src.cli "Albert Einstein"

# Wikipedia only
python -m src.cli "Albert Einstein" --sources wikipedia

# Run a specific stage
python -m src.cli "Albert Einstein" --stage synthesize
```

### 5. Start the web UI

```bash
python -m src.web.app
# Open http://localhost:5000
```

### 6. Open in Obsidian

Open `output/vault/Albert_Einstein/` as an Obsidian vault. Switch to graph
view to see the synthetic brain.

## Project Structure

```
synthetic-brain/
├── config/
│   └── sources.yaml              # per-source profiles
├── src/
│   ├── scrapers/
│   │   ├── dispatcher.py         # parallel spider launcher
│   │   ├── wikipedia_spider.py   # Fetcher-based
│   │   ├── news_spider.py        # sitemap + Fetcher
│   │   ├── imdb_spider.py        # Fetcher-based
│   │   └── social_spider.py      # StealthyFetcher (skeleton)
│   ├── storage/
│   │   └── db.py                 # PostgreSQL + pgvector
│   ├── pipeline/
│   │   ├── chunking.py           # recursive text splitter
│   │   ├── embeddings.py         # SentenceTransformer
│   │   ├── ner.py                # spaCy NER
│   │   ├── clustering.py         # DBSCAN
│   │   └── synthesis.py          # OpenRouter LLM
│   ├── vault/
│   │   ├── obsidian_writer.py    # markdown + [[wikilinks]]
│   │   └── templates/            # per-entity-type templates
│   ├── web/
│   │   ├── app.py                # Flask app
│   │   ├── routes.py             # API + page routes
│   │   ├── static/               # CSS + JS
│   │   └── templates/            # Jinja2 HTML
│   └── cli.py                    # python -m src.cli "Name"
├── docker-compose.yml
├── schema.sql
├── requirements.txt
├── .env.example
└── pyproject.toml
```

## Vault Structure (per subject)

Inspired by [obsidian-mind](https://github.com/breferrari/obsidian-mind):

```
output/vault/Albert_Einstein/
├── Home.md                    # Dashboard with links to all sections
├── vault-manifest.json        # Metadata and schemas
├── brain/
│   ├── Profile.md             # AI-synthesized identity summary
│   ├── Key Facts.md           # Verified facts with sources
│   ├── Sources.md             # Provenance index
│   ├── Contradictions.md      # Conflicting claims
│   └── Open Questions.md      # Knowledge gaps
├── People/
│   ├── Index.md               # Map of Content
│   └── {Name}.md              # One note per person
├── Works/
│   ├── Index.md
│   └── {Title}.md
├── Organizations/
├── Events/
├── Places/
└── templates/                 # Obsidian templates
```

Every note has YAML frontmatter (`entity_type`, `sources`, `confidence`,
`last_updated`, `description`) and `[[wikilinks]]` to related entities.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql://synthetic_brain:changeme@localhost:5432/synthetic_brain` | PostgreSQL connection |
| `OPENROUTER_API_KEY` | — | Required for LLM synthesis |
| `VAULT_PATH` | `./output/vault` | Obsidian vault output directory |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | SentenceTransformer model |
| `SPACY_MODEL` | `en_core_web_sm` | spaCy NER model |
| `FLASK_SECRET_KEY` | — | Flask session secret |

## Pipeline Stages

1. **Scrape** — Dispatches spiders to fetch data from Wikipedia, news, IMDb
2. **Chunk** — Splits raw text into 500-char overlapping chunks
3. **Embed** — Encodes chunks with SentenceTransformer (384-dim vectors)
4. **NER** — Extracts entities (people, places, works, orgs, events) via spaCy
5. **Cluster** — Groups similar chunks with DBSCAN (cosine distance)
6. **Synthesize** — LLM merges each cluster into sourced paragraphs
7. **Vault** — Writes Obsidian vault with `[[wikilinks]]` and frontmatter

## License

MIT
