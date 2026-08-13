/**
 * Billing-configuration banner (Rate Cards + Model Gateway pages).
 *
 * Rate cards key on the provider the cost logger reports at runtime; a card keyed to any other value
 * (e.g. LiteLLM's catalog tag `bedrock_converse` instead of the runtime family `bedrock`) never
 * matches usage and the model silently bills $0. This banner shows only what is wrong RIGHT NOW and
 * fixable right now — derived from configuration, so a mis-keyed model is flagged before its first
 * call, and the list reaches zero once every finding is addressed. What already billed $0 is a
 * different question with unfixable rows in it, and is deliberately not reported here.
 *
 * Two severities, rendered as separate cards so the blame lands on the right side:
 * - unbillable deployments (red): the gateway routes a model under a provider no active card prices,
 *   so it WILL bill $0. Actionable: a re-key button per server-vetted candidate (`rekey_candidates`);
 *   cards that are pricing another deployment of the same alias are named but not moved.
 * - orphan cards (amber): pricing keyed outside the runtime vocabulary. It bills nothing today and
 *   belongs to no deployment, so there is nothing to re-key TO — it needs a decision on the card.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, Info } from 'lucide-react';
import { toast } from 'sonner';
import {
  providerConfigCheckApiV1AdminRateCardsProviderConfigGetOptions,
  rekeyRateCardApiV1AdminRateCardsRekeyPostMutation,
} from '@/api/generated/@tanstack/react-query.gen';
import type { OrphanCard, UnbillableDeployment } from '@/api/generated/types.gen';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { PROVIDER_CONFIG_QUERY_KEY } from '@/lib/providerCheckQuery';

export function ProviderMismatchBanner() {
  const queryClient = useQueryClient();

  const { data } = useQuery({
    ...providerConfigCheckApiV1AdminRateCardsProviderConfigGetOptions(),
    staleTime: 60_000,
    retry: 1,
  });

  const rekeyMutation = useMutation({
    ...rekeyRateCardApiV1AdminRateCardsRekeyPostMutation(),
    onSuccess: (result) => {
      toast.success(`Rate card re-keyed to ${result.provider} — billing matches from the next call`);
      queryClient.invalidateQueries({ queryKey: PROVIDER_CONFIG_QUERY_KEY });
      queryClient.invalidateQueries({ queryKey: ['rate-card-entries-all'] });
      queryClient.invalidateQueries({ queryKey: ['gateway-models'] });
    },
    onError: (error) => {
      // 404/409 carry a plain-string detail (e.g. "card already exists at target — merge
      // manually"); validation errors carry a list — fall back to a generic message then.
      const detail = (error as { detail?: unknown })?.detail;
      toast.error(typeof detail === 'string' ? detail : 'Failed to re-key rate card');
    },
  });

  if (!data) return null;
  const deployments = data.unbillable_deployments;
  const orphans = data.orphan_cards;
  if (deployments.length === 0 && orphans.length === 0) return null;

  return (
    <>
      {deployments.length > 0 && (
        <Card className="border-destructive/50 bg-destructive/5">
          <CardContent className="pt-0">
            <div className="flex gap-3">
              <AlertTriangle className="w-5 h-5 text-destructive mt-0.5 flex-shrink-0" />
              <div className="space-y-2 flex-1 min-w-0">
                <p className="text-sm font-medium">
                  {deployments.length} model{deployments.length === 1 ? '' : 's'} will bill $0 — no rate card
                  matches the provider the gateway routes {deployments.length === 1 ? 'it' : 'them'} under
                </p>
                <div className="space-y-1.5">
                  {deployments.map((d) => (
                    <DeploymentRow
                      key={`${d.model_name}::${d.runtime_provider ?? 'underivable'}`}
                      deployment={d}
                      busy={rekeyMutation.isPending}
                      onRekey={(fromProvider) =>
                        rekeyMutation.mutate({
                          body: {
                            model_name: d.model_name,
                            from_provider: fromProvider,
                            // Only ever rendered when runtime_provider is known (see the row).
                            to_provider: d.runtime_provider as string,
                          },
                        })
                      }
                    />
                  ))}
                </div>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {!data.gateway_checked && (
        <Card className="border-amber-400/50 bg-amber-50/50 dark:border-amber-600/40 dark:bg-amber-950/20">
          <CardContent className="pt-0">
            <p className="text-xs text-muted-foreground">
              Gateway unreachable — registered models could not be checked, so this banner only reflects the rate
              cards themselves.
            </p>
          </CardContent>
        </Card>
      )}

      {orphans.length > 0 && (
        <Card className="border-amber-400/50 bg-amber-50/50 dark:border-amber-600/40 dark:bg-amber-950/20">
          <CardContent className="pt-0">
            <div className="flex gap-3">
              <Info className="w-5 h-5 text-amber-600 dark:text-amber-400 mt-0.5 flex-shrink-0" />
              <div className="space-y-2 flex-1 min-w-0">
                <p className="text-sm font-medium">
                  {orphans.length} rate card{orphans.length === 1 ? '' : 's'} keyed on a provider the runtime
                  never reports — that pricing can never match usage
                </p>
                <div className="space-y-1.5">
                  {orphans.map((c) => (
                    <OrphanCardRow key={`${c.provider}::${c.model_name}`} card={c} />
                  ))}
                </div>
                <p className="text-xs text-muted-foreground">
                  These are leftovers from an older vocabulary (LiteLLM catalog tags like{' '}
                  <span className="font-mono">bedrock_converse</span>, Vertex locations like{' '}
                  <span className="font-mono">eu</span>, hand-typed vendor names). They bill nothing on their
                  own — re-key one to the family its model actually routes under, or expire it.
                </p>
              </div>
            </div>
          </CardContent>
        </Card>
      )}
    </>
  );
}

// A deployment that cannot bill. The re-key button moves an existing card (pricing history included)
// to the provider this model routes under.
function DeploymentRow({
  deployment,
  busy,
  onRekey,
}: {
  deployment: UnbillableDeployment;
  busy: boolean;
  onRekey: (fromProvider: string) => void;
}) {
  const existingCards = deployment.other_providers ?? [];
  // Only the server-vetted subset gets a button. Two different reasons a named card isn't offered,
  // and they need different sentences: a PATTERN card is keyed on another model name (a re-key would
  // 404) and prices a whole family, while a card under a provider this alias is also routed as is
  // busy pricing that other deployment. Saying the wrong one sends the admin down the wrong path.
  const candidates = deployment.rekey_candidates ?? [];
  const patternCards = deployment.pattern_providers ?? [];
  // Underivable deployments have no target provider at all, so EVERY card is "not a candidate" —
  // that is not the same as a card being busy elsewhere, and must not borrow that explanation.
  const unroutable = deployment.reason === 'provider_underivable';
  const blocked = unroutable
    ? []
    : existingCards.filter((p) => !candidates.includes(p) && !patternCards.includes(p));
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-md border border-destructive/30 bg-background px-3 py-2 text-sm">
      <span className="font-mono truncate">{deployment.model_name}</span>
      <span className="text-muted-foreground text-xs">
        {unroutable ? (
          <>
            the gateway model id is neither prefixed with a route nor a known catalog model, so nothing can
            route or bill it — re-register it with a prefixed id (e.g.{' '}
            <span className="font-mono">bedrock/…</span>) or an explicit{' '}
            <span className="font-mono">custom_llm_provider</span>
            {existingCards.length > 0 && <> (its {existingCards.join(', ')} pricing stays as it is)</>}
          </>
        ) : (
          <>
            routes as{' '}
            <Badge variant="outline" className="text-[10px]">{deployment.runtime_provider}</Badge>
            {existingCards.length > 0 ? (
              <> — card keyed {existingCards.join(', ')}</>
            ) : (
              <> — no rate card for this model at all</>
            )}
          </>
        )}
        {patternCards.length > 0 && !unroutable && (
          <>
            {' '}
            — the {patternCards.join(', ')} pricing comes from a pattern card on another model name, so it
            can&apos;t be re-keyed (that would move the whole family&apos;s pricing); add a{' '}
            {deployment.runtime_provider} rate card for this model instead.
          </>
        )}
        {blocked.length > 0 && (
          <>
            {' '}
            — the {blocked.join(', ')} card {blocked.length === 1 ? 'is' : 'are'} pricing this model&apos;s
            other deployment, so moving {blocked.length === 1 ? 'it' : 'them'} isn&apos;t offered; add a{' '}
            {deployment.runtime_provider} rate card instead.
          </>
        )}
      </span>
      <span className="flex-1" />
      {deployment.runtime_provider &&
        candidates.map((from) => (
          <Button key={from} size="sm" variant="outline" disabled={busy} onClick={() => onRekey(from)}>
            Re-key {from} → {deployment.runtime_provider}
          </Button>
        ))}
    </div>
  );
}

// Pricing keyed outside the runtime vocabulary. No deployment claims it, so there is no target
// provider to offer — the admin decides which model (if any) it was meant to price.
function OrphanCardRow({ card }: { card: OrphanCard }) {
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-md border border-amber-400/40 bg-background px-3 py-2 text-sm">
      <Badge variant="outline" className="text-[10px]">{card.provider}</Badge>
      <span className="font-mono truncate">{card.model_name_pattern ?? card.model_name}</span>
      {card.model_name_pattern && <span className="text-muted-foreground text-xs">(pattern)</span>}
    </div>
  );
}
