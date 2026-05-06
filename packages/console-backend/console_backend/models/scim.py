"""Pydantic models for SCIM 2.0 protocol (RFC 7643/7644)."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# SCIM Schema URIs
SCIM_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
SCIM_GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
SCIM_LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
SCIM_PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
SCIM_ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
SCIM_SP_CONFIG_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"
SCIM_SCHEMA_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Schema"
SCIM_RESOURCE_TYPE_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:ResourceType"


# ─── Meta ─────────────────────────────────────────────────────────────────────


class ScimMeta(BaseModel):
    """SCIM resource metadata."""

    resourceType: str
    created: datetime | None = None
    lastModified: datetime | None = None
    location: str | None = None


# ─── User ─────────────────────────────────────────────────────────────────────


class ScimName(BaseModel):
    """SCIM user name component."""

    givenName: str | None = None
    familyName: str | None = None
    formatted: str | None = None


class ScimEmail(BaseModel):
    """SCIM user email."""

    value: str
    type: str = "work"
    primary: bool = True


class ScimGroupRef(BaseModel):
    """SCIM user's group reference (read-only)."""

    value: str
    display: str | None = None
    ref: str | None = Field(None, alias="$ref")

    class Config:
        populate_by_name = True


class ScimUser(BaseModel):
    """SCIM 2.0 User resource."""

    schemas: list[str] = Field(default_factory=lambda: [SCIM_USER_SCHEMA])
    id: str | None = None
    externalId: str | None = None
    userName: str  # Maps to email
    name: ScimName | None = None
    displayName: str | None = None
    emails: list[ScimEmail] | None = None
    active: bool = True
    groups: list[ScimGroupRef] | None = None
    meta: ScimMeta | None = None

    class Config:
        populate_by_name = True


class ScimUserCreate(BaseModel):
    """Inbound SCIM user creation/replacement request."""

    schemas: list[str] = Field(default_factory=lambda: [SCIM_USER_SCHEMA])
    externalId: str | None = None
    userName: str
    name: ScimName | None = None
    displayName: str | None = None
    emails: list[ScimEmail] | None = None
    active: bool = True


# ─── Group ────────────────────────────────────────────────────────────────────


class ScimMember(BaseModel):
    """SCIM group member reference."""

    value: str  # User ID
    display: str | None = None
    ref: str | None = Field(None, alias="$ref")

    class Config:
        populate_by_name = True


class ScimGroup(BaseModel):
    """SCIM 2.0 Group resource."""

    schemas: list[str] = Field(default_factory=lambda: [SCIM_GROUP_SCHEMA])
    id: str | None = None
    externalId: str | None = None
    displayName: str
    members: list[ScimMember] | None = None
    meta: ScimMeta | None = None

    class Config:
        populate_by_name = True


class ScimGroupCreate(BaseModel):
    """Inbound SCIM group creation/replacement request."""

    schemas: list[str] = Field(default_factory=lambda: [SCIM_GROUP_SCHEMA])
    externalId: str | None = None
    displayName: str
    members: list[ScimMember] | None = None


# ─── PATCH ────────────────────────────────────────────────────────────────────


class ScimPatchOperation(BaseModel):
    """Single SCIM PATCH operation."""

    op: Literal["add", "remove", "replace"]
    path: str | None = None
    value: Any = None


class ScimPatchOp(BaseModel):
    """SCIM PATCH request body."""

    schemas: list[str] = Field(default_factory=lambda: [SCIM_PATCH_SCHEMA])
    Operations: list[ScimPatchOperation]


# ─── List Response ────────────────────────────────────────────────────────────


class ScimListResponse(BaseModel):
    """SCIM 2.0 List Response."""

    schemas: list[str] = Field(default_factory=lambda: [SCIM_LIST_SCHEMA])
    totalResults: int
    startIndex: int = 1
    itemsPerPage: int = 0
    Resources: list[dict[str, Any]] = Field(default_factory=list)


# ─── Error ────────────────────────────────────────────────────────────────────


class ScimError(BaseModel):
    """SCIM 2.0 Error response."""

    schemas: list[str] = Field(default_factory=lambda: [SCIM_ERROR_SCHEMA])
    detail: str
    status: str  # HTTP status code as string per spec
    scimType: str | None = None


# ─── Discovery ────────────────────────────────────────────────────────────────


class ScimBulkConfig(BaseModel):
    supported: bool = False
    maxOperations: int = 0
    maxPayloadSize: int = 0


class ScimFilterConfig(BaseModel):
    supported: bool = True
    maxResults: int = 200


class ScimChangePasswordConfig(BaseModel):
    supported: bool = False


class ScimSortConfig(BaseModel):
    supported: bool = False


class ScimETagConfig(BaseModel):
    supported: bool = False


class ScimPatchConfig(BaseModel):
    supported: bool = True


class ScimAuthScheme(BaseModel):
    type: str = "oauthbearertoken"
    name: str = "OAuth Bearer Token"
    description: str = "Authentication scheme using a Bearer token"


class ScimServiceProviderConfig(BaseModel):
    """SCIM Service Provider Configuration."""

    schemas: list[str] = Field(default_factory=lambda: [SCIM_SP_CONFIG_SCHEMA])
    documentationUri: str | None = None
    patch: ScimPatchConfig = Field(default_factory=ScimPatchConfig)
    bulk: ScimBulkConfig = Field(default_factory=ScimBulkConfig)
    filter: ScimFilterConfig = Field(default_factory=ScimFilterConfig)
    changePassword: ScimChangePasswordConfig = Field(default_factory=ScimChangePasswordConfig)
    sort: ScimSortConfig = Field(default_factory=ScimSortConfig)
    etag: ScimETagConfig = Field(default_factory=ScimETagConfig)
    authenticationSchemes: list[ScimAuthScheme] = Field(
        default_factory=lambda: [ScimAuthScheme()]
    )
    meta: ScimMeta = Field(
        default_factory=lambda: ScimMeta(resourceType="ServiceProviderConfig")
    )


class ScimResourceType(BaseModel):
    """SCIM Resource Type definition."""

    schemas: list[str] = Field(default_factory=lambda: [SCIM_RESOURCE_TYPE_SCHEMA])
    id: str
    name: str
    endpoint: str
    description: str | None = None
    schema_: str = Field(..., alias="schema")
    meta: ScimMeta | None = None

    class Config:
        populate_by_name = True


# ─── Schema Definition ────────────────────────────────────────────────────────


class ScimSchemaAttribute(BaseModel):
    """A single attribute in a SCIM schema definition."""

    name: str
    type: str  # "string", "complex", "boolean", "dateTime", "reference"
    multiValued: bool = False
    description: str = ""
    required: bool = False
    mutability: str = "readWrite"  # "readOnly", "readWrite", "immutable", "writeOnly"
    returned: str = "default"  # "always", "never", "default", "request"
    uniqueness: str = "none"  # "none", "server", "global"
    subAttributes: list["ScimSchemaAttribute"] | None = None
    caseExact: bool = False
    referenceTypes: list[str] | None = None


class ScimSchemaDefinition(BaseModel):
    """SCIM Schema definition (RFC 7643 §7)."""

    schemas: list[str] = Field(default_factory=lambda: [SCIM_SCHEMA_SCHEMA])
    id: str
    name: str
    description: str = ""
    attributes: list[ScimSchemaAttribute] = Field(default_factory=list)
    meta: ScimMeta | None = None
