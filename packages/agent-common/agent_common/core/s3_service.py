"""S3 service for file operations — backward-compatibility shim.

.. deprecated::
    This module is deprecated. Use ``agent_common.core.object_storage`` instead:

    - ``S3Service`` → ``S3ObjectStorageService``
    - ``get_s3_service()`` → ``get_object_storage_service()``
    - ``parse_s3_uri()`` → ``parse_storage_uri()``

The classes and functions below delegate to the new abstraction layer
so that existing callers continue to work during the migration period.
"""

import logging
import warnings
from typing import Optional

from agent_common.core.object_storage import (
    IObjectStorageService,
    get_object_storage_service,
    parse_storage_uri,
)

logger = logging.getLogger(__name__)


def parse_s3_uri(s3_uri: str) -> tuple[str, str]:
    """Parse an S3 URI into bucket and key.

    .. deprecated:: Use ``parse_storage_uri`` from ``agent_common.core.object_storage`` instead.
    """
    return parse_storage_uri(s3_uri)


class S3Service:
    """Backward-compatible wrapper around IObjectStorageService.

    .. deprecated:: Use ``S3ObjectStorageService`` or ``get_object_storage_service()``
        from ``agent_common.core.object_storage`` instead.
    """

    def __init__(self, region: Optional[str] = None):
        warnings.warn(
            "S3Service is deprecated. Use get_object_storage_service() from "
            "agent_common.core.object_storage instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        self._delegate: IObjectStorageService = get_object_storage_service()

    async def generate_presigned_url(
        self,
        s3_uri: str,
        expiration: int = 3600,
    ) -> str:
        """Generate a presigned URL for an S3 object."""
        return await self._delegate.generate_presigned_url(s3_uri, expiration_seconds=expiration)

    async def upload_content(
        self,
        content: bytes,
        bucket: str,
        key: str,
        content_type: str = "application/octet-stream",
    ) -> str:
        """Upload content to S3. Returns S3 URI."""
        stored = await self._delegate.upload(bucket, key, content, content_type=content_type)
        return stored.uri


# Singleton instance
_s3_service: Optional[S3Service] = None


def get_s3_service() -> S3Service:
    """Get the singleton S3 service instance.

    .. deprecated:: Use ``get_object_storage_service()`` from
        ``agent_common.core.object_storage`` instead.
    """
    global _s3_service
    if _s3_service is None:
        _s3_service = S3Service()
    return _s3_service
