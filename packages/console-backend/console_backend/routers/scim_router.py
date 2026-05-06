"""SCIM 2.0 protocol router (RFC 7643/7644).

Provides endpoints for identity providers to push Users and Groups.
All endpoints require a valid SCIM bearer token (managed via /api/v1/admin/scim-tokens).
"""

import json
import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.session import get_db_session
from ..dependencies import require_scim_token
from ..models.scim import (
    SCIM_GROUP_SCHEMA,
    SCIM_USER_SCHEMA,
    ScimGroupCreate,
    ScimListResponse,
    ScimMeta,
    ScimPatchOp,
    ScimResourceType,
    ScimSchemaAttribute,
    ScimSchemaDefinition,
    ScimServiceProviderConfig,
    ScimUserCreate,
)
from ..services.scim_service import ScimException, ScimGroupService, ScimUserService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/scim/v2", tags=["scim"], dependencies=[Depends(require_scim_token)])

DbSession = Annotated[AsyncSession, Depends(get_db_session)]

SCIM_CONTENT_TYPE = "application/scim+json"


class _ScimEncoder(json.JSONEncoder):
    """JSON encoder that serializes datetime objects to ISO format strings."""

    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


def _scim_response(content: dict | list, status_code: int = 200) -> Response:
    """Create a SCIM-compliant JSON response."""
    body = json.dumps(content, cls=_ScimEncoder, ensure_ascii=False, separators=(",", ":"))
    return Response(content=body, status_code=status_code, media_type=SCIM_CONTENT_TYPE)


def _get_base_url(request: Request) -> str:
    """Get the base URL for SCIM resource locations."""
    return str(request.base_url).rstrip("/")


def get_scim_user_service(request: Request) -> ScimUserService:
    return request.app.state.scim_user_service


def get_scim_group_service(request: Request) -> ScimGroupService:
    return request.app.state.scim_group_service


# ─── Discovery Endpoints ──────────────────────────────────────────────────────


@router.get("/ServiceProviderConfig")
async def get_service_provider_config() -> JSONResponse:
    """Return SCIM Service Provider Configuration."""
    config = ScimServiceProviderConfig()
    return _scim_response(config.model_dump(exclude_none=True))


@router.get("/ResourceTypes")
async def get_resource_types(request: Request) -> JSONResponse:
    """Return supported SCIM resource types as a ListResponse."""
    base_url = _get_base_url(request)
    resource_types = [
        ScimResourceType(
            id="User",
            name="User",
            endpoint="/Users",
            description="User Account",
            schema_=SCIM_USER_SCHEMA,
            meta=ScimMeta(resourceType="ResourceType", location=f"{base_url}/api/scim/v2/ResourceTypes/User"),
        ),
        ScimResourceType(
            id="Group",
            name="Group",
            endpoint="/Groups",
            description="Group",
            schema_=SCIM_GROUP_SCHEMA,
            meta=ScimMeta(resourceType="ResourceType", location=f"{base_url}/api/scim/v2/ResourceTypes/Group"),
        ),
    ]
    resources = [rt.model_dump(exclude_none=True, by_alias=True) for rt in resource_types]
    return _scim_response({
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
        "totalResults": len(resources),
        "Resources": resources,
    })


@router.get("/Schemas")
async def get_schemas() -> JSONResponse:
    """Return all supported SCIM schemas as a ListResponse (RFC 7643 §7)."""
    schemas = [_user_schema(), _group_schema()]
    return _scim_response({
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
        "totalResults": len(schemas),
        "Resources": schemas,
    })


@router.get("/Schemas/{schema_id:path}")
async def get_schema(schema_id: str) -> JSONResponse:
    """Return a single SCIM schema by URI."""
    schema_map = {
        SCIM_USER_SCHEMA: _user_schema,
        SCIM_GROUP_SCHEMA: _group_schema,
    }
    builder = schema_map.get(schema_id)
    if not builder:
        from ..models.scim import SCIM_ERROR_SCHEMA, ScimError

        error = ScimError(detail="Schema not found", status="404")
        return _scim_response(error.model_dump(exclude_none=True), status_code=404)
    return _scim_response(builder())


def _user_schema() -> dict:
    """Build the User schema definition."""
    schema = ScimSchemaDefinition(
        id=SCIM_USER_SCHEMA,
        name="User",
        description="User Account",
        attributes=[
            ScimSchemaAttribute(
                name="userName",
                type="string",
                multiValued=False,
                required=True,
                uniqueness="server",
                description="Unique identifier for the User, typically email address.",
            ),
            ScimSchemaAttribute(
                name="name",
                type="complex",
                multiValued=False,
                description="The components of the user's name.",
                subAttributes=[
                    ScimSchemaAttribute(
                        name="givenName", type="string", description="Given (first) name."
                    ),
                    ScimSchemaAttribute(
                        name="familyName", type="string", description="Family (last) name."
                    ),
                    ScimSchemaAttribute(
                        name="formatted",
                        type="string",
                        description="Full name formatted for display.",
                    ),
                ],
            ),
            ScimSchemaAttribute(
                name="displayName",
                type="string",
                multiValued=False,
                description="Name displayed to the user.",
            ),
            ScimSchemaAttribute(
                name="emails",
                type="complex",
                multiValued=True,
                description="Email addresses for the User.",
                subAttributes=[
                    ScimSchemaAttribute(
                        name="value", type="string", description="Email address value."
                    ),
                    ScimSchemaAttribute(
                        name="type", type="string", description="Label: work, home, other."
                    ),
                    ScimSchemaAttribute(
                        name="primary", type="boolean", description="Is this the primary email?"
                    ),
                ],
            ),
            ScimSchemaAttribute(
                name="active",
                type="boolean",
                multiValued=False,
                description="Whether the user account is active.",
            ),
            ScimSchemaAttribute(
                name="externalId",
                type="string",
                multiValued=False,
                description="Identifier from the provisioning client.",
                mutability="readWrite",
            ),
            ScimSchemaAttribute(
                name="groups",
                type="complex",
                multiValued=True,
                mutability="readOnly",
                description="Groups the user belongs to.",
                subAttributes=[
                    ScimSchemaAttribute(
                        name="value", type="string", description="Group ID."
                    ),
                    ScimSchemaAttribute(
                        name="display", type="string", description="Group display name."
                    ),
                    ScimSchemaAttribute(
                        name="$ref", type="reference", description="URI of the group resource.",
                        referenceTypes=["Group"],
                    ),
                ],
            ),
        ],
    )
    return schema.model_dump(exclude_none=True)


def _group_schema() -> dict:
    """Build the Group schema definition."""
    schema = ScimSchemaDefinition(
        id=SCIM_GROUP_SCHEMA,
        name="Group",
        description="Group",
        attributes=[
            ScimSchemaAttribute(
                name="displayName",
                type="string",
                multiValued=False,
                required=True,
                description="Human-readable name for the Group.",
            ),
            ScimSchemaAttribute(
                name="members",
                type="complex",
                multiValued=True,
                description="Members of the group.",
                subAttributes=[
                    ScimSchemaAttribute(
                        name="value", type="string", description="Member user ID."
                    ),
                    ScimSchemaAttribute(
                        name="display", type="string", description="Member display name."
                    ),
                    ScimSchemaAttribute(
                        name="$ref", type="reference", description="URI of the member resource.",
                        referenceTypes=["User"],
                    ),
                ],
            ),
            ScimSchemaAttribute(
                name="externalId",
                type="string",
                multiValued=False,
                description="Identifier from the provisioning client.",
            ),
        ],
    )
    return schema.model_dump(exclude_none=True)


# ─── User Endpoints ──────────────────────────────────────────────────────────


@router.post("/Users", status_code=status.HTTP_201_CREATED)
async def create_user(
    request: Request,
    db: DbSession,
    body: ScimUserCreate,
) -> JSONResponse:
    """Create a new user via SCIM provisioning."""
    service = get_scim_user_service(request)
    try:
        user = await service.create_user(db, body, base_url=_get_base_url(request))
        await db.commit()
        return _scim_response(user.model_dump(exclude_none=True, by_alias=True), status_code=201)
    except ScimException as e:
        return _scim_response(e.to_scim_error().model_dump(exclude_none=True), status_code=e.status)


@router.get("/Users")
async def list_users(
    request: Request,
    db: DbSession,
    filter: str | None = Query(None, description="SCIM filter expression"),
    startIndex: int = Query(1, ge=1, description="1-based start index"),
    count: int = Query(100, ge=1, le=200, description="Number of results"),
    sortBy: str | None = Query(None, description="Attribute to sort by"),
    sortOrder: str = Query("ascending", description="Sort order: ascending or descending"),
) -> JSONResponse:
    """List users with optional SCIM filtering."""
    service = get_scim_user_service(request)
    try:
        result = await service.list_users(
            db, filter_str=filter, start_index=startIndex, count=count,
            sort_by=sortBy, sort_order=sortOrder, base_url=_get_base_url(request),
        )
        return _scim_response(result.model_dump(exclude_none=True, by_alias=True))
    except ScimException as e:
        return _scim_response(e.to_scim_error().model_dump(exclude_none=True), status_code=e.status)


@router.get("/Users/{user_id}")
async def get_user(
    request: Request,
    db: DbSession,
    user_id: str,
) -> JSONResponse:
    """Get a single user by ID."""
    service = get_scim_user_service(request)
    try:
        user = await service.get_user(db, user_id, base_url=_get_base_url(request))
        return _scim_response(user.model_dump(exclude_none=True, by_alias=True))
    except ScimException as e:
        return _scim_response(e.to_scim_error().model_dump(exclude_none=True), status_code=e.status)


@router.put("/Users/{user_id}")
async def replace_user(
    request: Request,
    db: DbSession,
    user_id: str,
    body: ScimUserCreate,
) -> JSONResponse:
    """Full replacement of a user (PUT)."""
    service = get_scim_user_service(request)
    try:
        user = await service.replace_user(db, user_id, body, base_url=_get_base_url(request))
        await db.commit()
        return _scim_response(user.model_dump(exclude_none=True, by_alias=True))
    except ScimException as e:
        return _scim_response(e.to_scim_error().model_dump(exclude_none=True), status_code=e.status)


@router.patch("/Users/{user_id}")
async def patch_user(
    request: Request,
    db: DbSession,
    user_id: str,
    body: ScimPatchOp,
) -> JSONResponse:
    """Partial update of a user (PATCH)."""
    service = get_scim_user_service(request)
    try:
        user = await service.patch_user(db, user_id, body, base_url=_get_base_url(request))
        await db.commit()
        return _scim_response(user.model_dump(exclude_none=True, by_alias=True))
    except ScimException as e:
        return _scim_response(e.to_scim_error().model_dump(exclude_none=True), status_code=e.status)


@router.delete("/Users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    request: Request,
    db: DbSession,
    user_id: str,
) -> Response:
    """Soft-delete a user."""
    service = get_scim_user_service(request)
    try:
        await service.delete_user(db, user_id)
        await db.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except ScimException as e:
        return _scim_response(e.to_scim_error().model_dump(exclude_none=True), status_code=e.status)


# ─── Group Endpoints ─────────────────────────────────────────────────────────


@router.post("/Groups", status_code=status.HTTP_201_CREATED)
async def create_group(
    request: Request,
    db: DbSession,
    body: ScimGroupCreate,
) -> JSONResponse:
    """Create a new group via SCIM provisioning."""
    service = get_scim_group_service(request)
    try:
        group = await service.create_group(db, body, base_url=_get_base_url(request))
        await db.commit()
        return _scim_response(group.model_dump(exclude_none=True, by_alias=True), status_code=201)
    except ScimException as e:
        return _scim_response(e.to_scim_error().model_dump(exclude_none=True), status_code=e.status)


@router.get("/Groups")
async def list_groups(
    request: Request,
    db: DbSession,
    filter: str | None = Query(None, description="SCIM filter expression"),
    startIndex: int = Query(1, ge=1, description="1-based start index"),
    count: int = Query(100, ge=1, le=200, description="Number of results"),
) -> JSONResponse:
    """List groups with optional SCIM filtering."""
    service = get_scim_group_service(request)
    try:
        result = await service.list_groups(
            db, filter_str=filter, start_index=startIndex, count=count, base_url=_get_base_url(request)
        )
        return _scim_response(result.model_dump(exclude_none=True, by_alias=True))
    except ScimException as e:
        return _scim_response(e.to_scim_error().model_dump(exclude_none=True), status_code=e.status)


@router.get("/Groups/{group_id}")
async def get_group(
    request: Request,
    db: DbSession,
    group_id: str,
) -> JSONResponse:
    """Get a single group by ID."""
    service = get_scim_group_service(request)
    try:
        group = await service.get_group(db, group_id, base_url=_get_base_url(request))
        return _scim_response(group.model_dump(exclude_none=True, by_alias=True))
    except ScimException as e:
        return _scim_response(e.to_scim_error().model_dump(exclude_none=True), status_code=e.status)


@router.put("/Groups/{group_id}")
async def replace_group(
    request: Request,
    db: DbSession,
    group_id: str,
    body: ScimGroupCreate,
) -> JSONResponse:
    """Full replacement of a group (PUT)."""
    service = get_scim_group_service(request)
    try:
        group = await service.replace_group(db, group_id, body, base_url=_get_base_url(request))
        await db.commit()
        return _scim_response(group.model_dump(exclude_none=True, by_alias=True))
    except ScimException as e:
        return _scim_response(e.to_scim_error().model_dump(exclude_none=True), status_code=e.status)


@router.patch("/Groups/{group_id}")
async def patch_group(
    request: Request,
    db: DbSession,
    group_id: str,
    body: ScimPatchOp,
) -> JSONResponse:
    """Partial update of a group (PATCH)."""
    service = get_scim_group_service(request)
    try:
        group = await service.patch_group(db, group_id, body, base_url=_get_base_url(request))
        await db.commit()
        return _scim_response(group.model_dump(exclude_none=True, by_alias=True))
    except ScimException as e:
        return _scim_response(e.to_scim_error().model_dump(exclude_none=True), status_code=e.status)


@router.delete("/Groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(
    request: Request,
    db: DbSession,
    group_id: str,
) -> Response:
    """Soft-delete a group."""
    service = get_scim_group_service(request)
    try:
        await service.delete_group(db, group_id)
        await db.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except ScimException as e:
        return _scim_response(e.to_scim_error().model_dump(exclude_none=True), status_code=e.status)
