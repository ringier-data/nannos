"""Shared test fixtures and utilities."""

import logging
import time
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import docker
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from moto import mock_aws
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from console_backend.config import (
    Config,
    OidcConfig,
    OrchestratorConfig,
)
from console_backend.controllers.auth_controller import AuthController
from console_backend.db.session import get_db_session
from console_backend.dependencies import require_auth, require_auth_or_bearer_token
from console_backend.models.session import StoredSession
from console_backend.models.user import User, UserRole, UserStatus
from console_backend.repositories.secrets_repository import SecretsRepository
from console_backend.repositories.sub_agent_repository import SubAgentRepository
from console_backend.repositories.user_group_repository import UserGroupRepository
from console_backend.repositories.user_repository import UserRepository
from console_backend.services.audit_service import AuditService
from console_backend.services.notification_service import NotificationService
from console_backend.services.oauth_service import OAuthService
from console_backend.services.secrets_service import SecretsService
from console_backend.services.sub_agent_service import SubAgentService
from console_backend.services.user_group_service import UserGroupService
from console_backend.services.user_service import UserService
from console_backend.services.user_settings_service import UserSettingsService

logger = logging.getLogger(__name__)


@pytest.fixture
def aws_mock():
    """Fixture that provides mocked AWS services (S3, SSM, etc.).

    Use this fixture instead of @mock_aws decorator for async tests.
    The decorator approach doesn't work properly with pytest-asyncio.

    Services that use AWS (like SecretsService) should depend on this fixture
    to ensure the mock is active when the aiobotocore session is created.
    """
    with mock_aws():
        yield


# Docker network cleanup fixture
@pytest.fixture(scope="session", autouse=True)
def cleanup_docker_networks():
    """Clean up Docker networks before and after test session to prevent address pool exhaustion.

    Only removes networks that match test patterns or are associated with test containers.
    """
    docker_client = docker.from_env()

    def cleanup():
        """Remove unused Docker networks related to tests."""
        try:
            # Get all networks
            networks = docker_client.networks.list()
            removed_count = 0

            # Test images we create containers from
            test_images = [
                "docker.rcplus.io/pgvector/pgvector:pg16",
            ]

            for network in networks:
                # Skip default networks
                if network.name in ["bridge", "host", "none"]:
                    continue

                # Only clean up networks that match our test patterns
                is_test_network = network.name.startswith("test-network-") or network.name.startswith(
                    "build-db-container-network"
                )

                if not is_test_network:
                    # Check if any containers in the network use our test images
                    try:
                        network.reload()
                        for container in network.containers:
                            if any(image_name in container.image.tags for image_name in test_images):
                                is_test_network = True
                                break
                    except Exception:
                        pass

                if not is_test_network:
                    continue

                # Remove if unused
                try:
                    network.reload()  # Refresh network data
                    if not network.containers:
                        network.remove()
                        removed_count += 1
                        logger.debug(f"Removed unused test Docker network: {network.name}")
                except docker.errors.NotFound:
                    # Network already removed
                    pass
                except Exception as e:
                    logger.debug(f"Could not remove network {network.name}: {e}")

            if removed_count > 0:
                logger.info(f"Cleaned up {removed_count} unused test Docker network(s)")
        except Exception as e:
            logger.warning(f"Failed to clean up Docker networks: {e}")

    # Clean up before tests
    cleanup()

    yield

    # Clean up after tests
    cleanup()


# Test configuration
@pytest.fixture
def test_config():
    """Create test configuration."""
    return Config(
        environment="local",  # Use 'local' so is_local() returns True for HTTP validation
        base_domain="localhost:9999",
        secret_key="test-secret-key",
        session_ttl_seconds=3600,
        oidc=OidcConfig(
            client_id="test_client_id",
            client_secret=SecretStr("test_client_secret"),
            scope="openid profile email",
        ),
        orchestrator=OrchestratorConfig(
            client_id="orchestrator_client_id",
        ),
    )


@pytest.fixture
def mock_config(test_config, monkeypatch):
    """Mock the global config by patching its attributes."""
    import console_backend.config as config_module

    # Patch config attributes directly on the real config object
    monkeypatch.setattr(config_module.config, "environment", test_config.environment)
    monkeypatch.setattr(config_module.config, "base_domain", test_config.base_domain)
    monkeypatch.setattr(config_module.config, "secret_key", test_config.secret_key)
    monkeypatch.setattr(config_module.config, "session_ttl_seconds", test_config.session_ttl_seconds)
    monkeypatch.setattr(config_module.config, "oidc", test_config.oidc)
    monkeypatch.setattr(config_module.config, "orchestrator", test_config.orchestrator)

    # Also patch the config imported by auth_controller
    import console_backend.controllers.auth_controller

    monkeypatch.setattr(console_backend.controllers.auth_controller, "config", test_config)

    # Patch services that import config
    import console_backend.services.session_service

    monkeypatch.setattr(console_backend.services.session_service, "config", test_config)

    return test_config


@pytest.fixture
def session_service():
    """Create a mock SessionService for tests."""
    service = MagicMock()
    service.create_session = AsyncMock(return_value="test-session-id")
    service.get_session = AsyncMock(return_value=None)
    service.destroy_session = AsyncMock()
    service.update_session = AsyncMock()
    service.get_orchestrator_cookie = AsyncMock(return_value=None)
    service.update_orchestrator_cookie = AsyncMock()
    service.clear_orchestrator_cookie = AsyncMock()
    return service


@pytest.fixture
def notification_service():
    """Create NotificationService mock for testing."""
    service = NotificationService()
    return service


@pytest.fixture
def user_service():
    """Create UserService instance with injected dependencies."""
    audit_service = AuditService()
    user_repo = UserRepository()
    user_repo.set_audit_service(audit_service)
    service = UserService()
    service.set_repository(user_repo)
    service.set_audit_service(audit_service)
    return service


@pytest.fixture
def secrets_service(notification_service):
    """Create SecretsService instance with injected dependencies."""
    audit_service = AuditService()
    secrets_repo = SecretsRepository()
    secrets_repo.set_audit_service(audit_service)
    service = SecretsService()
    service.set_repository(secrets_repo)
    service.set_notification_service(notification_service)
    return service


@pytest.fixture
def sub_agent_service():
    """Create SubAgentService instance with injected dependencies."""
    audit_service = AuditService()
    sub_agent_repo = SubAgentRepository()
    sub_agent_repo.set_audit_service(audit_service)
    service = SubAgentService()
    service.set_repository(sub_agent_repo)
    return service


@pytest.fixture
def user_group_service(sub_agent_service: SubAgentService, notification_service: NotificationService):
    """Create UserGroupService instance with injected dependencies."""
    service = UserGroupService()
    user_group_repo = UserGroupRepository()
    service.set_repository(user_group_repo)
    service.set_sub_agent_service(sub_agent_service)
    service.set_notification_service(notification_service)
    audit_service = AuditService()
    user_group_repo.set_audit_service(audit_service)
    return service


@pytest.fixture
def user_settings_service():
    """Create UserSettingsService instance with injected dependencies."""
    service = UserSettingsService()
    return service


# Fixtures for repositories with DI
@pytest.fixture
def user_repository() -> UserRepository:
    repo = UserRepository()
    repo.set_audit_service(AuditService())
    return repo


@pytest.fixture
def sub_agent_repository() -> SubAgentRepository:
    repo = SubAgentRepository()
    repo.set_audit_service(AuditService())
    return repo


@pytest.fixture
def secrets_repository() -> SecretsRepository:
    repo = SecretsRepository()
    repo.set_audit_service(AuditService())
    return repo


# User fixtures
@pytest.fixture
def test_user() -> User:
    """Create a test user (member role) for use as actor in tests."""
    return User(
        id="test-user-id",
        sub="test-user-sub",
        email="test@example.com",
        first_name="Test",
        last_name="User",
        company_name="Test Company",
        is_administrator=False,
        role=UserRole.MEMBER,
        status=UserStatus.ACTIVE,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def test_admin_user() -> User:
    """Create a test admin user for use as actor in tests."""
    return User(
        id="admin-user-id",
        sub="admin-user-sub",
        email="admin@example.com",
        first_name="Admin",
        last_name="User",
        company_name="Test Company",
        is_administrator=True,
        role=UserRole.ADMIN,
        status=UserStatus.ACTIVE,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def test_approver_user() -> User:
    """Create a test approver user for use as actor in tests."""
    return User(
        id="test-approver-id",
        sub="test-approver-sub",
        email="approver@example.com",
        first_name="Test",
        last_name="Approver",
        company_name="Test Company",
        is_administrator=False,
        role=UserRole.APPROVER,
        status=UserStatus.ACTIVE,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest_asyncio.fixture
async def test_user_db(pg_session: AsyncSession, test_user: User, user_repository: UserRepository) -> User:
    """Create test user in database."""
    await user_repository.create(
        db=pg_session,
        actor=test_user,
        fields={
            "id": test_user.id,
            "sub": test_user.sub,
            "email": test_user.email,
            "first_name": test_user.first_name,
            "last_name": test_user.last_name,
            "is_administrator": test_user.is_administrator,
            "role": test_user.role,
            "status": test_user.status,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        },
        returning="id",
    )
    await pg_session.commit()
    return test_user


@pytest_asyncio.fixture
async def test_admin_user_db(pg_session: AsyncSession, test_admin_user: User, user_repository: UserRepository) -> User:
    """Create test user in database."""
    await user_repository.create(
        db=pg_session,
        actor=test_admin_user,
        fields={
            "id": test_admin_user.id,
            "sub": test_admin_user.sub,
            "email": test_admin_user.email,
            "first_name": test_admin_user.first_name,
            "last_name": test_admin_user.last_name,
            "is_administrator": test_admin_user.is_administrator,
            "role": test_admin_user.role,
            "status": test_admin_user.status,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        },
        returning="id",
    )
    await pg_session.commit()
    return test_admin_user


@pytest_asyncio.fixture
async def test_approver_user_db(
    pg_session: AsyncSession, test_approver_user: User, user_repository: UserRepository
) -> User:
    """Create test user in database."""
    await user_repository.create(
        db=pg_session,
        actor=test_approver_user,
        fields={
            "id": test_approver_user.id,
            "sub": test_approver_user.sub,
            "email": test_approver_user.email,
            "first_name": test_approver_user.first_name,
            "last_name": test_approver_user.last_name,
            "is_administrator": test_approver_user.is_administrator,
            "role": test_approver_user.role,
            "status": test_approver_user.status,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        },
        returning="id",
    )
    await pg_session.commit()
    return test_approver_user


@pytest_asyncio.fixture
async def add_user_to_db(pg_session):
    """Setup test environment with app and db."""

    async def _add_user(user: User):
        # add user to db
        await pg_session.execute(
            text("""
            INSERT INTO users (id, sub, email, first_name, last_name, is_administrator, role, status)
            VALUES (:id, :sub, :email, :first_name, :last_name, :is_administrator, :role, :status)
            """),
            {
                "id": user.id,
                "sub": user.sub,
                "email": user.email,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "is_administrator": user.is_administrator,
                "role": user.role,
                "status": user.status,
            },
        )
        await pg_session.commit()

    return _add_user


# Oidc mock responses
@pytest.fixture
def oidc_token_response() -> dict[str, Any]:
    """Mock Oidc token response."""
    return {
        "access_token": "test_access_token",
        "id_token": "test_id_token",
        "refresh_token": "test_refresh_token",
        "token_type": "Bearer",
        "expires_in": 3600,
    }


@pytest.fixture
def oidc_userinfo_response() -> dict[str, Any]:
    """Mock Oidc userinfo response."""
    return {
        "sub": "test-user-id",
        "email": "test@example.com",
        "given_name": "Test",
        "family_name": "User",
        "company_name": "Test Company",
    }


@pytest.fixture
def oidc_token_exchange_response() -> dict[str, Any]:
    """Mock Oidc token exchange response."""
    return {
        "access_token": "exchanged_access_token",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "openid profile email",
        "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
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
    import console_backend.controllers.auth_controller

    # Create a mock OAuth client
    mock_oidc_client = MagicMock()
    mock_oidc_client.authorize_redirect = AsyncMock()
    mock_oidc_client.authorize_access_token = AsyncMock()
    # Mock load_server_metadata as AsyncMock with proper server metadata
    mock_server_metadata = {
        "end_session_endpoint": "https://test.oidc.com/oauth2/v1/logout",
        "issuer": "https://test.oidc.com",
    }
    mock_oidc_client.load_server_metadata = AsyncMock(return_value=mock_server_metadata)
    mock_oidc_client.server_metadata = mock_server_metadata

    # Patch __getattr__ to return our mock when 'oidc' is accessed
    original_getattr = console_backend.controllers.auth_controller.oauth.__class__.__getattr__

    def mock_getattr(self, key):
        if key == "oidc":
            return mock_oidc_client
        return original_getattr(self, key)

    monkeypatch.setattr(console_backend.controllers.auth_controller.oauth.__class__, "__getattr__", mock_getattr)

    yield mock_oidc_client


@pytest_asyncio.fixture
async def auth_controller(session_service, user_service, mock_config):
    """Create AuthController instance with mocked session service."""
    controller = AuthController(session_service, user_service)
    yield controller


# Helper functions
@pytest.fixture
def create_mock_request():
    """Factory to create mock request objects."""

    def _create(
        cookies: dict[str, str] | None = None,
        query_params: dict[str, str] | None = None,
        user: User | None = None,
        session: StoredSession | None = None,
        session_id: str | None = None,
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
            if name == "index":
                return "https://localhost:9999/"
            return f"https://localhost:9999/{name}"

        request.url_for = MagicMock(side_effect=mock_url_for)

        # Mock app.state with services (for dependency injection)
        app = MagicMock()
        request.app = app

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


# PostgreSQL with Rambler migrations fixtures using template database approach
# This is much faster than recreating containers for each test:
# 1. Session-scoped: Start container, run migrations, mark DB as template
# 2. Function-scoped: Clone template DB for each test, drop after test


@pytest.fixture(scope="session")
def postgres_template():
    """Start PostgreSQL container and create template database with migrations.

    This runs once per test session. The 'console' database becomes a template
    that is cloned for each test, providing fast isolation.
    """
    import os
    import random

    client = docker.from_env()

    # Configuration matching build-db-container.sh
    pg_user = "postgres"
    pg_password = "password"
    pg_database = "console"
    pg_schema = "console"
    pg_port = 5432
    host_port = 5433 + random.randint(0, 100)  # Random port to avoid conflicts

    network_name = f"test-network-{random.randint(1000, 9999)}"
    db_container_name = f"test-postgres-{random.randint(1000, 9999)}"

    # Get migrations directory path
    tests_dir = os.path.dirname(os.path.abspath(__file__))
    # Go up: tests -> console-backend, then into sqlmigrations/ddl
    package_root = os.path.abspath(os.path.join(tests_dir, ".."))
    migrations_dir = os.path.join(package_root, "sqlmigrations", "ddl")
    migrations_dir = os.path.normpath(os.path.realpath(migrations_dir))

    containers_to_cleanup = []

    def cleanup():
        for container in containers_to_cleanup:
            try:
                container.stop(timeout=1)
            except Exception:
                pass
            try:
                container.remove(force=True)
            except Exception:
                pass
        try:
            client.networks.get(network_name).remove()
        except Exception:
            pass

    try:
        # Create network
        client.networks.create(network_name, driver="bridge")

        # Start PostgreSQL container with pgvector extension
        pg_container = client.containers.run(
            "docker.rcplus.io/pgvector/pgvector:pg16",
            detach=True,
            name=db_container_name,
            network=network_name,
            environment={
                "POSTGRES_USER": pg_user,
                "POSTGRES_PASSWORD": pg_password,
                "POSTGRES_DB": pg_database,
            },
            ports={f"{pg_port}/tcp": host_port},
        )
        containers_to_cleanup.append(pg_container)

        # Wait for PostgreSQL to be ready
        max_retries = 60
        for i in range(max_retries):
            try:
                exit_code, output = pg_container.exec_run(
                    f'psql -U {pg_user} -d {pg_database} -c "SELECT 1"',
                )
                if exit_code == 0:
                    logger.info(f"PostgreSQL ready after {i + 1} attempts")
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("PostgreSQL failed to start")

        # Create schema and set search path
        exit_code, output = pg_container.exec_run(
            f'psql -U {pg_user} -d {pg_database} -c "ALTER USER {pg_user} SET search_path TO {pg_schema}"'
        )
        if exit_code != 0:
            raise RuntimeError(f"Failed to set search path: {output.decode()}")

        exit_code, output = pg_container.exec_run(f'psql -U {pg_user} -d {pg_database} -c "CREATE SCHEMA {pg_schema}"')
        if exit_code != 0:
            raise RuntimeError(f"Failed to create schema: {output.decode()}")

        # Install pgvector extension (provisioning step - same as build-db-container.sh)
        exit_code, output = pg_container.exec_run(
            f'psql -U {pg_user} -d {pg_database} -c "CREATE EXTENSION IF NOT EXISTS vector"'
        )
        if exit_code != 0:
            raise RuntimeError(f"Failed to create vector extension: {output.decode()}")

        time.sleep(0.5)

        # Run Rambler migrations
        rambler_result = client.containers.run(
            "docker.rcplus.io/zhaowde/rambler:latest",
            remove=True,
            network=network_name,
            volumes={migrations_dir: {"bind": "/scripts", "mode": "ro"}},
            environment={
                "RAMBLER_DRIVER": "postgresql",
                "RAMBLER_PROTOCOL": "tcp",
                "RAMBLER_HOST": db_container_name,
                "RAMBLER_PORT": str(pg_port),
                "RAMBLER_USER": pg_user,
                "RAMBLER_PASSWORD": pg_password,
                "RAMBLER_DATABASE": pg_database,
                "RAMBLER_DIRECTORY": "/scripts",
                "RAMBLER_TABLE": "migrations",
                "RAMBLER_SCHEMA": pg_schema,
            },
        )
        logger.info(f"Rambler migrations applied: {rambler_result.decode()}")

        # Mark the database as a template for fast cloning
        # First disconnect any sessions (shouldn't be any but just in case)
        pg_container.exec_run(
            f"psql -U {pg_user} -d postgres -c "
            f"\"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{pg_database}' AND pid <> pg_backend_pid()\""
        )
        exit_code, output = pg_container.exec_run(
            f'psql -U {pg_user} -d postgres -c "ALTER DATABASE {pg_database} WITH is_template = true"'
        )
        if exit_code != 0:
            raise RuntimeError(f"Failed to set database as template: {output.decode()}")
        logger.info(f"Database {pg_database} marked as template")

        yield {
            "host": "localhost",
            "port": host_port,
            "user": pg_user,
            "password": pg_password,
            "template_database": pg_database,
            "schema": pg_schema,
            "container": pg_container,
        }

    finally:
        cleanup()


# Counter for unique test database names
_test_db_counter = 0
_test_db_lock = None


def _get_test_db_name():
    """Generate a unique test database name."""
    global _test_db_counter
    import threading

    global _test_db_lock
    if _test_db_lock is None:
        _test_db_lock = threading.Lock()
    with _test_db_lock:
        _test_db_counter += 1
        return f"test_db_{_test_db_counter}"


@pytest.fixture(scope="function")
def postgres_with_migrations(postgres_template):
    """Create a fresh database from template for each test.

    This is FAST because PostgreSQL's TEMPLATE feature copies at the filesystem level.
    """
    test_db_name = _get_test_db_name()
    container = postgres_template["container"]
    pg_user = postgres_template["user"]
    template_db = postgres_template["template_database"]
    schema = postgres_template["schema"]

    # Create database from template
    exit_code, output = container.exec_run(
        f'psql -U {pg_user} -d postgres -c "CREATE DATABASE {test_db_name} TEMPLATE {template_db}"'
    )
    if exit_code != 0:
        raise RuntimeError(f"Failed to create test database: {output.decode()}")

    dsn = f"postgresql+asyncpg://{postgres_template['user']}:{postgres_template['password']}@{postgres_template['host']}:{postgres_template['port']}/{test_db_name}"

    yield {
        "host": postgres_template["host"],
        "port": postgres_template["port"],
        "user": pg_user,
        "password": postgres_template["password"],
        "database": test_db_name,
        "schema": schema,
        "dsn": dsn,
    }

    # Drop the test database after the test
    # First terminate any connections
    container.exec_run(
        f"psql -U {pg_user} -d postgres -c "
        f"\"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{test_db_name}'\""
    )
    container.exec_run(f'psql -U {pg_user} -d postgres -c "DROP DATABASE IF EXISTS {test_db_name}"')


@pytest_asyncio.fixture
async def pg_session(postgres_with_migrations):
    """Create an async SQLAlchemy session for the test database.

    Each test gets its own database cloned from the template,
    so no transaction rollback needed - the DB is dropped after the test.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_async_engine(
        postgres_with_migrations["dsn"],
        echo=False,
    )

    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        await session.execute(text(f"SET search_path TO {postgres_with_migrations['schema']}"))
        yield session

    await engine.dispose()


@pytest.fixture
def test_user_model():
    """Create a test user model for auth override."""
    return User(
        id="test-user-id",
        sub="test-user-id",
        email="test@example.com",
        first_name="Test",
        last_name="User",
        is_administrator=False,
        status=UserStatus.ACTIVE,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest_asyncio.fixture
async def app(pg_session, test_user_model):
    """Create FastAPI app with real database and mocked auth."""

    from app import app

    yield app


@pytest_asyncio.fixture
async def app_with_db(app, pg_session, test_user_model):
    """Create FastAPI app with real database and mocked auth."""
    from fastapi import Request

    # Override get_db_session to use test database
    async def override_get_db():
        yield pg_session

    # Override require_auth to return test user
    def override_require_auth():
        return test_user_model

    # Override require_auth_or_bearer_token to return test user
    async def override_require_auth_or_bearer_token(request: Request):
        return test_user_model

    app.dependency_overrides[get_db_session] = override_get_db
    app.dependency_overrides[require_auth] = override_require_auth
    app.dependency_overrides[require_auth_or_bearer_token] = override_require_auth_or_bearer_token

    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, role)
            VALUES (:id, :sub, :email, :first_name, :last_name, :role)
        """),
        {
            "id": test_user_model.id,
            "sub": test_user_model.sub,
            "email": test_user_model.email,
            "first_name": test_user_model.first_name,
            "last_name": test_user_model.last_name,
            "role": test_user_model.role,
        },
    )
    await pg_session.commit()

    yield app

    # Cleanup: Remove the override after the test
    app.dependency_overrides.pop(get_db_session, None)
    app.dependency_overrides.pop(require_auth, None)
    app.dependency_overrides.pop(require_auth_or_bearer_token, None)


@pytest_asyncio.fixture()
async def client(app):
    """Mock the database using the pg_session."""
    from console_backend.db.connection import force_reset_db_state

    force_reset_db_state()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            # # Add default authentication headers for tests
            # client.headers.update(
            #     {
            #         "Authorization": "Bearer test-token",
            #     }
            # )
            yield client


@pytest_asyncio.fixture()
async def client_with_db(app_with_db):
    """Mock the database using the pg_session."""
    from console_backend.db.connection import force_reset_db_state

    force_reset_db_state()
    async with LifespanManager(app_with_db):
        transport = ASGITransport(app=app_with_db)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            # # Add default authentication headers for tests
            # client.headers.update(
            #     {
            #         "Authorization": "Bearer test-token",
            #     }
            # )
            yield client


@pytest_asyncio.fixture
async def pg_engine(postgres_with_migrations):
    """Create an async SQLAlchemy engine connected to the test PostgreSQL database."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(
        postgres_with_migrations["dsn"],
        echo=False,
    )

    yield engine

    await engine.dispose()


@pytest.fixture
def mock_request(client):
    """Create a mock FastAPI Request with app.state for router tests.

    This fixture is used by router tests that call endpoint functions directly,
    bypassing FastAPI's dependency injection. It provides a mock Request object
    with app.state containing initialized services.
    """

    request = MagicMock()

    # Mock app.state with services (for dependency injection)
    request.app = client._transport.app
    request.state = client._transport.app.state

    return request


# Helper functions
@pytest.fixture
def get_mock_request(client):
    """Factory to create mock request objects."""

    def _create(
        user: User | None = None,
    ):
        request = MagicMock()

        # Mock app.state with services (for dependency injection)
        request.app = client._transport.app
        request.state = client._transport.app.state
        request.state.user = user
        return request

    return _create
