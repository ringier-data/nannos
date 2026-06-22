/**
 * Budget Guard admin API.
 *
 * Hand-written wrappers for the console-backend admin endpoints
 * (`/api/v1/admin/budget/*`) which the generated SDK doesn't describe yet.
 * Uses the shared `client` so the X-Admin-Mode interceptor is applied.
 *
 * TODO: replace with generated bindings after `npm run gen-sdk`.
 */
import { client } from './generated/client.gen';

export interface BudgetSettings {
  enabled: boolean;
  monthly_limit_usd: number;
  warning_thresholds: number[];
  updated_at: string;
}

export interface BudgetSettingsUpdate {
  enabled?: boolean;
  monthly_limit_usd?: number;
  warning_thresholds?: number[];
}

export interface BudgetStatus {
  enabled: boolean;
  spend_usd: number;
  limit_usd: number;
  usage_percentage: number;
  is_locked: boolean;
  warnings: number[];
  period_start: string;
  period_end: string;
}

const BASE = '/api/v1/admin/budget';

export async function getBudgetSettings(): Promise<BudgetSettings> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { data, error } = await (client as any).get({ url: `${BASE}/settings` });
  if (error) throw error;
  return data as BudgetSettings;
}

export async function updateBudgetSettings(body: BudgetSettingsUpdate): Promise<BudgetSettings> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { data, error } = await (client as any).put({ url: `${BASE}/settings`, body });
  if (error) throw error;
  return data as BudgetSettings;
}

export async function getBudgetStatus(): Promise<BudgetStatus> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { data, error } = await (client as any).get({ url: `${BASE}/status` });
  if (error) throw error;
  return data as BudgetStatus;
}
