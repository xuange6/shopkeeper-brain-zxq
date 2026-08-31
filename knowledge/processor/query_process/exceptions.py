"""Custom exceptions for the query workflow."""


class QueryProcessError(Exception):
    """Base exception for query processing."""

    def __init__(self, message: str, node_name: str = "", cause: Exception = None):
        self.node_name = node_name
        self.cause = cause
        super().__init__(message)

    def __str__(self):
        parts = []
        if self.node_name:
            parts.append(f"[{self.node_name}]")
        parts.append(super().__str__())
        if self.cause:
            parts.append(f"(cause: {self.cause})")
        return " ".join(parts)


class ConfigurationError(QueryProcessError):
    pass


class SearchError(QueryProcessError):
    pass


class EmbeddingError(QueryProcessError):
    pass


class LLMError(QueryProcessError):
    pass


class StorageError(QueryProcessError):
    pass


class MilvusError(StorageError):
    pass


class Neo4jError(StorageError):
    pass


class MongoDBError(StorageError):
    pass


class ValidationError(QueryProcessError):
    pass


class EntityAlignmentError(QueryProcessError):
    pass


class RerankError(QueryProcessError):
    pass


class ItemNameConfirmError(QueryProcessError):
    pass
