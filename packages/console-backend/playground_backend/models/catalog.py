"""Pydantic models for the catalog system."""

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

# --- Enums ---


class CatalogSourceType(str, Enum):
    """Catalog source type enum matching database enum."""

    GOOGLE_DRIVE = "google_drive"


class CatalogSourceKind(str, Enum):
    """Kind of source within a catalog (e.g. entire shared drive, subfolder, or user-shared folder)."""

    SHARED_DRIVE = "shared_drive"
    DRIVE_FOLDER = "drive_folder"
    SHARED_FOLDER = "shared_folder"


class CatalogStatus(str, Enum):
    """Catalog status enum matching database enum."""

    ACTIVE = "active"
    SYNCING = "syncing"
    ERROR = "error"
    DISABLED = "disabled"


class CatalogConnectionStatus(str, Enum):
    """Catalog connection status enum matching database enum."""

    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


class CatalogSyncJobStatus(str, Enum):
    """Catalog sync job status enum matching database enum."""

    PENDING = "pending"
    RUNNING = "running"
    REINDEXING = "reindexing"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# --- Response Models ---


class CatalogOwner(BaseModel):
    """Owner information for catalog responses."""

    id: str
    name: str
    email: str


class Catalog(BaseModel):
    """Catalog entity."""

    id: str
    name: str
    description: str | None = None
    owner_user_id: str
    owner: CatalogOwner | None = None
    source_type: CatalogSourceType
    source_config: dict[str, Any] = Field(default_factory=dict)
    status: CatalogStatus = CatalogStatus.ACTIVE
    has_connection: bool = False
    total_pages: int = 0
    indexed_pages: int = 0
    last_synced_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Config:
        from_attributes = True


class CatalogPermission(BaseModel):
    """Catalog permission entry."""

    id: int
    catalog_id: str
    user_group_id: int
    permissions: list[str] = Field(default_factory=lambda: ["read"])
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CatalogConnection(BaseModel):
    """Catalog OAuth connection (token details excluded for security)."""

    id: str
    catalog_id: str
    connector_user_id: str
    token_expiry: datetime | None = None
    scopes: list[str] = Field(default_factory=list)
    connected_at: datetime | None = None
    status: CatalogConnectionStatus = CatalogConnectionStatus.ACTIVE
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CatalogFileSyncStatus(str, Enum):
    """Sync status for individual catalog files."""

    PENDING = "pending"
    SYNCING = "syncing"
    SYNCED = "synced"
    FAILED = "failed"
    SKIPPED = "skipped"


class CatalogFile(BaseModel):
    """Catalog file (document-level metadata)."""

    id: str
    catalog_id: str
    source_file_id: str
    source_file_name: str
    mime_type: str | None = None
    folder_path: str | None = None
    page_count: int | None = None
    indexed_pages: int = 0
    indexing_excluded: bool = False
    sync_status: CatalogFileSyncStatus = CatalogFileSyncStatus.SYNCED
    skip_reason: str | None = None
    summary: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    source_modified_at: datetime | None = None
    synced_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Config:
        from_attributes = True


class CatalogPage(BaseModel):
    """Catalog page (individual slide/page within a file)."""

    id: str
    catalog_id: str
    file_id: str
    page_number: int
    title: str | None = None
    text_content: str | None = None
    speaker_notes: str | None = None
    content_hash: str | None = None
    thumbnail_s3_key: str | None = None
    source_ref: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    indexed_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Config:
        from_attributes = True


class CatalogSyncJob(BaseModel):
    """Catalog sync job status."""

    id: str
    catalog_id: str
    status: CatalogSyncJobStatus = CatalogSyncJobStatus.PENDING
    total_files: int = 0
    processed_files: int = 0
    failed_files: int = 0
    error_details: dict[str, Any] | list[dict[str, Any]] | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Config:
        from_attributes = True


class CatalogSource(BaseModel):
    """A single source within a catalog (e.g. one shared drive or one shared folder)."""

    id: str
    type: CatalogSourceKind
    drive_id: str | None = None
    drive_name: str | None = None
    folder_id: str | None = None
    folder_name: str | None = None
    exclude_folder_patterns: list[str] = Field(default_factory=list)


# --- Request Models ---


class CatalogCreate(BaseModel):
    """Request model for creating a catalog."""

    name: str
    description: str | None = None
    source_type: CatalogSourceType
    source_config: dict[str, Any] = Field(default_factory=dict)


class CatalogUpdate(BaseModel):
    """Request model for updating a catalog."""

    name: str | None = None
    description: str | None = None
    source_config: dict[str, Any] | None = None


class CatalogPermissionUpdate(BaseModel):
    """Request model for updating catalog permissions."""

    permissions: list[dict[str, Any]]
    """List of {user_group_id: int, permissions: list[str]} entries."""


class UpdateFileIndexing(BaseModel):
    """Request model for toggling file indexing exclusion."""

    indexing_excluded: bool


class AddSourceRequest(BaseModel):
    """Request model for adding a source to a catalog."""

    type: CatalogSourceKind
    drive_id: str | None = None
    drive_name: str | None = None
    folder_id: str | None = None
    folder_name: str | None = None
    exclude_folder_patterns: list[str] = Field(default_factory=list)


class UpdateSourceRequest(BaseModel):
    """Request model for updating a source's settings (e.g. exclusion patterns)."""

    exclude_folder_patterns: list[str] = Field(default_factory=list)


# --- List Response Models ---


class CatalogListResponse(BaseModel):
    """Paginated catalog list response."""

    items: list[Catalog]
    total: int


class CatalogFileListResponse(BaseModel):
    """List response for catalog files."""

    items: list[CatalogFile]
    total: int


class CatalogPageListResponse(BaseModel):
    """List response for catalog pages."""

    items: list[CatalogPage]
    total: int


class CatalogSyncJobListResponse(BaseModel):
    """List response for sync job history."""

    items: list[CatalogSyncJob]
    total: int
