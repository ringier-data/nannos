"""Pytest configuration and fixtures."""

import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from tests.support.marker_gate import remember_markexpr

# Add app directory to Python path for imports
app_dir = Path(__file__).parent.parent / "app"
sys.path.insert(0, str(app_dir))


@pytest.fixture(scope="session", autouse=True)
def download_nltk_data():
    """Download NLTK punkt tokenizer before running tests.

    This fixture runs once per test session and ensures NLTK data
    is available for semantic chunking tests.
    """
    import nltk

    try:
        nltk.data.find("tokenizers/punkt")
    except LookupError:
        nltk.download("punkt", quiet=True)
        nltk.download("punkt_tab", quiet=True)


# Configure pytest-asyncio
pytest_plugins = ("pytest_asyncio",)


@pytest.fixture(scope="session")
def event_loop_policy():
    """Set event loop policy for async tests."""
    import asyncio

    return asyncio.get_event_loop_policy()


def pytest_configure(config):
    """Configure pytest with asyncio support."""
    config.option.asyncio_mode = "auto"

    # Must happen here rather than in tests/integration/conftest.py: that module
    # probes the gateway over the network while being imported, and this is the
    # only hook that runs before the import. Lets the probe be skipped entirely
    # when nothing asked for the integration tier — see tests/support/marker_gate.
    remember_markexpr(getattr(config.option, "markexpr", None))


@pytest.fixture(scope="function")
def dynamodb_table():
    """Create a mocked DynamoDB table for testing.

    Matches the actual CloudFormation configuration with PK/SK schema.
    """
    with mock_aws():
        # Create DynamoDB resource
        dynamodb = boto3.resource("dynamodb", region_name="eu-central-1")

        # Create the table matching the CloudFormation config
        table = dynamodb.create_table(
            TableName="dev-nannos-infrastructure-agents-langgraph-checkpoints",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        )

        # Wait for table to be ready
        table.meta.client.get_waiter("table_exists").wait(
            TableName="dev-nannos-infrastructure-agents-langgraph-checkpoints"
        )

        yield table
