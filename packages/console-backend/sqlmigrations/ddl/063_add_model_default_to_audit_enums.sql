-- rambler up
-- Add 'model_default' to audit_entity_type enum. The admin "Set fleet default model"
-- action (admin_model_gateway_router.set_default) audits with
-- AuditEntityType.MODEL_DEFAULT; without this value the audit INSERT fails with
-- `invalid input value for enum audit_entity_type: "model_default"` and rolls back the
-- whole Set-Default operation.
ALTER TYPE audit_entity_type
ADD VALUE IF NOT EXISTS 'model_default';
-- rambler down
-- Note: PostgreSQL does not support removing enum values
-- This is a forward-only migration
