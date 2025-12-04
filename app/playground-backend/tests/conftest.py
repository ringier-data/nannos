"""Shared test fixtures and utilities."""

import logging
import time

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from uuid import uuid4

import boto3
import docker
import httpx
import pytest
import pytest_asyncio

from aiodynamo.client import Client
from aiodynamo.credentials import Key, StaticCredentials
from aiodynamo.http.httpx import HTTPX
from config import (
    Config,
    DynamoDBConfig,
    OidcConfig,
    OrchestratorConfig,
)
from controllers.auth_controller import AuthController
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from models.session import StoredSession
from models.user import User
from services.oauth_service import OAuthService
from services.session_service import SessionService
from services.user_service import UserService
from yarl import URL


logger = logging.getLogger(__name__)


# Mock boto3 credentials for all tests
@pytest.fixture(autouse=True)
def mock_boto3_credentials():
    """Mock boto3 Session.get_credentials() to return test credentials."""
    mock_credentials = Mock()
    mock_credentials.access_key = 'test-access-key'
    mock_credentials.secret_key = 'test-secret-key'
    mock_credentials.token = None

    with patch('boto3.Session.get_credentials', return_value=mock_credentials):
        yield mock_credentials


# Test configuration
@pytest.fixture
def test_config():
    """Create test configuration."""
    return Config(
        environment='local',  # Use 'local' so is_local() returns True for HTTP validation
        base_domain='localhost:9999',
        secret_key='test-secret-key',
        session_ttl_seconds=3600,
        oidc=OidcConfig(
            domain='test.oidc.com',
            client_id='test_client_id',
            client_secret='test_client_secret',
            redirect_uri='http://localhost:9999/api/v1/auth/login-callback',
            logout_redirect_uri='http://localhost:9999/api/v1/auth/logout-callback',
            scope='openid profile email',
        ),
        dynamodb=DynamoDBConfig(
            region='us-east-1',
            users_table='a2a-inspector-users',
            sessions_table='a2a-inspector-sessions',
            conversations_table='a2a-inspector-conversations',
            messages_table='a2a-inspector-messages',
        ),
        orchestrator=OrchestratorConfig(
            client_id='orchestrator_client_id',
            base_url='http://localhost:8080',
        ),
    )


@pytest.fixture
def mock_config(test_config, monkeypatch):
    """Mock the global config by patching its attributes."""
    # The actual config is in backend.config, but services import it as "from config import config"
    # Due to pythonpath including "backend", this resolves to backend.config
    # So we need to patch backend.config.config
    import backend.config

    # Patch config attributes directly on the real config object
    monkeypatch.setattr(backend.config.config, 'environment', test_config.environment)
    monkeypatch.setattr(backend.config.config, 'base_domain', test_config.base_domain)
    monkeypatch.setattr(backend.config.config, 'secret_key', test_config.secret_key)
    monkeypatch.setattr(backend.config.config, 'session_ttl_seconds', test_config.session_ttl_seconds)
    monkeypatch.setattr(backend.config.config, 'oidc', test_config.oidc)
    monkeypatch.setattr(backend.config.config, 'dynamodb', test_config.dynamodb)
    monkeypatch.setattr(backend.config.config, 'orchestrator', test_config.orchestrator)

    # Also patch the config imported by auth_controller
    import controllers.auth_controller

    monkeypatch.setattr(controllers.auth_controller, 'config', test_config)

    # Patch services that import config
    import services.user_service

    monkeypatch.setattr(services.user_service, 'config', test_config)

    import services.session_service

    monkeypatch.setattr(services.session_service, 'config', test_config)

    return test_config


# DynamoDB Local fixtures
@pytest.fixture(scope='session')
def dynamodb_local():
    """Start DynamoDB Local container for the test session."""
    client = docker.from_env()

    # Clean up any existing DynamoDB Local containers or containers using port 8765
    try:
        for container in client.containers.list(all=True):
            try:
                # Check if it's using port 8765
                ports = container.ports.get('8000/tcp') or []
                is_using_port = any(port_mapping.get('HostPort') == '8765' for port_mapping in ports)

                # Also check if it's a DynamoDB Local image
                is_dynamodb = 'amazon/dynamodb-local' in container.image.tags

                if is_using_port or is_dynamodb:
                    try:
                        container.stop(timeout=1)
                    except Exception:
                        pass
                    try:
                        container.remove(force=True)
                    except Exception:
                        pass
            except Exception:
                continue
    except Exception:
        pass  # Ignore cleanup errors

    # Pull and start DynamoDB Local container
    container = client.containers.run(
        'amazon/dynamodb-local:latest',
        command='-jar DynamoDBLocal.jar -inMemory -sharedDb',
        ports={'8000/tcp': 8765},
        detach=True,
        remove=True,
    )

    # Wait a moment for DynamoDB Local to start
    time.sleep(2)

    logger.info(f'DynamoDB Local container started: {container.id}')

    yield 'http://localhost:8765'

    # Cleanup - stop the container
    logger.info(f'Stopping DynamoDB Local container: {container.id}')
    try:
        container.stop(timeout=1)
        logger.info('DynamoDB Local container stopped successfully')
    except Exception as e:
        logger.warning(f'Failed to stop container (may already be removed): {e}')


@pytest.fixture(scope='function')
def dynamodb_tables(dynamodb_local, mock_config):
    """Create DynamoDB tables for each test."""
    # Create boto3 client to set up tables
    dynamodb = boto3.resource(
        'dynamodb',
        endpoint_url=dynamodb_local,
        region_name=mock_config.dynamodb.region,
        aws_access_key_id='test',
        aws_secret_access_key='test',
    )

    # Create users table
    users_table = dynamodb.create_table(
        TableName=mock_config.dynamodb.users_table,
        KeySchema=[{'AttributeName': 'id', 'KeyType': 'HASH'}],
        AttributeDefinitions=[{'AttributeName': 'id', 'AttributeType': 'S'}],
        BillingMode='PAY_PER_REQUEST',
    )

    # Create sessions table
    sessions_table = dynamodb.create_table(
        TableName=mock_config.dynamodb.sessions_table,
        KeySchema=[{'AttributeName': 'session_id', 'KeyType': 'HASH'}],
        AttributeDefinitions=[{'AttributeName': 'session_id', 'AttributeType': 'S'}],
        BillingMode='PAY_PER_REQUEST',
    )

    # Create conversations table with userId (HASH) + conversationId (RANGE)
    # UUIDv7 conversationIds are time-ordered, enabling efficient newest-first queries
    conversations_table = dynamodb.create_table(
        TableName=mock_config.dynamodb.conversations_table,
        KeySchema=[
            {'AttributeName': 'userId', 'KeyType': 'HASH'},
            {'AttributeName': 'conversationId', 'KeyType': 'RANGE'},
        ],
        AttributeDefinitions=[
            {'AttributeName': 'userId', 'AttributeType': 'S'},
            {'AttributeName': 'conversationId', 'AttributeType': 'S'},
        ],
        BillingMode='PAY_PER_REQUEST',
    )

    # Create messages table
    messages_table = dynamodb.create_table(
        TableName=mock_config.dynamodb.messages_table,
        KeySchema=[
            {'AttributeName': 'conversationId', 'KeyType': 'HASH'},
            {'AttributeName': 'sortKey', 'KeyType': 'RANGE'},
        ],
        AttributeDefinitions=[
            {'AttributeName': 'conversationId', 'AttributeType': 'S'},
            {'AttributeName': 'sortKey', 'AttributeType': 'S'},
        ],
        BillingMode='PAY_PER_REQUEST',
    )

    # Wait for tables to be ready
    users_table.wait_until_exists()
    sessions_table.wait_until_exists()
    conversations_table.wait_until_exists()
    messages_table.wait_until_exists()

    # Return URL object for aiodynamo
    yield URL(dynamodb_local)

    # Cleanup: delete tables after test
    users_table.delete()
    sessions_table.delete()
    conversations_table.delete()
    messages_table.delete()


# Service fixtures with aiodynamo clients pointing to DynamoDB Local
@pytest_asyncio.fixture
async def session_service(mock_config, dynamodb_tables):
    """Create SessionService instance with DynamoDB Local."""
    http_client = httpx.AsyncClient()
    client = Client(
        HTTPX(http_client),
        StaticCredentials(Key('test', 'test')),
        mock_config.dynamodb.region,
        endpoint=dynamodb_tables,
    )

    service = SessionService()
    service.client = client
    service.table = client.table(mock_config.dynamodb.sessions_table)

    yield service

    # Cleanup - close the underlying httpx client
    await http_client.aclose()


@pytest_asyncio.fixture
async def user_service(mock_config, dynamodb_tables):
    """Create UserService instance with DynamoDB Local."""
    http_client = httpx.AsyncClient()
    client = Client(
        HTTPX(http_client),
        StaticCredentials(Key('test', 'test')),
        mock_config.dynamodb.region,
        endpoint=dynamodb_tables,
    )

    service = UserService()
    service.client = client
    service.table = client.table(mock_config.dynamodb.users_table)

    yield service

    # Cleanup - close the underlying httpx client
    await http_client.aclose()


# User fixtures
@pytest.fixture
def test_user() -> User:
    """Create a test user."""
    return User(
        id='test-user-id',
        sub='test-user-id',
        email='test@example.com',
        first_name='Test',
        last_name='User',
        company_name='Test Company',
        is_administrator=False,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def test_admin_user() -> User:
    """Create a test admin user."""
    return User(
        id='admin-user-id',
        sub='admin-user-id',
        email='admin@example.com',
        first_name='Admin',
        last_name='User',
        company_name='Test Company',
        is_administrator=True,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def test_session(test_user) -> StoredSession:
    """Create a test session."""
    issued_at = datetime.now(timezone.utc)
    return StoredSession(
        session_id=str(uuid4()),
        user_id=test_user.id,
        refresh_token='test_refresh_token',
        issued_at=issued_at,
        ttl=int((issued_at + timedelta(days=30)).timestamp()),
    )


# Oidc mock responses
@pytest.fixture
def oidc_token_response() -> dict[str, Any]:
    """Mock Oidc token response."""
    return {
        'access_token': 'test_access_token',
        'id_token': 'test_id_token',
        'refresh_token': 'test_refresh_token',
        'token_type': 'Bearer',
        'expires_in': 3600,
    }


@pytest.fixture
def oidc_userinfo_response() -> dict[str, Any]:
    """Mock Oidc userinfo response."""
    return {
        'sub': 'test-user-id',
        'email': 'test@example.com',
        'given_name': 'Test',
        'family_name': 'User',
        'company_name': 'Test Company',
    }


@pytest.fixture
def oidc_token_exchange_response() -> dict[str, Any]:
    """Mock Oidc token exchange response."""
    return {
        'access_token': 'exchanged_access_token',
        'token_type': 'Bearer',
        'expires_in': 3600,
        'scope': 'openid profile',
        'issued_token_type': 'urn:ietf:params:oauth:token-type:access_token',
    }


@pytest.fixture
def mock_httpx_client():
    """Create a mock httpx AsyncClient."""
    mock_client = AsyncMock()
    return mock_client


@pytest_asyncio.fixture
async def oauth_service(mock_config, mock_httpx_client):
    """Create OAuthService instance with mocked httpx client."""
    service = OAuthService(
        client_id=mock_config.oidc.client_id,
        client_secret=mock_config.oidc.client_secret,
        issuer=mock_config.oidc.issuer,
    )
    # Patch the _get_oauth_client method to return our mock
    service._get_oauth_client = AsyncMock(return_value=mock_httpx_client)
    yield service


@pytest.fixture(autouse=True)
def mock_oauth(mock_config, monkeypatch):
    """Mock the oauth.oidc client for all tests.

    This fixture runs automatically for all tests and ensures oauth.oidc
    is available before any controller code tries to access it.
    """
    import backend.controllers.auth_controller

    # Create a mock OAuth client
    mock_oidc_client = MagicMock()
    mock_oidc_client.authorize_redirect = AsyncMock()
    mock_oidc_client.authorize_access_token = AsyncMock()
    # Mock load_server_metadata as AsyncMock with proper server metadata
    mock_server_metadata = {
        'end_session_endpoint': 'https://test.oidc.com/oauth2/v1/logout',
        'issuer': 'https://test.oidc.com',
    }
    mock_oidc_client.load_server_metadata = AsyncMock(return_value=mock_server_metadata)
    mock_oidc_client.server_metadata = mock_server_metadata

    # Patch __getattr__ to return our mock when 'oidc' is accessed
    original_getattr = backend.controllers.auth_controller.oauth.__class__.__getattr__

    def mock_getattr(self, key):
        if key == 'oidc':
            return mock_oidc_client
        return original_getattr(self, key)

    monkeypatch.setattr(backend.controllers.auth_controller.oauth.__class__, '__getattr__', mock_getattr)

    yield mock_oidc_client


@pytest_asyncio.fixture
async def auth_controller(session_service, user_service, mock_config):
    """Create AuthController instance with mocked OAuth."""
    # Don't call register_oauth_provider() in tests - it tries to make real HTTP requests
    # Tests will mock oauth.oidc directly as needed
    controller = AuthController(session_service, user_service)
    yield controller


# FastAPI test client
@pytest.fixture
def app(mock_config):
    """Create FastAPI test app."""
    from routers import auth_router
    from starlette.middleware.sessions import SessionMiddleware

    app = FastAPI()

    # Add SessionMiddleware (required for OAuth)
    app.add_middleware(
        SessionMiddleware,
        secret_key=mock_config.secret_key,
        max_age=600,
        same_site='lax',
        https_only=False,  # Test environment
    )

    # Add an index route for testing redirects
    @app.get('/', name='index')
    async def index(request: Request):
        from fastapi.responses import HTMLResponse

        # Check if user is authenticated
        user = getattr(request.state, 'user', None)
        if not user:
            # Redirect to login if not authenticated
            redirect_to = str(request.url_for('index'))
            login_url = str(request.url_for('login'))
            if redirect_to:
                login_url += f'?redirectTo={redirect_to}'
            return HTMLResponse(status_code=302, headers={'Location': login_url})
        # Serve a simple response if authenticated
        return HTMLResponse('<html><body>Authenticated</body></html>')

    app.include_router(auth_router)
    return app


@pytest.fixture
def client(app):
    """Create test client."""
    return TestClient(app)


# Helper functions
@pytest.fixture
def create_mock_request():
    """Factory to create mock request objects."""

    def _create(
        cookies: dict[str, str] = None,
        query_params: dict[str, str] = None,
        user: User = None,
        session: StoredSession = None,
        session_id: str = None,
    ):
        request = MagicMock()
        request.cookies = MagicMock()
        request.cookies.get = MagicMock(side_effect=lambda k, default=None: (cookies or {}).get(k, default))
        request.query_params = MagicMock()
        request.query_params.get = MagicMock(side_effect=lambda k, default=None: (query_params or {}).get(k, default))

        # Use a simple object for state to allow attribute assignment
        class State:
            pass

        request.state = State()
        request.state.user = user
        request.state.session = session
        request.state.session_id = session_id

        # Mock session dict for Authlib
        request.session = {}

        # Mock url_for to return a proper URL string
        def mock_url_for(name, **path_params):
            if name == 'index':
                return 'https://localhost:9999/'
            return f'https://localhost:9999/{name}'

        request.url_for = MagicMock(side_effect=mock_url_for)

        return request

    return _create


@pytest.fixture
def create_mock_response():
    """Factory to create mock response objects."""

    def _create():
        response = MagicMock()
        response.set_cookie = MagicMock()
        response.delete_cookie = MagicMock()
        return response

    return _create
