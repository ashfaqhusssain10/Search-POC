"""Neo4j and Qdrant connection helpers for SearchPOC."""

from contextlib import contextmanager
from typing import Generator

from neo4j import GraphDatabase, Session
from qdrant_client import QdrantClient

from core.settings import (
    NEO4J_PASSWORD,
    NEO4J_URI,
    NEO4J_USER,
    QDRANT_API_KEY,
    QDRANT_HOST,
    QDRANT_PORT,
)

_neo4j_driver = None
_qdrant_client = None


def get_neo4j_driver():
    """Return a singleton Neo4j driver."""
    global _neo4j_driver
    if _neo4j_driver is None:
        _neo4j_driver = GraphDatabase.driver(
            NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD)
        )
    return _neo4j_driver


@contextmanager
def neo4j_session() -> Generator[Session, None, None]:
    """Context manager yielding a Neo4j session."""
    driver = get_neo4j_driver()
    with driver.session() as session:
        yield session


def get_qdrant_client() -> QdrantClient:
    """Return a singleton Qdrant client.

    Supports both local (host+port) and cloud (full URL) configurations.
    If QDRANT_HOST starts with 'http', it is treated as a full URL.
    """
    global _qdrant_client
    if _qdrant_client is None:
        if QDRANT_HOST.startswith("http"):
            _qdrant_client = QdrantClient(url=QDRANT_HOST, api_key=QDRANT_API_KEY, timeout=60)
        else:
            _qdrant_client = QdrantClient(
                host=QDRANT_HOST,
                port=QDRANT_PORT,
                api_key=QDRANT_API_KEY,
            )
    return _qdrant_client


def close_connections() -> None:
    """Close all open connections."""
    global _neo4j_driver, _qdrant_client
    if _neo4j_driver is not None:
        _neo4j_driver.close()
        _neo4j_driver = None
    if _qdrant_client is not None:
        _qdrant_client.close()
        _qdrant_client = None
