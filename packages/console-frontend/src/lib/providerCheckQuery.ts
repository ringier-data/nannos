/**
 * The billing-configuration query key, shared by every surface that can FIX what it reports.
 *
 * The check is uncached server-side, so a refetch after a fix always shows the fixed state. A fix can
 * arrive from three places — re-keying from the banner, registering/editing a model on the Model
 * Gateway page (the server then writes a correctly-keyed card), or adding a rate card on the Rate
 * Cards page — and each must invalidate this key. Otherwise the red banner keeps rendering the
 * pre-fix state while mounted, and its Re-key buttons 409 ("already exists") right after a fix.
 *
 * Derived from the generated query options so it can't drift from the SDK's own key.
 */
import { providerConfigCheckApiV1AdminRateCardsProviderConfigGetQueryKey } from '@/api/generated/@tanstack/react-query.gen';

export const PROVIDER_CONFIG_QUERY_KEY = providerConfigCheckApiV1AdminRateCardsProviderConfigGetQueryKey();
