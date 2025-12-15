
SELECT DISTINCT sa.id,
    sa.name,
    sa.owner_user_id,
    sa.owner_status,
    sa.type,
    sa.current_version,
    sa.default_version,
    sa.is_public,
    sa.deleted_at,
    sa.created_at,
    sa.updated_at,
    u.email as owner_email,
    u.first_name,
    u.last_name,
    cv.id as cv_id,
    cv.version as cv_version,
    cv.version_hash as cv_version_hash,
    cv.release_number as cv_release_number,
    cv.description as cv_description,
    cv.model as cv_model,
    cv.system_prompt as cv_system_prompt,
    cv.agent_url as cv_agent_url,
    cv.mcp_tools as cv_mcp_tools,
    cv.foundry_hostname as cv_foundry_hostname,
    cv.foundry_client_id as cv_foundry_client_id,
    cv.foundry_client_secret_ref as cv_foundry_client_secret_ref,
    s.ssm_parameter_name as cv_foundry_client_secret_ssmkey,
    -- needed for the orchestrator
    cv.foundry_ontology_rid as cv_foundry_ontology_rid,
    cv.foundry_query_api_name as cv_foundry_query_api_name,
    cv.foundry_scopes as cv_foundry_scopes,
    cv.foundry_version as cv_foundry_version,
    cv.change_summary as cv_change_summary,
    cv.status as cv_status,
    cv.approved_by_user_id as cv_approved_by_user_id,
    cv.approved_at as cv_approved_at,
    cv.rejection_reason as cv_rejection_reason,
    cv.deleted_at as cv_deleted_at,
    cv.created_at as cv_created_at,
    (usa.sub_agent_id IS NOT NULL) as is_activated
FROM sub_agents sa
    JOIN users u ON sa.owner_user_id = u.id
    LEFT JOIN sub_agent_config_versions cv ON sa.id = cv.sub_agent_id
    AND sa.current_version = cv.version
    LEFT JOIN secrets s ON cv.foundry_client_secret_ref = s.id
    LEFT JOIN sub_agent_permissions sap ON sa.id = sap.sub_agent_id
    LEFT JOIN user_group_members ugm ON sap.user_group_id = ugm.user_group_id
    LEFT JOIN user_sub_agent_activations usa ON sa.id = usa.sub_agent_id
    AND usa.user_id = '267e9176-4bd7-479f-8e6e-3afa770c3804'
WHERE sa.deleted_at IS NULL -- AND (
    --     (
    --         sa.owner_user_id = '267e9176-4bd7-479f-8e6e-3afa770c3804'
    --     )
    --     OR sa.is_public = TRUE
    --     OR ugm.user_id = '267e9176-4bd7-479f-8e6e-3afa770c3804'
    -- )
    AND (usa.sub_agent_id IS NOT NULL)
ORDER BY sa.updated_at DESC
