/**
 * Model Gateway admin API (runtime model registration, Q6).
 *
 * Hand-written wrappers for the console-backend admin endpoints
 * (`/api/v1/admin/model-gateway/*`) which the generated SDK doesn't describe yet.
 * Uses the shared `client` so the X-Admin-Mode interceptor is applied.
 *
 * TODO: replace with generated bindings after `npm run gen-sdk`.
 */
import { client } from './generated/client.gen';

export type FlowDirection = 'input' | 'output' | 'other';

export interface RateCardPricingEntry {
  price_per_million: number;
  flow_direction: FlowDirection;
}

export type DefaultRole = 'chat' | 'embedding' | 'multimodal_embedding';

export interface GatewayModel {
  model_name: string;
  model_id?: string | null;
  provider?: string | null;
  litellm_model?: string | null;
  mode?: string | null;
  input_modes?: string[];
  default_roles?: DefaultRole[];
  db_model?: boolean;
  input_cost_per_token?: number | null;
  output_cost_per_token?: number | null;
  supports_reasoning?: boolean | null;
  supports_vision?: boolean | null;
}

export interface ModelRegistrationRequest {
  model_name: string;
  litellm_params: Record<string, unknown>;
  model_info?: Record<string, unknown>;
  mode?: 'chat' | 'embedding';
  input_modes: string[];
  provider: string;
  pricing: Record<string, RateCardPricingEntry>;
  model_name_pattern?: string | null;
}

export interface ModelRegistrationResponse {
  model_name: string;
  rate_card_entry_ids: number[];
  gateway_model_id?: string | null;
  status: string;
}

export interface CostPrefill {
  pricing: Record<string, RateCardPricingEntry>;
  source: string;
}

export interface CatalogModel {
  model_id: string;
  provider?: string | null;
  mode: string;
  input_cost_per_token?: number | null;
  output_cost_per_token?: number | null;
  cache_read_input_token_cost?: number | null;
  cache_creation_input_token_cost?: number | null;
  max_input_tokens?: number | null;
  supports_vision: boolean;
  supports_reasoning: boolean;
  supports_audio_input: boolean;
  supports_pdf_input: boolean;
}

const BASE = '/api/v1/admin/model-gateway';

export interface AvailableModel {
  value: string;
  label: string;
  provider: string;
  supports_thinking: boolean;
  thinking_levels?: string[] | null;
  is_default: boolean;
}

/** The live model picker — models registered on the gateway (read by every model dropdown). */
export async function listAvailableModels(): Promise<AvailableModel[]> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { data, error } = await (client as any).get({ url: '/api/v1/models' });
  if (error) throw error;
  return data as AvailableModel[];
}

/** Per-role default model aliases (role → alias). Used to gate embedding-dependent UI. */
export async function listModelDefaults(): Promise<Record<string, string>> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { data, error } = await (client as any).get({ url: '/api/v1/models/defaults' });
  if (error) throw error;
  return (data ?? {}) as Record<string, string>;
}

export async function listModelCatalog(): Promise<CatalogModel[]> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { data, error } = await (client as any).get({ url: `${BASE}/catalog` });
  if (error) throw error;
  return data as CatalogModel[];
}

export async function listGatewayModels(): Promise<GatewayModel[]> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { data, error } = await (client as any).get({ url: `${BASE}/models` });
  if (error) throw error;
  return data as GatewayModel[];
}

export async function getCostPrefill(modelName: string): Promise<CostPrefill> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { data, error } = await (client as any).get({
    url: `${BASE}/models/${encodeURIComponent(modelName)}/cost-prefill`,
  });
  if (error) throw error;
  return data as CostPrefill;
}

export async function registerGatewayModel(
  body: ModelRegistrationRequest,
): Promise<ModelRegistrationResponse> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { data, error } = await (client as any).post({ url: `${BASE}/models`, body });
  if (error) throw error;
  return data as ModelRegistrationResponse;
}

export async function updateGatewayModel(
  modelId: string,
  body: ModelRegistrationRequest,
): Promise<ModelRegistrationResponse> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { data, error } = await (client as any).put({
    url: `${BASE}/models/${encodeURIComponent(modelId)}`,
    body,
  });
  if (error) throw error;
  return data as ModelRegistrationResponse;
}

export async function testGatewayModel(modelName: string): Promise<{ status: string }> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { data, error } = await (client as any).post({
    url: `${BASE}/models/${encodeURIComponent(modelName)}/test`,
  });
  if (error) throw error;
  return data as { status: string };
}

export async function setGatewayModelDefault(modelId: string, role: DefaultRole): Promise<void> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { error } = await (client as any).post({
    url: `${BASE}/models/${encodeURIComponent(modelId)}/default`,
    body: { role },
  });
  if (error) throw error;
}

export async function deleteGatewayModel(modelId: string): Promise<void> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { error } = await (client as any).delete({
    url: `${BASE}/models/${encodeURIComponent(modelId)}`,
  });
  if (error) throw error;
}
