"""Outbound SCIM 2.0 push service.

Handles asynchronous push of user/group changes to configured outbound SCIM endpoints.
Uses fire-and-forget pattern with retries and audit logging for failures.
"""

import asyncio
import logging
from typing import TYPE_CHECKING

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models.audit import AuditAction, AuditEntityType
from ..services.audit_service import AuditService

if TYPE_CHECKING:
    from ..services.outbound_scim_endpoint_service import OutboundScimEndpointService

logger = logging.getLogger(__name__)

# Retry configuration
MAX_RETRIES = 3
BACKOFF_BASE = 2  # seconds: 2, 4, 8


class OutboundScimPushService:
    """Pushes user/group changes to configured outbound SCIM 2.0 endpoints.

    All push operations are fire-and-forget — they do not block the caller.
    Failures are logged to audit trail and sync state is tracked in the database.
    """

    def __init__(self) -> None:
        self._endpoint_service: "OutboundScimEndpointService | None" = None
        self._audit_service: AuditService | None = None
        self._db_session_factory: async_sessionmaker[AsyncSession] | None = None

    def set_endpoint_service(self, service: "OutboundScimEndpointService") -> None:
        self._endpoint_service = service

    def set_audit_service(self, audit_service: AuditService) -> None:
        self._audit_service = audit_service

    def set_db_session_factory(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._db_session_factory = factory

    @property
    def endpoint_service(self) -> "OutboundScimEndpointService":
        if self._endpoint_service is None:
            raise RuntimeError("OutboundScimEndpointService not set on OutboundScimPushService")
        return self._endpoint_service

    @property
    def audit_service(self) -> AuditService:
        if self._audit_service is None:
            raise RuntimeError("AuditService not set on OutboundScimPushService")
        return self._audit_service

    @property
    def db_session_factory(self) -> async_sessionmaker[AsyncSession]:
        if self._db_session_factory is None:
            raise RuntimeError("db_session_factory not set on OutboundScimPushService")
        return self._db_session_factory

    def push_user(self, user_id: str, operation: str) -> None:
        """Schedule async push of a user change to all active outbound endpoints.

        Args:
            user_id: Internal user ID
            operation: 'create' or 'update'
        """
        asyncio.create_task(
            self._push_user_async(user_id, operation),
            name=f"outbound-scim-push-user-{user_id}-{operation}",
        )

    def push_group(self, group_id: int, operation: str) -> None:
        """Schedule async push of a group change to all active outbound endpoints.

        Args:
            group_id: Internal group ID
            operation: 'create' or 'update'
        """
        asyncio.create_task(
            self._push_group_async(group_id, operation),
            name=f"outbound-scim-push-group-{group_id}-{operation}",
        )

    async def _push_user_async(self, user_id: str, operation: str) -> None:
        """Push user to all active endpoints (background task)."""
        try:
            async with self.db_session_factory() as db:
                endpoints = await self.endpoint_service.get_active_endpoints(db)
                active_endpoints = [ep for ep in endpoints if ep["push_users"]]

            if not active_endpoints:
                return

            tasks = [
                self._push_user_to_endpoint(ep, user_id, operation)
                for ep in active_endpoints
            ]
            await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            logger.error(f"Failed to initiate outbound SCIM push for user {user_id}: {e}")

    async def _push_group_async(self, group_id: int, operation: str) -> None:
        """Push group to all active endpoints (background task)."""
        try:
            async with self.db_session_factory() as db:
                endpoints = await self.endpoint_service.get_active_endpoints(db)
                active_endpoints = [ep for ep in endpoints if ep["push_groups"]]

            if not active_endpoints:
                return

            tasks = [
                self._push_group_to_endpoint(ep, group_id, operation)
                for ep in active_endpoints
            ]
            await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            logger.error(f"Failed to initiate outbound SCIM push for group {group_id}: {e}")

    async def _push_user_to_endpoint(self, endpoint: dict, user_id: str, operation: str) -> None:
        """Push a single user to a single endpoint with retries."""
        endpoint_id = endpoint["id"]
        base_url = endpoint["endpoint_url"].rstrip("/")
        token = endpoint["bearer_token"]

        for attempt in range(MAX_RETRIES):
            try:
                async with self.db_session_factory() as db:
                    # Load user data
                    user_row = await self._fetch_user(db, user_id)
                    if not user_row:
                        logger.warning(f"Outbound SCIM: user {user_id} not found, skipping push")
                        return

                    # Check existing sync state for remote_id
                    sync_state = await self.endpoint_service.get_sync_state(db, endpoint_id, "user", user_id)
                    remote_id = sync_state["remote_id"] if sync_state else None

                # Build SCIM payload
                payload = self._build_scim_user_payload(user_row)

                # Determine HTTP method and URL
                if remote_id:
                    # Update existing remote resource
                    url = f"{base_url}/Users/{remote_id}"
                    method = "PUT"
                else:
                    # Create new remote resource
                    url = f"{base_url}/Users"
                    method = "POST"

                # Send request
                async with httpx.AsyncClient(timeout=30) as client:
                    response = await client.request(
                        method,
                        url,
                        json=payload,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Content-Type": "application/scim+json",
                            "Accept": "application/scim+json",
                        },
                    )

                if response.status_code in (200, 201):
                    # Extract remote ID from response
                    response_data = response.json()
                    new_remote_id = response_data.get("id", remote_id)

                    async with self.db_session_factory() as db:
                        await self.endpoint_service.upsert_sync_state(
                            db,
                            endpoint_id=endpoint_id,
                            entity_type="user",
                            entity_id=user_id,
                            remote_id=new_remote_id,
                        )
                        await db.commit()

                    logger.info(
                        f"Outbound SCIM: {method} user {user_id} to endpoint {endpoint_id} "
                        f"succeeded (remote_id={new_remote_id})"
                    )
                    return  # Success

                elif response.status_code == 409 and method == "POST":
                    # Resource already exists — try to find it and update instead
                    logger.info(f"Outbound SCIM: user {user_id} already exists at endpoint {endpoint_id}, attempting lookup")
                    found_remote_id = await self._lookup_remote_user(base_url, token, user_row)
                    if found_remote_id:
                        async with self.db_session_factory() as db:
                            await self.endpoint_service.upsert_sync_state(
                                db,
                                endpoint_id=endpoint_id,
                                entity_type="user",
                                entity_id=user_id,
                                remote_id=found_remote_id,
                            )
                            await db.commit()
                        # Retry will now use PUT with the found remote_id
                        continue
                    else:
                        error_msg = f"409 Conflict but could not find existing user at remote endpoint"
                        logger.warning(f"Outbound SCIM: {error_msg}")
                else:
                    error_msg = f"HTTP {response.status_code}: {response.text[:500]}"
                    logger.warning(
                        f"Outbound SCIM: {method} user {user_id} to endpoint {endpoint_id} "
                        f"failed (attempt {attempt + 1}/{MAX_RETRIES}): {error_msg}"
                    )

            except httpx.TimeoutException:
                error_msg = f"Request timeout (attempt {attempt + 1}/{MAX_RETRIES})"
                logger.warning(f"Outbound SCIM: push user {user_id} to endpoint {endpoint_id}: {error_msg}")
            except httpx.RequestError as e:
                error_msg = f"Connection error: {e} (attempt {attempt + 1}/{MAX_RETRIES})"
                logger.warning(f"Outbound SCIM: push user {user_id} to endpoint {endpoint_id}: {error_msg}")
            except Exception as e:
                error_msg = f"Unexpected error: {e}"
                logger.error(f"Outbound SCIM: push user {user_id} to endpoint {endpoint_id}: {error_msg}")
                break  # Don't retry unexpected errors

            # Backoff before retry
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(BACKOFF_BASE ** (attempt + 1))

        # All retries exhausted — record failure
        try:
            async with self.db_session_factory() as db:
                await self.endpoint_service.upsert_sync_state(
                    db,
                    endpoint_id=endpoint_id,
                    entity_type="user",
                    entity_id=user_id,
                    last_error=error_msg,
                    increment_retry=True,
                )
                await db.commit()
            logger.error(
                f"Outbound SCIM: push user {user_id} to endpoint {endpoint_id} failed "
                f"after {MAX_RETRIES} attempts: {error_msg}"
            )
        except Exception as e:
            logger.error(f"Outbound SCIM: failed to record sync failure for user {user_id}: {e}")

    async def _push_group_to_endpoint(self, endpoint: dict, group_id: int, operation: str) -> None:
        """Push a single group to a single endpoint with retries."""
        endpoint_id = endpoint["id"]
        base_url = endpoint["endpoint_url"].rstrip("/")
        token = endpoint["bearer_token"]
        entity_id = str(group_id)

        for attempt in range(MAX_RETRIES):
            try:
                async with self.db_session_factory() as db:
                    # Load group data
                    group_row, members = await self._fetch_group_with_members(db, group_id)
                    if not group_row:
                        logger.warning(f"Outbound SCIM: group {group_id} not found, skipping push")
                        return

                    # Check existing sync state for remote_id
                    sync_state = await self.endpoint_service.get_sync_state(db, endpoint_id, "group", entity_id)
                    remote_id = sync_state["remote_id"] if sync_state else None

                # Build SCIM payload
                payload = self._build_scim_group_payload(group_row, members)

                # Determine HTTP method and URL
                if remote_id:
                    url = f"{base_url}/Groups/{remote_id}"
                    method = "PUT"
                else:
                    url = f"{base_url}/Groups"
                    method = "POST"

                # Send request
                async with httpx.AsyncClient(timeout=30) as client:
                    response = await client.request(
                        method,
                        url,
                        json=payload,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Content-Type": "application/scim+json",
                            "Accept": "application/scim+json",
                        },
                    )

                if response.status_code in (200, 201):
                    response_data = response.json()
                    new_remote_id = response_data.get("id", remote_id)

                    async with self.db_session_factory() as db:
                        await self.endpoint_service.upsert_sync_state(
                            db,
                            endpoint_id=endpoint_id,
                            entity_type="group",
                            entity_id=entity_id,
                            remote_id=new_remote_id,
                        )
                        await db.commit()

                    logger.info(
                        f"Outbound SCIM: {method} group {group_id} to endpoint {endpoint_id} "
                        f"succeeded (remote_id={new_remote_id})"
                    )
                    return  # Success

                elif response.status_code == 409 and method == "POST":
                    logger.info(f"Outbound SCIM: group {group_id} already exists at endpoint {endpoint_id}, attempting lookup")
                    found_remote_id = await self._lookup_remote_group(base_url, token, group_row)
                    if found_remote_id:
                        async with self.db_session_factory() as db:
                            await self.endpoint_service.upsert_sync_state(
                                db,
                                endpoint_id=endpoint_id,
                                entity_type="group",
                                entity_id=entity_id,
                                remote_id=found_remote_id,
                            )
                            await db.commit()
                        continue
                    else:
                        error_msg = f"409 Conflict but could not find existing group at remote endpoint"
                        logger.warning(f"Outbound SCIM: {error_msg}")
                else:
                    error_msg = f"HTTP {response.status_code}: {response.text[:500]}"
                    logger.warning(
                        f"Outbound SCIM: {method} group {group_id} to endpoint {endpoint_id} "
                        f"failed (attempt {attempt + 1}/{MAX_RETRIES}): {error_msg}"
                    )

            except httpx.TimeoutException:
                error_msg = f"Request timeout (attempt {attempt + 1}/{MAX_RETRIES})"
                logger.warning(f"Outbound SCIM: push group {group_id} to endpoint {endpoint_id}: {error_msg}")
            except httpx.RequestError as e:
                error_msg = f"Connection error: {e} (attempt {attempt + 1}/{MAX_RETRIES})"
                logger.warning(f"Outbound SCIM: push group {group_id} to endpoint {endpoint_id}: {error_msg}")
            except Exception as e:
                error_msg = f"Unexpected error: {e}"
                logger.error(f"Outbound SCIM: push group {group_id} to endpoint {endpoint_id}: {error_msg}")
                break

            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(BACKOFF_BASE ** (attempt + 1))

        # All retries exhausted
        try:
            async with self.db_session_factory() as db:
                await self.endpoint_service.upsert_sync_state(
                    db,
                    endpoint_id=endpoint_id,
                    entity_type="group",
                    entity_id=entity_id,
                    last_error=error_msg,
                    increment_retry=True,
                )
                await db.commit()
            logger.error(
                f"Outbound SCIM: push group {group_id} to endpoint {endpoint_id} failed "
                f"after {MAX_RETRIES} attempts: {error_msg}"
            )
        except Exception as e:
            logger.error(f"Outbound SCIM: failed to record sync failure for group {group_id}: {e}")

    # ─── Remote Lookup Helpers ───────────────────────────────────────────────

    async def _lookup_remote_user(self, base_url: str, token: str, user_row) -> str | None:
        """Attempt to find an existing user on the remote SCIM server by email."""
        try:
            email = user_row.email
            url = f"{base_url}/Users?filter=userName eq \"{email}\""
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/scim+json",
                    },
                )
            if response.status_code == 200:
                data = response.json()
                resources = data.get("Resources", [])
                if resources:
                    return resources[0].get("id")
        except Exception as e:
            logger.warning(f"Outbound SCIM: failed to lookup remote user by email: {e}")
        return None

    async def _lookup_remote_group(self, base_url: str, token: str, group_row) -> str | None:
        """Attempt to find an existing group on the remote SCIM server by displayName."""
        try:
            name = group_row.name
            url = f"{base_url}/Groups?filter=displayName eq \"{name}\""
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/scim+json",
                    },
                )
            if response.status_code == 200:
                data = response.json()
                resources = data.get("Resources", [])
                if resources:
                    return resources[0].get("id")
        except Exception as e:
            logger.warning(f"Outbound SCIM: failed to lookup remote group by displayName: {e}")
        return None

    # ─── Database Helpers ────────────────────────────────────────────────────

    async def _fetch_user(self, db: AsyncSession, user_id: str):
        """Fetch user row from database."""
        result = await db.execute(
            text("""
                SELECT id, email, first_name, last_name, scim_external_id, scim_user_name,
                       status, phone_number, phone_number_idp, created_at, updated_at
                FROM users
                WHERE id = :id AND deleted_at IS NULL
            """),
            {"id": user_id},
        )
        return result.fetchone()

    async def _fetch_group_with_members(self, db: AsyncSession, group_id: int):
        """Fetch group row and its members from database."""
        # Fetch group
        result = await db.execute(
            text("""
                SELECT id, name, description, scim_external_id, created_at, updated_at
                FROM user_groups
                WHERE id = :id AND deleted_at IS NULL
            """),
            {"id": group_id},
        )
        group_row = result.fetchone()
        if not group_row:
            return None, []

        # Fetch members
        result = await db.execute(
            text("""
                SELECT u.id, u.email, u.first_name, u.last_name
                FROM user_group_members ugm
                JOIN users u ON u.id = ugm.user_id
                WHERE ugm.user_group_id = :group_id AND u.deleted_at IS NULL
            """),
            {"group_id": group_id},
        )
        members = result.fetchall()
        return group_row, members

    # ─── SCIM Payload Builders ───────────────────────────────────────────────

    def _build_scim_user_payload(self, user_row) -> dict:
        """Build a SCIM 2.0 User resource from a database row."""
        payload: dict = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "userName": user_row.email,
            "name": {
                "givenName": user_row.first_name or "",
                "familyName": user_row.last_name or "",
            },
            "emails": [
                {
                    "value": user_row.email,
                    "type": "work",
                    "primary": True,
                }
            ],
            "active": user_row.status == "active",
        }

        if user_row.scim_external_id:
            payload["externalId"] = user_row.scim_external_id

        display_name = f"{user_row.first_name or ''} {user_row.last_name or ''}".strip()
        if display_name:
            payload["displayName"] = display_name

        phone = user_row.phone_number_idp or user_row.phone_number
        if phone:
            payload["phoneNumbers"] = [{"value": phone, "type": "work"}]

        return payload

    def _build_scim_group_payload(self, group_row, members) -> dict:
        """Build a SCIM 2.0 Group resource from a database row and members."""
        payload: dict = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
            "displayName": group_row.name,
            "members": [
                {
                    "value": member.id,
                    "display": f"{member.first_name or ''} {member.last_name or ''}".strip() or member.email,
                }
                for member in members
            ],
        }

        if group_row.scim_external_id:
            payload["externalId"] = group_row.scim_external_id

        return payload

    # ─── Connectivity Test ───────────────────────────────────────────────────

    async def test_endpoint(self, endpoint_url: str, bearer_token: str) -> dict:
        """Test connectivity to a remote SCIM endpoint.

        Attempts to GET /ServiceProviderConfig to verify the endpoint is reachable
        and responds as a SCIM server.
        """
        base_url = endpoint_url.rstrip("/")
        url = f"{base_url}/ServiceProviderConfig"

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {bearer_token}",
                        "Accept": "application/scim+json",
                    },
                )

            if response.status_code == 200:
                return {"success": True, "status_code": 200, "detail": "Endpoint is reachable and responded successfully"}
            else:
                return {
                    "success": False,
                    "status_code": response.status_code,
                    "detail": f"Endpoint returned HTTP {response.status_code}",
                }
        except httpx.TimeoutException:
            return {"success": False, "status_code": None, "detail": "Connection timed out"}
        except httpx.RequestError as e:
            return {"success": False, "status_code": None, "detail": f"Connection failed: {e}"}
