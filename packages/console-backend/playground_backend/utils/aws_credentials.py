"""Refreshable AWS credentials for aiodynamo using boto3's credential chain.

aiodynamo's built-in Credentials.auto() uses its own httpx-based HTTP client to
reach EKS Pod Identity / IMDS metadata endpoints, which can fail in certain EKS
network configurations. boto3/botocore's credential chain handles these endpoints
reliably and supports automatic token refresh.

This module bridges the two: it implements aiodynamo's credential interface but
delegates to boto3 for actual credential resolution and refresh.
"""

import logging

import boto3
from aiodynamo.credentials import Key

logger = logging.getLogger(__name__)


class BotoRefreshableCredentials:
    """Credentials that delegate to boto3's credential chain with auto-refresh.

    Unlike aiodynamo's StaticCredentials, this calls get_frozen_credentials()
    on each DynamoDB request, allowing boto3 to transparently refresh expired
    tokens (e.g., EKS Pod Identity, IRSA, instance profiles).
    """

    def __init__(self) -> None:
        session = boto3.Session()
        self._credentials = session.get_credentials()
        if self._credentials is None:
            raise RuntimeError("No AWS credentials found by boto3")
        logger.info("Using boto3 refreshable credentials")

    async def get_key(self, http: object) -> Key:
        creds = self._credentials.get_frozen_credentials()
        return Key(
            id=creds.access_key,
            secret=creds.secret_key,
            token=creds.token,
        )
