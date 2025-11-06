"""Pytest configuration and fixtures."""

import os
import sys
from pathlib import Path

import pytest
import boto3
from moto import mock_aws

# Add app directory to Python path for imports
app_dir = Path(__file__).parent.parent / "app"
sys.path.insert(0, str(app_dir))


# Configure pytest-asyncio
pytest_plugins = ('pytest_asyncio',)


@pytest.fixture(scope="session")
def event_loop_policy():
    """Set event loop policy for async tests."""
    import asyncio
    return asyncio.get_event_loop_policy()


def pytest_configure(config):
    """Configure pytest with asyncio support."""
    config.option.asyncio_mode = "auto"


@pytest.fixture(scope="function")
def aws_credentials():
    """Mock AWS credentials for moto."""
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "eu-central-1"


@pytest.fixture(scope="function")
def dynamodb_table(aws_credentials):
    """Create a mocked DynamoDB table for testing.
    
    Matches the actual CloudFormation configuration with PK/SK schema.
    """
    with mock_aws():
        # Create DynamoDB resource
        dynamodb = boto3.resource("dynamodb", region_name="eu-central-1")
        
        # Create the table matching the CloudFormation config
        table = dynamodb.create_table(
            TableName="dev-alloy-infrastructure-agents-langgraph-checkpoints",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"}
            ],
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"}
            ]
        )
        
        # Wait for table to be ready
        table.meta.client.get_waiter("table_exists").wait(
            TableName="dev-alloy-infrastructure-agents-langgraph-checkpoints"
        )
        
        yield table
