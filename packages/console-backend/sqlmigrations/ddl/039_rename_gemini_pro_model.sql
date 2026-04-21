-- rambler up

-- Rename gemini-3-pro-preview to gemini-3.1-pro-preview
-- The model was renamed by Google (gemini-3-pro-preview no longer exists on Vertex AI)
UPDATE rate_cards
SET model_name = 'gemini-3.1-pro-preview',
    model_name_pattern = '^gemini-3\.1-pro-preview.*$'
WHERE provider = 'google_genai'
  AND model_name = 'gemini-3-pro-preview';

-- rambler down

-- Revert: rename back to gemini-3-pro-preview
UPDATE rate_cards
SET model_name = 'gemini-3-pro-preview',
    model_name_pattern = '^gemini-3-pro-preview.*$'
WHERE provider = 'google_genai'
  AND model_name = 'gemini-3.1-pro-preview';
