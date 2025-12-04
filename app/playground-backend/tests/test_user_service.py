"""Tests for UserService."""

import pytest

from aiodynamo.expressions import F


@pytest.mark.asyncio
class TestUserService:
    """Test UserService functionality."""

    async def test_upsert_user_create_new(self, user_service):
        """Test creating a new user."""
        user = await user_service.upsert_user(
            sub='new-user-id',
            email='new@example.com',
            first_name='New',
            last_name='User',
            company_name='New Company',
        )

        assert user is not None
        assert user.id == 'new-user-id'
        assert user.sub == 'new-user-id'
        assert user.email == 'new@example.com'
        assert user.first_name == 'New'
        assert user.last_name == 'User'
        assert user.company_name == 'New Company'
        assert user.is_administrator is False
        assert user.language == 'en'
        assert user.agent_urls == []
        assert user.tool_names == []

    async def test_upsert_user_update_existing(self, user_service):
        """Test updating an existing user."""
        # Create initial user
        user1 = await user_service.upsert_user(
            sub='test-user-id',
            email='test@example.com',
            first_name='Test',
            last_name='User',
        )

        # Update the user
        user2 = await user_service.upsert_user(
            sub='test-user-id',
            email='updated@example.com',
            first_name='Updated',
            last_name='Name',
            company_name='New Company',
        )

        # Verify update
        assert user2.id == user1.id
        assert user2.email == 'updated@example.com'
        assert user2.first_name == 'Updated'
        assert user2.last_name == 'Name'
        assert user2.company_name == 'New Company'

    async def test_get_user_not_found(self, user_service):
        """Test getting a non-existent user."""
        user = await user_service.get_user('non-existent-id')
        assert user is None

    async def test_get_user_success(self, user_service):
        """Test getting an existing user."""
        # Create user first
        created_user = await user_service.upsert_user(
            sub='test-user-id',
            email='test@example.com',
            first_name='Test',
            last_name='User',
        )

        # Get the user
        user = await user_service.get_user('test-user-id')

        assert user is not None
        assert user.id == created_user.id
        assert user.email == created_user.email

    async def test_upsert_user_without_company_name(self, user_service):
        """Test creating a user without company name."""
        user = await user_service.upsert_user(
            sub='test-user-id',
            email='test@example.com',
            first_name='Test',
            last_name='User',
        )

        assert user.company_name is None

    async def test_user_editable_fields_preserved_on_relogin(self, user_service):
        """Test that user-editable fields are preserved when user logs in again.

        This verifies the atomic upsert correctly uses SET IF_NOT_EXISTS
        for fields that users can edit (is_administrator, language, agent_urls, tool_names).
        """
        # Create initial user
        user1 = await user_service.upsert_user(
            sub='test-user-id',
            email='test@example.com',
            first_name='Test',
            last_name='User',
        )
        assert user1.is_administrator is False
        assert user1.language == 'en'
        assert user1.agent_urls == []
        assert user1.tool_names == []

        # Simulate admin setting is_administrator=True, user changing language,
        # and adding agent_urls/tool_names via direct DynamoDB update
        # (This mimics what would happen through admin APIs)
        await user_service.table.update_item(
            key={'id': 'test-user-id'},
            update_expression=(
                F('is_administrator').set(True)
                & F('language').set('de')
                & F('agent_urls').set(['https://agent1.example.com'])
                & F('tool_names').set(['tool1', 'tool2'])
            ),
        )

        # Verify the direct update worked
        user_after_admin_update = await user_service.get_user('test-user-id')
        assert user_after_admin_update is not None
        assert user_after_admin_update.is_administrator is True
        assert user_after_admin_update.language == 'de'
        assert user_after_admin_update.agent_urls == ['https://agent1.example.com']
        assert user_after_admin_update.tool_names == ['tool1', 'tool2']

        # Now simulate user logging in again (upsert from OIDC callback)
        user2 = await user_service.upsert_user(
            sub='test-user-id',
            email='newemail@example.com',  # Email changed in IdP
            first_name='Test',
            last_name='User',
        )

        # OIDC-sourced fields should be updated
        assert user2.email == 'newemail@example.com'

        # User-editable fields should be PRESERVED (not reset to defaults)
        assert user2.is_administrator is True  # Should NOT be reset to False
        assert user2.language == 'de'  # Should NOT be reset to 'en'
        assert user2.agent_urls == ['https://agent1.example.com']  # Should NOT be reset to []
        assert user2.tool_names == ['tool1', 'tool2']  # Should NOT be reset to []

        # created_at should also be preserved
        assert user2.created_at == user1.created_at

    async def test_created_at_preserved_on_update(self, user_service):
        """Test that created_at is preserved when updating user."""
        # Create user
        user1 = await user_service.upsert_user(
            sub='test-user-id',
            email='test@example.com',
            first_name='Test',
            last_name='User',
        )
        original_created_at = user1.created_at

        # Update user
        user2 = await user_service.upsert_user(
            sub='test-user-id',
            email='updated@example.com',
            first_name='Updated',
            last_name='User',
        )

        # Verify created_at is the same
        assert user2.created_at == original_created_at
