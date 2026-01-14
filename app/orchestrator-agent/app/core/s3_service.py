"""S3 service for file operations using aiobotocore.

Provides async methods for:
- Generating presigned URLs for S3 objects
- Reading file content from S3

Used by the orchestrator agent to handle file attachments in messages.
"""

import logging
import os
from typing import Optional
from urllib.parse import urlparse

from aiobotocore.session import get_session

logger = logging.getLogger(__name__)


def parse_s3_uri(s3_uri: str) -> tuple[str, str]:
    """Parse an S3 URI into bucket and key.

    Args:
        s3_uri: S3 URI in format s3://bucket/key

    Returns:
        Tuple of (bucket, key)

    Raises:
        ValueError: If URI format is invalid
    """
    parsed = urlparse(s3_uri)
    if parsed.scheme != "s3":
        raise ValueError(f"Invalid S3 URI scheme: {parsed.scheme}. Expected 's3://'")
    if not parsed.netloc:
        raise ValueError(f"Invalid S3 URI: missing bucket name in {s3_uri}")
    bucket = parsed.netloc
    key = parsed.path.lstrip("/")
    if not key:
        raise ValueError(f"Invalid S3 URI: missing key in {s3_uri}")
    return bucket, key


class S3Service:
    """Async S3 service for file operations.

    Uses aiobotocore for async operations. Credentials are resolved using
    the same pattern as other services (auto credentials for ECS, static for local).
    """

    def __init__(self, region: Optional[str] = None):
        """Initialize the S3 service.

        Args:
            region: AWS region. Defaults to AWS_REGION env var or eu-central-1.
        """
        self.region = region or os.getenv("AWS_REGION", "eu-central-1")
        self._session = get_session()

    async def generate_presigned_url(
        self,
        s3_uri: str,
        expiration: int = 3600,
    ) -> str:
        """Generate a presigned URL for an S3 object.

        Args:
            s3_uri: S3 URI in format s3://bucket/key
            expiration: URL expiration time in seconds (default 1 hour, max 24 hours)

        Returns:
            Presigned URL string

        Raises:
            ValueError: If S3 URI is invalid
            Exception: If presigned URL generation fails
        """
        bucket, key = parse_s3_uri(s3_uri)

        # Clamp expiration to max 24 hours
        expiration = min(expiration, 86400)

        async with self._session.create_client("s3", region_name=self.region) as client:
            url = await client.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=expiration,
            )
        logger.debug(f"Generated presigned URL for {s3_uri} (expires in {expiration}s)")
        return url

    async def upload_content(
        self,
        content: bytes,
        bucket: str,
        key: str,
        content_type: str = "application/octet-stream",
    ) -> str:
        """Upload content to S3.

        Args:
            content: Content bytes to upload
            bucket: S3 bucket name
            key: S3 object key
            content_type: MIME type (default: application/octet-stream)

        Returns:
            S3 URI (s3://bucket/key)

        Raises:
            Exception: If upload fails
        """
        async with self._session.create_client("s3", region_name=self.region) as client:
            await client.put_object(
                Bucket=bucket,
                Key=key,
                Body=content,
                ContentType=content_type,
            )
        s3_uri = f"s3://{bucket}/{key}"
        logger.info(f"Uploaded {len(content)} bytes to {s3_uri}")
        return s3_uri


# Singleton instance
_s3_service: Optional[S3Service] = None


def get_s3_service() -> S3Service:
    """Get the singleton S3 service instance."""
    global _s3_service
    if _s3_service is None:
        _s3_service = S3Service()
    return _s3_service
