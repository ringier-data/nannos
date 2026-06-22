-- rambler up
-- Add 'budget_setting' to audit_entity_type enum. The admin "update Budget Guard
-- settings" action (admin_budget_router.update_settings) audits with
-- AuditEntityType.BUDGET_SETTING; without this value the audit INSERT fails with
-- `invalid input value for enum audit_entity_type: "budget_setting"` and rolls back the
-- whole settings update.
ALTER TYPE audit_entity_type
ADD VALUE IF NOT EXISTS 'budget_setting';
-- rambler down
-- Note: PostgreSQL does not support removing enum values
-- This is a forward-only migration
