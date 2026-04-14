"""Environment variable loading for SearchPOC."""

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

NEO4J_URI: str = os.environ["NEO4J_URI"]
NEO4J_USER: str = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD: str = os.environ["NEO4J_PASSWORD"]

QDRANT_HOST: str = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT: int = int(os.getenv("QDRANT_PORT", "6333"))
QDRANT_API_KEY: str | None = os.getenv("QDRANT_API_KEY") or None

OPENAI_API_KEY: str = os.environ["OPENAI_API_KEY"]
GEMINI_API_KEY: str = os.environ["GEMINI_API_KEY"]

DYNAMODB_CSV: str = os.getenv(
    "DYNAMODB_CSV", "Search -POC data - Active DynamoDB Master Data.csv"
)
SUPABASE_CSV: str = os.getenv(
    "SUPABASE_CSV", "Search -POC data - Supabase Master Data.csv"
)

QDRANT_COLLECTION: str = "item_search_communities"
EMBEDDING_MODEL: str = "text-embedding-3-small"
EMBEDDING_DIM: int = 1536
QDRANT_SCORE_THRESHOLD: float = 0.35
