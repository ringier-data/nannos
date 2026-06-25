/**
 * Model Gateway admin API (runtime model registration).
 *
 * Thin throw-on-error wrappers over the generated SDK operations for the model-gateway and
 * model-discovery endpoints, kept only for stable call-site names and a small surface that
 * returns `data` directly. They go through the generated bindings (same shared `client`, so
 * the X-Admin-Mode interceptor still applies).
 *
 * Types are RE-EXPORTED from the generated bindings, never hand-maintained here — a
 * hand-written copy drifts from the backend. The few types below are NOT generated and stay
 * hand-written on purpose:
 *   - `DefaultRole` narrows the backend's free `string` role to the canonical fleet roles.
 *   - `FeatureStatus` / `EmbeddingStatus` shape responses the backend serializes as loose
 *     dicts (no schema in the OpenAPI doc), so there is nothing to re-export.
 */
import {
  consoleListModels,
  costPrefillApiV1AdminModelGatewayModelsModelNameCostPrefillGet,
  deleteModelApiV1AdminModelGatewayModelsModelIdDelete,
  editModelApiV1AdminModelGatewayModelsModelIdPut,
  embeddingStatusApiV1ModelsEmbeddingsStatusGet,
  gatewayUiConfigApiV1AdminModelGatewayConfigGet,
  getSystemStatusApiV1AdminSystemStatusGet,
  listModelsApiV1AdminModelGatewayModelsGet,
  modelCatalogApiV1AdminModelGatewayCatalogGet,
  registerModelApiV1AdminModelGatewayModelsPost,
  setDefaultApiV1AdminModelGatewayModelsModelIdDefaultPost,
  testModelApiV1AdminModelGatewayModelsModelNameTestPost,
} from './generated/sdk.gen';
import type {
  AvailableModel,
  CatalogModel,
  CostPrefill,
  GatewayModel,
  GatewayUiConfig,
  ModelRegistrationRequest,
  ModelRegistrationResponse,
  RateCardPricingEntryInput,
} from './generated/types.gen';

export type {
  AvailableModel,
  CatalogModel,
  CostPrefill,
  GatewayModel,
  GatewayUiConfig,
  ModelRegistrationRequest,
  ModelRegistrationResponse,
};

/** A rate-card pricing entry as submitted on registration (per-million price + flow). */
export type RateCardPricingEntry = RateCardPricingEntryInput;

// Canonical fleet-default roles — mirrors console-backend VALID_ROLES (models/model_gateway.py).
// The backend types the role as a free `string`; we narrow it here so the registration UI's
// role switches and labels stay exhaustive. 'chat' is the standard chat tier; 'chat:low'/
// 'chat:premium' are the low/premium tiers.
export type DefaultRole =
  | 'chat'
  | 'chat:low'
  | 'chat:premium'
  | 'embedding'
  | 'multimodal_embedding'
  | 'search';

/** The live model picker — models registered on the gateway (read by every model dropdown). */
export async function listAvailableModels(): Promise<AvailableModel[]> {
  const { data, error } = await consoleListModels();
  if (error) throw error;
  return (data ?? []) as AvailableModel[];
}

export interface EmbeddingStatus {
  ready: boolean;
  status: 'ready' | 'degraded' | 'disabled';
  model: string | null;
  reason: string | null;
}

/** Whether catalog embedding is usable: default set AND registered on the gateway. */
export async function getEmbeddingStatus(): Promise<EmbeddingStatus> {
  const { data, error } = await embeddingStatusApiV1ModelsEmbeddingsStatusGet();
  if (error) throw error;
  return data as unknown as EmbeddingStatus;
}

export type FeatureStatusLevel = 'ready' | 'limited' | 'degraded' | 'disabled';

export interface FeatureStatus {
  key: string;
  name: string;
  status: FeatureStatusLevel;
  detail: string;
  remediation: string | null;
  /** A capability caveat for a 'limited' feature (works, but with a known limitation). */
  caveat: string | null;
}

/** Admin system-status: per-feature readiness with remediation hints. */
export async function getSystemStatus(): Promise<FeatureStatus[]> {
  const { data, error } = await getSystemStatusApiV1AdminSystemStatusGet();
  if (error) throw error;
  return ((data as { features?: FeatureStatus[] })?.features ?? []) as FeatureStatus[];
}

export async function listModelCatalog(): Promise<CatalogModel[]> {
  const { data, error } = await modelCatalogApiV1AdminModelGatewayCatalogGet();
  if (error) throw error;
  return (data ?? []) as CatalogModel[];
}

/** Deployment defaults the registration form needs (env-driven). */
export async function getGatewayConfig(): Promise<GatewayUiConfig> {
  const { data, error } = await gatewayUiConfigApiV1AdminModelGatewayConfigGet();
  if (error) throw error;
  return data as GatewayUiConfig;
}

export async function listGatewayModels(): Promise<GatewayModel[]> {
  const { data, error } = await listModelsApiV1AdminModelGatewayModelsGet();
  if (error) throw error;
  return (data ?? []) as GatewayModel[];
}

export async function getCostPrefill(modelName: string): Promise<CostPrefill> {
  const { data, error } = await costPrefillApiV1AdminModelGatewayModelsModelNameCostPrefillGet({
    path: { model_name: modelName },
  });
  if (error) throw error;
  return data as CostPrefill;
}

export async function registerGatewayModel(
  body: ModelRegistrationRequest,
): Promise<ModelRegistrationResponse> {
  const { data, error } = await registerModelApiV1AdminModelGatewayModelsPost({ body });
  if (error) throw error;
  return data as ModelRegistrationResponse;
}

export async function updateGatewayModel(
  modelId: string,
  body: ModelRegistrationRequest,
): Promise<ModelRegistrationResponse> {
  const { data, error } = await editModelApiV1AdminModelGatewayModelsModelIdPut({
    path: { model_id: modelId },
    body,
  });
  if (error) throw error;
  return data as ModelRegistrationResponse;
}

export async function testGatewayModel(modelName: string): Promise<{ status: string }> {
  const { data, error } = await testModelApiV1AdminModelGatewayModelsModelNameTestPost({
    path: { model_name: modelName },
  });
  if (error) throw error;
  return (data ?? { status: 'ok' }) as { status: string };
}

export async function setGatewayModelDefault(modelId: string, role: DefaultRole): Promise<void> {
  const { error } = await setDefaultApiV1AdminModelGatewayModelsModelIdDefaultPost({
    path: { model_id: modelId },
    body: { role },
  });
  if (error) throw error;
}

export async function deleteGatewayModel(modelId: string): Promise<void> {
  const { error } = await deleteModelApiV1AdminModelGatewayModelsModelIdDelete({
    path: { model_id: modelId },
  });
  if (error) throw error;
}
