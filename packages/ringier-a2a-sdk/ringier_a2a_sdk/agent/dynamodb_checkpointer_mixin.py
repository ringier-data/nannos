"""DynamoDB checkpointer mixin for LangGraph agents.

Provides a reusable _create_checkpointer() implementation backed by DynamoDB
with optional S3 offloading. Mix this into any LangGraphAgent subclass instead
of re-implementing the same boilerplate each time.

Example::

    class MyBedrockAgent(DynamoDBCheckpointerMixin, LangGraphBedrockAgent):
        # _create_checkpointer() is supplied by the mixin
        def _create_model(self): ...
        def _get_mcp_connections(self): ...

    class MyAgent(DynamoDBCheckpointerMixin, LangGraphAgent):
        # standalone — LangGraphBedrockAgent not required
        def _create_model(self): ...
        def _create_checkpointer(self):
            return DynamoDBCheckpointerMixin._create_checkpointer(self)
"""

import logging
import os

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph_checkpoint_aws import DynamoDBSaver

logger = logging.getLogger(__name__)


class DynamoDBCheckpointerMixin:
    """Mixin that implements _create_checkpointer() using DynamoDB + optional S3.

    Reads all configuration from environment variables so no constructor
    arguments are required.  Works with Python's MRO — place it before the
    base agent class in the class definition:

        class MyAgent(DynamoDBCheckpointerMixin, LangGraphBedrockAgent): ...

    Environment variables:
        CHECKPOINT_DYNAMODB_TABLE_NAME  (required) DynamoDB table name
        CHECKPOINT_AWS_REGION           (default: eu-central-1)
        CHECKPOINT_TTL_DAYS             (default: 14)
        CHECKPOINT_COMPRESSION_ENABLED  (default: true)
        CHECKPOINT_S3_BUCKET_NAME       (optional) enables S3 offloading of
                                        large checkpoints
    """

    def _create_checkpointer(self) -> BaseCheckpointSaver:
        """Create DynamoDB checkpointer with optional S3 offloading.

        Returns:
            Configured DynamoDBSaver instance

        Raises:
            ValueError: If CHECKPOINT_DYNAMODB_TABLE_NAME is not set
        """
        checkpoint_table = os.getenv("CHECKPOINT_DYNAMODB_TABLE_NAME")
        if not checkpoint_table:
            raise ValueError("CHECKPOINT_DYNAMODB_TABLE_NAME environment variable is required")

        checkpoint_region = os.getenv("CHECKPOINT_AWS_REGION", "eu-central-1")
        checkpoint_ttl_days = int(os.getenv("CHECKPOINT_TTL_DAYS", "14"))
        checkpoint_compression = os.getenv("CHECKPOINT_COMPRESSION_ENABLED", "true").lower() == "true"
        checkpoint_s3_bucket = os.getenv("CHECKPOINT_S3_BUCKET_NAME")

        s3_config = None
        if checkpoint_s3_bucket:
            s3_config = {"bucket_name": checkpoint_s3_bucket}
            logger.info(f"S3 offloading enabled for large checkpoints: {checkpoint_s3_bucket}")

        checkpointer = DynamoDBSaver(
            table_name=checkpoint_table,
            region_name=checkpoint_region,
            ttl_seconds=checkpoint_ttl_days * 24 * 60 * 60,
            enable_checkpoint_compression=checkpoint_compression,
            s3_offload_config=s3_config,  # type: ignore[arg-type]
        )

        logger.info(f"Initialized DynamoDB checkpointer: {checkpoint_table}")
        return checkpointer
