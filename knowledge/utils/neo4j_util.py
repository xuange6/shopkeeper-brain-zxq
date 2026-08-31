"""
Neo4j driver utilities for the import pipeline.
"""

from functools import lru_cache

from neo4j import GraphDatabase

from knowledge.processor.import_process.config import get_config
from knowledge.processor.import_process.exceptions import ConfigurationError, Neo4jError


@lru_cache(maxsize=1)
def get_neo4j_driver():
    """Create and cache a Neo4j driver from import configuration."""

    config = get_config()
    if not config.neo4j_uri:
        raise ConfigurationError("NEO4J_URI is not configured")
    if not config.neo4j_username:
        raise ConfigurationError("NEO4J_USERNAME is not configured")
    if not config.neo4j_password:
        raise ConfigurationError("NEO4J_PASSWORD is not configured")

    try:
        driver = GraphDatabase.driver(
            config.neo4j_uri,
            auth=(config.neo4j_username, config.neo4j_password),
        )
        driver.verify_connectivity()
        return driver
    except Exception as exc:
        raise Neo4jError(f"Neo4j connection failed: {exc}", cause=exc)


def close_neo4j_driver() -> None:
    """Close the cached Neo4j driver if it has been created."""

    if get_neo4j_driver.cache_info().currsize == 0:
        return

    driver = get_neo4j_driver()
    driver.close()
    get_neo4j_driver.cache_clear()
