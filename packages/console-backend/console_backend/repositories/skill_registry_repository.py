"""Repository for the skill_registry table with automatic audit logging."""

from console_backend.models.audit import AuditEntityType
from console_backend.repositories.base import AuditedRepository


class SkillRegistryRepository(AuditedRepository):
    """CRUD operations for the skill_registry table."""

    def __init__(self) -> None:
        super().__init__(
            entity_type=AuditEntityType.SKILL,
            table_name="skill_registry",
        )
