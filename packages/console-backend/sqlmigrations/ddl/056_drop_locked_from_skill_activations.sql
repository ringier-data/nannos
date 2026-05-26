-- rambler up
-- Step 1: Migrate existing locked rows to scope='sub-agent' (before dropping column)
UPDATE skill_activations
SET scope = 'sub-agent'
WHERE locked = TRUE;
-- Step 2: Drop existing constraints that reference locked or the old scope values
ALTER TABLE skill_activations DROP CONSTRAINT chk_activation_scope;
ALTER TABLE skill_activations DROP CONSTRAINT skill_activations_scope_check;
-- Step 3: Drop the locked column
ALTER TABLE skill_activations DROP COLUMN locked;
-- Step 4: Add updated constraints
-- Scope values: 'personal', 'group', 'sub-agent'
ALTER TABLE skill_activations
ADD CONSTRAINT skill_activations_scope_check CHECK (scope IN ('personal', 'group', 'sub-agent'));
-- Ensure correct nullability per scope
ALTER TABLE skill_activations
ADD CONSTRAINT chk_activation_scope CHECK (
        (
            scope = 'personal'
            AND user_id IS NOT NULL
        )
        OR (
            scope = 'group'
            AND group_id IS NOT NULL
        )
        OR (scope = 'sub-agent')
    );
-- rambler down
-- Restore locked column
ALTER TABLE skill_activations
ADD COLUMN locked BOOLEAN NOT NULL DEFAULT FALSE;
-- Convert sub-agent scope back to group+locked
UPDATE skill_activations
SET scope = 'group',
    locked = TRUE
WHERE scope = 'sub-agent';
-- Restore original constraints
ALTER TABLE skill_activations DROP CONSTRAINT IF EXISTS skill_activations_scope_check;
ALTER TABLE skill_activations DROP CONSTRAINT IF EXISTS chk_activation_scope;
ALTER TABLE skill_activations
ADD CONSTRAINT skill_activations_scope_check CHECK (scope IN ('personal', 'group'));
ALTER TABLE skill_activations
ADD CONSTRAINT chk_activation_scope CHECK (
        locked = TRUE
        OR (
            scope = 'personal'
            AND user_id IS NOT NULL
        )
        OR (
            scope = 'group'
            AND group_id IS NOT NULL
        )
    );
-- Revert sub-agent scope back to group (original storage)
UPDATE skill_activations
SET scope = 'group'
WHERE scope = 'sub-agent';
-- Restore original constraints
ALTER TABLE skill_activations DROP CONSTRAINT skill_activations_scope_check;
ALTER TABLE skill_activations
ADD CONSTRAINT skill_activations_scope_check CHECK (scope IN ('personal', 'group'));
ALTER TABLE skill_activations DROP CONSTRAINT chk_activation_scope;
ALTER TABLE skill_activations
ADD CONSTRAINT chk_activation_scope CHECK (
        locked = TRUE
        OR (
            scope = 'personal'
            AND user_id IS NOT NULL
        )
        OR (
            scope = 'group'
            AND group_id IS NOT NULL
        )
    );
