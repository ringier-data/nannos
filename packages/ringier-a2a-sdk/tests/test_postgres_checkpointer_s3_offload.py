"""Test PostgreSQL checkpointer with S3 offloading for large blobs.

Tests the S3OffloadingSerde wrapper that transparently offloads checkpoint
blobs exceeding a size threshold to S3, storing compact JSON references in
the database instead.

To run locally:
    CHECKPOINT_S3_BUCKET_NAME=test-checkpoints \
    CHECKPOINT_S3_THRESHOLD_MB=0.001 \  # 1 KB for testing
    pytest tests/test_postgres_checkpointer_s3_offload.py -v

To run with real S3 (requires AWS credentials):
    AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... \
    CHECKPOINT_S3_BUCKET_NAME=your-real-bucket \
    pytest tests/test_postgres_checkpointer_s3_offload.py -v --with-real-s3
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from ringier_a2a_sdk.agent.postgres_checkpointer_mixin import S3OffloadingSerde


class TestS3OffloadingSerde:
    """Test suite for S3OffloadingSerde wrapper."""

    @pytest.fixture
    def serde(self):
        """Create an S3OffloadingSerde with 1 KB threshold for testing."""
        return S3OffloadingSerde(bucket="test-bucket", threshold_bytes=1024)

    def test_small_blob_not_offloaded(self, serde):
        """Blobs smaller than threshold pass through unchanged."""
        small_data = b"small payload"
        type_tag, data = serde.dumps_typed(small_data)

        # Should use inner serializer's type tag (not "s3ref")
        assert type_tag != "s3ref"
        assert data is not None

    def test_large_blob_offloaded_to_s3(self, serde):
        """Blobs larger than threshold are uploaded to S3 and replaced with JSON reference."""
        large_data = b"x" * 2000  # 2000 bytes > 1024 byte threshold

        with patch.object(serde, "_get_s3") as mock_s3_client:
            mock_client = MagicMock()
            mock_s3_client.return_value = mock_client

            type_tag, data = serde.dumps_typed(large_data)

            # Should return s3ref type tag
            assert type_tag == "s3ref"

            # Should call S3 put_object
            mock_client.put_object.assert_called_once()
            call_kwargs = mock_client.put_object.call_args.kwargs
            assert call_kwargs["Bucket"] == "test-bucket"
            assert "checkpoints/" in call_kwargs["Key"]
            assert call_kwargs["Body"] == large_data

            # Reference should be compact JSON with s3_key and original_type
            ref = json.loads(data)
            assert "s3_key" in ref
            assert "original_type" in ref
            assert ref["s3_key"].startswith("checkpoints/")

    def test_load_small_blob(self, serde):
        """Small blobs are deserialized directly."""
        original = {"content": "test", "metadata": {"key": "value"}}

        # Mock the inner serializer
        with patch.object(serde._inner, "dumps_typed", return_value=("dict", b'{"test": "data"}')):
            with patch.object(serde._inner, "loads_typed", return_value=original):
                type_tag, data = serde.dumps_typed(original)
                result = serde.loads_typed((type_tag, data))

        assert result == original

    def test_load_large_blob_from_s3(self, serde):
        """Large blobs are fetched from S3 and deserialized."""
        original_data = {"agent_state": "running", "messages": ["hello", "world"] * 100}
        s3_key = "checkpoints/abc-123-def"
        reference = {"s3_key": s3_key, "original_type": "dict"}

        with patch.object(serde, "_get_s3") as mock_s3_client:
            mock_client = MagicMock()
            mock_s3_client.return_value = mock_client

            # Mock S3 get_object to return the original data
            mock_response = {"Body": MagicMock()}
            mock_response["Body"].read.return_value = b'{"agent_state": "running"}'
            mock_client.get_object.return_value = mock_response

            # Mock inner deserializer
            with patch.object(serde._inner, "loads_typed", return_value=original_data):
                result = serde.loads_typed(("s3ref", json.dumps(reference).encode()))

            # Should have called S3 get_object
            mock_client.get_object.assert_called_once_with(Bucket="test-bucket", Key=s3_key)

            # Should have deserialized the retrieved data
            assert result == original_data

    def test_s3_key_is_uuid_based(self, serde):
        """S3 keys are UUID-based for uniqueness."""
        large_data = b"x" * 2000

        with patch.object(serde, "_get_s3") as mock_s3_client:
            mock_client = MagicMock()
            mock_s3_client.return_value = mock_client

            serde.dumps_typed(large_data)
            call_kwargs = mock_client.put_object.call_args.kwargs
            s3_key = call_kwargs["Key"]

            # Key should be checkpoints/<uuid>
            parts = s3_key.split("/")
            assert len(parts) == 2
            assert parts[0] == "checkpoints"
            # UUID validation (36 chars with dashes)
            assert len(parts[1]) == 36
            assert parts[1].count("-") == 4

    def test_threshold_exactly_at_limit(self, serde):
        """Blobs exactly at threshold should not be offloaded."""
        data_at_threshold = b"x" * 1024  # exactly threshold

        with patch.object(serde, "_get_s3") as mock_s3_client:
            mock_client = MagicMock()
            mock_s3_client.return_value = mock_client

            type_tag, _ = serde.dumps_typed(data_at_threshold)

            # Should not be offloaded (len(data) <= threshold)
            assert type_tag != "s3ref"
            mock_client.put_object.assert_not_called()

    def test_threshold_one_byte_over_limit(self, serde):
        """Blobs one byte over threshold should be offloaded."""
        data_over_threshold = b"x" * 1025  # one byte over threshold

        with patch.object(serde, "_get_s3") as mock_s3_client:
            mock_client = MagicMock()
            mock_s3_client.return_value = mock_client

            type_tag, _ = serde.dumps_typed(data_over_threshold)

            # Should be offloaded (len(data) > threshold)
            assert type_tag == "s3ref"
            mock_client.put_object.assert_called_once()

    def test_s3_client_lazy_initialization(self, serde):
        """S3 client is initialized lazily on first use."""
        assert serde._s3 is None

        with patch("boto3.client") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.return_value = mock_client

            client1 = serde._get_s3()
            client2 = serde._get_s3()

            # boto3.client should be called only once
            mock_boto3.assert_called_once_with("s3")
            # Same client instance returned
            assert client1 is client2
            assert serde._s3 is client1


class TestS3OffloadingIntegration:
    """Integration tests with mock PostgreSQL checkpointer.

    These tests verify that S3 offloading works correctly when integrated
    with AsyncPostgresSaver.
    """

    @pytest.mark.asyncio
    async def test_checkpoint_with_s3_offloading_mocked(self):
        """Verify checkpoint workflow with S3 offloading (all mocked)."""
        serde = S3OffloadingSerde(bucket="test-bucket", threshold_bytes=1024)

        # Simulate a large checkpoint state
        large_state = {
            "messages": [{"role": "user", "content": "x" * 500} for _ in range(10)],
            "metadata": {"user_id": "user-123", "session": "session-456"},
        }

        with patch.object(serde, "_get_s3") as mock_s3_client:
            mock_client = MagicMock()
            mock_s3_client.return_value = mock_client

            # Simulate serialization
            import pickle

            serialized = pickle.dumps(large_state)
            type_tag, data = serde.dumps_typed(serialized)

            if len(serialized) > 1024:
                # Should have been offloaded
                assert type_tag == "s3ref"
                mock_client.put_object.assert_called_once()

                ref = json.loads(data)
                assert "s3_key" in ref
                assert ref["s3_key"].startswith("checkpoints/")


@pytest.mark.skipif(
    not os.getenv("WITH_REAL_S3"),
    reason="Requires real AWS S3 credentials and bucket",
)
class TestS3OffloadingRealS3:
    """Integration tests with real S3 (opt-in via WITH_REAL_S3=1).

    Requires:
    - AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY
    - S3 bucket accessible via those credentials
    - CHECKPOINT_S3_BUCKET_NAME environment variable

    To run:
        WITH_REAL_S3=1 CHECKPOINT_S3_BUCKET_NAME=my-test-bucket \
        pytest tests/test_postgres_checkpointer_s3_offload.py::TestS3OffloadingRealS3 -v
    """

    @pytest.fixture
    def real_serde(self):
        """Create S3OffloadingSerde with real S3 credentials."""
        bucket = os.getenv("CHECKPOINT_S3_BUCKET_NAME")
        if not bucket:
            pytest.skip("CHECKPOINT_S3_BUCKET_NAME not set")
        return S3OffloadingSerde(bucket=bucket, threshold_bytes=1024)

    def test_upload_and_download_real_s3(self, real_serde):
        """Test actual S3 upload and download."""
        large_payload = b"x" * 2000
        type_tag, data = real_serde.dumps_typed(large_payload)

        assert type_tag == "s3ref"
        ref = json.loads(data)
        s3_key = ref["s3_key"]

        # Verify object was uploaded to S3
        s3 = real_serde._get_s3()
        try:
            response = s3.head_object(Bucket=real_serde._bucket, Key=s3_key)
            assert response["ContentLength"] == len(large_payload)
        finally:
            # Cleanup: delete the test object
            s3.delete_object(Bucket=real_serde._bucket, Key=s3_key)

    def test_roundtrip_through_s3(self, real_serde):
        """Test serialization roundtrip through S3."""
        import pickle

        original = {"state": "active", "data": [1, 2, 3] * 100}
        serialized = pickle.dumps(original)

        # Serialize (offload to S3)
        type_tag, offloaded_data = real_serde.dumps_typed(serialized)
        assert type_tag == "s3ref"
        ref = json.loads(offloaded_data)
        s3_key = ref["s3_key"]

        try:
            # Deserialize (fetch from S3)
            deserialized = real_serde.loads_typed(("s3ref", offloaded_data))
            assert pickle.loads(deserialized) == original
        finally:
            # Cleanup
            s3 = real_serde._get_s3()
            s3.delete_object(Bucket=real_serde._bucket, Key=s3_key)
