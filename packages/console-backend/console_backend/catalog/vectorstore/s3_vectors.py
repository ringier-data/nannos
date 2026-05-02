"""S3 Vectors implementation with multimodal embedding support.

Subclasses AmazonS3Vectors to detect image data in document metadata
and produce text+image embeddings via GeminiEmbeddings.embed_with_image().

Convention: pass thumbnail PNG bytes in metadata under the ``IMAGE_METADATA_KEY``
key. The subclass consumes and strips it before storage so raw bytes never
reach S3 metadata.
"""

from __future__ import annotations

import copy
import logging
import uuid
from typing import Any, Iterable, Optional

from langchain_aws.vectorstores.s3_vectors import AmazonS3Vectors
from langchain_core.embeddings import Embeddings

from ...config import config

logger = logging.getLogger(__name__)

# Metadata key carrying raw thumbnail PNG bytes for multimodal embedding.
# Consumed by MultimodalS3Vectors.add_texts() and stripped before storage.
IMAGE_METADATA_KEY = "_image_bytes"

# Large text fields stored in metadata but not indexed for filtering.
# These must be declared as non-filterable so S3 Vectors does not count them
# toward the 2048-byte filterable-metadata limit per record.
_NON_FILTERABLE_KEYS = [
    "_page_content",
    "content",
    "speaker_notes",
    "contextualized_content",
    "document_summary",
    "thumbnail_s3_key",
    "source_ref",
    "title",
    "source_file_name",
    "content_hash",
]


class MultimodalS3Vectors(AmazonS3Vectors):
    """AmazonS3Vectors with multimodal embedding support.

    When a document's metadata contains ``IMAGE_METADATA_KEY`` (raw PNG bytes),
    the embedding is produced via ``embed_with_image(text, image_bytes)`` on the
    index embeddings instance.  Documents without the key fall back to the
    standard ``embed_documents()`` path.

    The image bytes are stripped from metadata before storage — they are only
    used to produce a richer embedding vector.
    """

    def add_texts(
        self,
        texts: Iterable[str],
        metadatas: Optional[list[dict]] = None,
        *,
        ids: Optional[list[str]] = None,
        batch_size: int = 200,
        **kwargs: Any,
    ) -> list[str]:
        texts_list = list(texts)

        if metadatas:
            if len(metadatas) != len(texts_list):
                raise ValueError("Number of metadatas must match number of texts")
        if ids and len(ids) != len(texts_list):
            raise ValueError("Number of IDs must match number of texts")

        if self.embeddings is None:
            raise ValueError("Embeddings object is required for adding texts")

        result_ids: list[str] = []
        for i in range(0, len(texts_list), batch_size):
            vectors: list[dict[str, Any]] = []
            sliced_texts = texts_list[i : i + batch_size]
            sliced_metadatas = metadatas[i : i + batch_size] if metadatas else None

            # --- Embed: multimodal when image bytes are present ---
            embeddings_list: list[list[float]] = []

            # Separate texts with/without images for efficient batching
            text_only_indices: list[int] = []
            text_only_texts: list[str] = []

            for j, text in enumerate(sliced_texts):
                meta = sliced_metadatas[j] if sliced_metadatas else None
                image_bytes: bytes | None = meta.pop(IMAGE_METADATA_KEY, None) if meta else None

                if image_bytes and hasattr(self.embeddings, "embed_with_image"):
                    # Multimodal: text + image in one embedding call (Gemini Embedding 2)
                    embeddings_list.append(self.embeddings.embed_with_image(text, image_bytes))
                else:
                    # Queue for batch text-only embedding
                    embeddings_list.append([])
                    text_only_indices.append(j)
                    text_only_texts.append(text)

            # Batch-embed all text-only documents in one call
            if text_only_texts:
                batch_embeddings = self.embeddings.embed_documents(text_only_texts)
                for idx, emb in zip(text_only_indices, batch_embeddings):
                    embeddings_list[idx] = emb

            # --- Create index on first batch if needed ---
            if i == 0 and self.create_index_if_not_exist:
                if self._get_index() is None:
                    self._create_index(dimension=len(embeddings_list[0]))

            # --- Build vector records ---
            for j, text in enumerate(sliced_texts):
                doc_id = (ids[i + j] if ids else None) or uuid.uuid4().hex
                result_ids.append(doc_id)

                if sliced_metadatas:
                    if self.page_content_metadata_key:
                        metadata = copy.copy(sliced_metadatas[j])
                        metadata[self.page_content_metadata_key] = text
                    else:
                        metadata = sliced_metadatas[j]
                else:
                    if self.page_content_metadata_key:
                        metadata = {self.page_content_metadata_key: text}
                    else:
                        metadata = {}

                vectors.append(
                    {
                        "key": doc_id,
                        "data": {self.data_type: embeddings_list[j]},
                        "metadata": metadata,
                    }
                )

            self.client.put_vectors(
                vectorBucketName=self.vector_bucket_name,
                indexName=self.index_name,
                vectors=vectors,
            )

        return result_ids


def create_s3_vector_store(
    catalog_id: str,
    index_embedding: Embeddings,
    query_embedding: Embeddings,
) -> MultimodalS3Vectors:
    """Create a MultimodalS3Vectors instance for a specific catalog.

    Args:
        catalog_id: Catalog ID used for the index name.
        index_embedding: Embeddings with document role (for add_documents).
        query_embedding: Embeddings with query role (for similarity_search).
    """
    return MultimodalS3Vectors(
        vector_bucket_name=config.catalog.vector_bucket_name,
        index_name=f"catalog-{catalog_id}",
        embedding=index_embedding,
        query_embedding=query_embedding,
        distance_metric="cosine",
        create_index_if_not_exist=True,
        non_filterable_metadata_keys=_NON_FILTERABLE_KEYS,
    )
