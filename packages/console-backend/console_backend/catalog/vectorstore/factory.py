"""Factory for creating VectorStore instances per catalog.

Uses LangChain's VectorStore ABC as the abstraction layer.
One vector index per catalog for clean isolation.
"""

from __future__ import annotations

from langchain_core.embeddings import Embeddings
from langchain_core.vectorstores import VectorStore

from ...config import config


class CatalogVectorStoreFactory:
    """Creates VectorStore instances for catalogs. One index per catalog."""

    @staticmethod
    def create(
        catalog_id: str,
        index_embedding: Embeddings,
        query_embedding: Embeddings,
        backend: str | None = None,
    ) -> VectorStore:
        """Create a VectorStore instance for a catalog.

        Args:
            catalog_id: The catalog ID (used to name the vector index).
            index_embedding: Embeddings instance with document role.
            query_embedding: Embeddings instance with query role.
            backend: Vector store backend ("s3_vectors", "pgvector", etc.).
                     Defaults to config.catalog.vector_store_backend.
        """
        backend = backend or config.catalog.vector_store_backend

        if backend == "s3_vectors":
            from .s3_vectors import create_s3_vector_store

            return create_s3_vector_store(catalog_id, index_embedding, query_embedding)
        elif backend == "pgvector":
            raise NotImplementedError("pgvector backend not yet implemented")
        else:
            raise ValueError(f"Unknown vector store backend: {backend}")
