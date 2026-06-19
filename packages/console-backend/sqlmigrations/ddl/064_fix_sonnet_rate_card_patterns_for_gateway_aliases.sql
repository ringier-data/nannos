-- rambler up
-- The gateway now logs usage under the public model alias (LiteLLM `model_group`,
-- e.g. "claude-sonnet-4.5") rather than the resolved Bedrock deployment id
-- ("global.anthropic.claude-sonnet-4-5-20250929-v1:0"). The Sonnet rate-card patterns
-- only matched the hyphenated deployment form (claude-sonnet-4-5), so the dotted alias
-- never matched → $0 cost for all Sonnet traffic. Widen the patterns with a [.-] class
-- so they match both the dotted public alias and the hyphenated deployment id.
-- (062 realigned the `provider` column; this realigns the `model_name_pattern`.)
-- Haiku/Gemini aliases already use the hyphenated/escaped-dot forms their patterns match.
UPDATE rate_cards
   SET model_name_pattern = '^(global\.anthropic\.)?claude-sonnet-4[.-]5.*$'
 WHERE model_name = 'claude-sonnet-4.5'
   AND model_name_pattern = '^(global\.anthropic\.)?claude-sonnet-4-5.*$';

UPDATE rate_cards
   SET model_name_pattern = '^(global\.anthropic\.)?claude-sonnet-4[.-]6.*$'
 WHERE model_name = 'claude-sonnet-4.6'
   AND model_name_pattern = '^(global\.anthropic\.)?claude-sonnet-4-6.*$';

-- rambler down
UPDATE rate_cards
   SET model_name_pattern = '^(global\.anthropic\.)?claude-sonnet-4-5.*$'
 WHERE model_name = 'claude-sonnet-4.5'
   AND model_name_pattern = '^(global\.anthropic\.)?claude-sonnet-4[.-]5.*$';

UPDATE rate_cards
   SET model_name_pattern = '^(global\.anthropic\.)?claude-sonnet-4-6.*$'
 WHERE model_name = 'claude-sonnet-4.6'
   AND model_name_pattern = '^(global\.anthropic\.)?claude-sonnet-4[.-]6.*$';
