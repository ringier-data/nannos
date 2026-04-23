"""Local filesystem file storage — DEPRECATED.

.. deprecated::
    This module is deprecated. Use ``FileStorageService`` with a
    ``LocalObjectStorageService`` backend from ``agent_common.core.object_storage`` instead.

    The ``FileStorageService`` in ``file_storage_service.py`` now accepts an
    ``IObjectStorageService`` in its constructor and transparently supports
    both S3 and local filesystem backends.

Kept for backward compatibility only. Will be removed in a future release.
"""

import logging
import warnings

from playground_backend.services.object_storage import LocalObjectStorageService

from .file_storage_service import FileStorageService

logger = logging.getLogger(__name__)


class LocalFileStorageService(FileStorageService):
    """Deprecated: use FileStorageService(storage_service=LocalObjectStorageService(...)) instead."""

    def __init__(self, base_path: str | None = None) -> None:
        warnings.warn(
            "LocalFileStorageService is deprecated. Use FileStorageService with "
            "LocalObjectStorageService from agent_common.core.object_storage instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        import os

        local_storage = LocalObjectStorageService(
            root_path=base_path or os.getenv("LOCAL_FILE_STORAGE_PATH", "./local-uploads"),
        )
        super().__init__(storage_service=local_storage)
