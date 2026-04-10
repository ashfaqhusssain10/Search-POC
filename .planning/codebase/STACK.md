# Technology Stack

**Analysis Date:** 2026-04-10

## Languages

**Primary:**
- Python 3.9.6 - Core application language for all scripts and data processing pipelines

## Runtime

**Environment:**
- Python 3.9.6

**Package Manager:**
- pip
- Lockfile: requirements.txt (present, pinned versions)

## Frameworks

**Core Infrastructure:**
- Neo4j Python driver 5.14.0+ - Graph database client for dish/community/platter relationships
- Qdrant client 1.8.0+ - Vector database client for semantic search via embeddings

**AI/Embeddings:**
- OpenAI API 1.30.0+ - LLM client for GPT-4o-mini (summaries) and text-embedding-3-small (vector embeddings)

**Data Processing:**
- NetworkX 3.0+ - Graph algorithms for community detection
- Graspologic 3.3.0+ - Graph learning and visualization utilities

**AWS:**
- boto3 1.34.0+ - AWS SDK for DynamoDB table scanning and data retrieval

**Utilities:**
- python-dotenv 1.0.0+ - Environment variable loading from .env files

## Key Dependencies

**Critical:**
- neo4j (5.14.0+) - Graph database connectivity for storing dishes, platters, communities, and relationships
- qdrant-client (1.8.0+) - Vector search engine for embedding-based dish similarity queries
- openai (1.30.0+) - LLM integration for generating community narratives and embeddings
- boto3 (1.34.0+) - AWS DynamoDB access for canonical dish and platter data
- networkx (3.0+) - Community detection algorithms (Louvain method implied in scripts)
- graspologic (3.3.0+) - Graph learning operations for community analysis

**Supporting:**
- python-dotenv (1.0.0+) - Configuration management via environment variables

## Configuration

**Environment:**
- Loaded via `core/settings.py` from `.env` file using python-dotenv
- Key required variables:
  - `NEO4J_URI` - Bolt connection string (e.g., bolt://localhost:7687)
  - `NEO4J_USER` - Neo4j database user (default: neo4j)
  - `NEO4J_PASSWORD` - Neo4j database password
  - `QDRANT_HOST` - Vector database host (default: localhost)
  - `QDRANT_PORT` - Vector database port (default: 6333)
  - `QDRANT_API_KEY` - Optional Qdrant API key
  - `OPENAI_API_KEY` - OpenAI API key (required)
  - `AWS_REGION` - AWS region (default: ap-south-1)
  - `PLATTERS_TABLE` - DynamoDB platter table name
  - `VARIATIONS_TABLE` - DynamoDB variations table name
  - `DYNAMODB_CSV` - CSV file path for DynamoDB master data
  - `SUPABASE_CSV` - CSV file path for Supabase master data

**Hardcoded Configuration (in `core/settings.py`):**
- `QDRANT_COLLECTION = "item_search_communities"` - Vector collection name
- `EMBEDDING_MODEL = "text-embedding-3-small"` - OpenAI embedding model
- `EMBEDDING_DIM = 1536` - Vector dimension for embeddings
- `QDRANT_SCORE_THRESHOLD = 0.35` - Cosine similarity threshold for search results

## Build/Runtime Tools

**Entry Point Pattern:**
- Scripts run via `python -m scripts.<module_name>` pattern
- Located in: `scripts/` directory
- Core utilities in: `core/` directory

**Development/Local Setup:**
- Virtual environment: `venv/` directory present
- No build step required (pure Python)

## Platform Requirements

**Development:**
- Python 3.9+
- Network access to Neo4j instance (bolt://localhost:7687 default)
- Network access to Qdrant instance (localhost:6333 default)
- Network access to OpenAI API (https://api.openai.com)
- Network access to AWS DynamoDB (ap-south-1 default)
- Valid AWS credentials (via ~/.aws/credentials or environment variables)
- .env file with required secrets

**Production:**
- Python 3.9+ runtime
- Persistent Neo4j instance (graph database)
- Persistent Qdrant instance (vector database)
- AWS DynamoDB access (external, pre-existing tables)
- OpenAI API key with embedding and chat model access
- Configuration via environment variables

---

*Stack analysis: 2026-04-10*
