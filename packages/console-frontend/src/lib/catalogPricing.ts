import type { CatalogModel } from '@/api/generated';

// Cost-map values are USD per token (or per query / image); the form edits USD per million.
// Rounded to 10 significant digits so float noise (2e-7 × 1e6 = 0.19999999999999998) never
// reaches the field, and so never reaches the saved rate card. Also what the pickers display, so
// a regional tier's uplift on a cheap model ($0.022 vs $0.02) stays visible.
export const pricePerMillion = (v?: number | null): string | undefined =>
  v && v > 0 ? String(Number((v * 1_000_000).toPrecision(10))) : undefined;
const perMillion = pricePerMillion;

/** The form's price fields (billing unit → USD per million) seeded from one catalog entry. */
export function pricesFromCatalogEntry(entry: CatalogModel): Record<string, string> {
  // Web search is a per-query fee keyed by context size; price the `medium` tier (what
  // gateway_web_search sends), falling back to low/high — mirrors the backend cost-prefill.
  const search = entry.search_context_cost_per_query;
  const perQuery =
    search?.search_context_size_medium ?? search?.search_context_size_low ?? search?.search_context_size_high;
  const map: Array<[string, string | undefined]> = [
    ['base_input_tokens', perMillion(entry.input_cost_per_token)],
    ['base_output_tokens', perMillion(entry.output_cost_per_token)],
    ['cache_read_input_tokens', perMillion(entry.cache_read_input_token_cost)],
    ['cache_creation_input_tokens', perMillion(entry.cache_creation_input_token_cost)],
    ['input_images', perMillion(entry.input_cost_per_image)],
    ['web_search', perMillion(perQuery)],
  ];
  const prices: Record<string, string> = {};
  for (const [unit, val] of map) if (val) prices[unit] = val;
  return prices;
}

/** Whether a catalog entry lists a per-query web-search fee (some carry it without the boolean). */
export const hasWebSearchFee = (entry: CatalogModel): boolean => {
  const s = entry.search_context_cost_per_query;
  return !!(s?.search_context_size_medium ?? s?.search_context_size_low ?? s?.search_context_size_high);
};

/** The provider route of a gateway model id: the part before the first "/" ("" when unprefixed). */
export const routeOf = (modelId: string): string =>
  modelId.includes('/') ? modelId.slice(0, modelId.indexOf('/')) : '';

// The underlying model a catalog id names, without its route, region or date: the last path
// segment, minus a trailing release date (YYYY-MM-DD, or the older 4-digit MMDD form Azure used,
// as in gpt-35-turbo-1106). "azure/eu/gpt-6-sol", "azure/us/gpt-6-sol" and
// "azure/gpt-6-sol-2026-09-22" all name "gpt-6-sol"; "azure/gpt-6-sol-pro" does not, and neither
// does gpt-4-1106-preview (the suffix is only stripped at the end of the name).
const underlyingModel = (modelId: string): string =>
  modelId
    .slice(modelId.lastIndexOf('/') + 1)
    .replace(/-(\d{4}-\d{2}-\d{2}|\d{4})$/, '')
    .toLowerCase();

/**
 * Catalog entries that can serve as the base model for a gateway model id: same route and mode,
 * and, when the deployment name is itself a known model, only that model's variants (the region
 * and date entries that differ in price, e.g. an EU Data Zone tier). A deployment name that
 * matches no model (e.g. a deployment called "chatgpt-4o") gets every entry of its route, since
 * only the admin knows what it serves.
 */
export function compatibleBaseModels(catalog: CatalogModel[], gatewayModelId: string, mode: string): CatalogModel[] {
  const route = routeOf(gatewayModelId);
  if (!route) return [];
  const sameRoute = catalog.filter(
    (c) => c.mode === mode && (routeOf(c.model_id) === route || (!routeOf(c.model_id) && c.family === route)),
  );
  const name = underlyingModel(gatewayModelId);
  const variants = sameRoute.filter((c) => underlyingModel(c.model_id) === name);
  return variants.length > 0 ? variants : sameRoute;
}
