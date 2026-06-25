import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Globe } from 'lucide-react';
import { toast } from 'sonner';

import { setGatewayModelDefault, type GatewayModel } from '@/api/model-gateway';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

const byCostAsc = (a: GatewayModel, b: GatewayModel) =>
  (a.input_cost_per_token ?? Infinity) - (b.input_cost_per_token ?? Infinity);

/**
 * Web Search configuration: pick the Search Provider and (for the gateway-native provider) which
 * web-search-capable model backs the agent's `web_search` tool.
 *
 * The functional knob is the `search` model-default: setting it pins the gateway-native search to
 * that model; leaving it unset auto-selects the cheapest web-search-capable model (mirrors
 * model_factory.get_web_search_model). External providers appear disabled until integrated.
 */
export function WebSearchSettings({ models }: { models: GatewayModel[] }) {
  const queryClient = useQueryClient();

  const capable = models.filter((m) => m.supports_web_search).sort(byCostAsc);
  const selected = models.find((m) => (m.default_roles ?? []).includes('search'));
  const autoModel = capable[0];
  const active = selected ?? autoModel;
  const available = capable.length > 0;

  const mutation = useMutation({
    mutationFn: (modelId: string) => setGatewayModelDefault(modelId, 'search'),
    onSuccess: () => {
      toast.success('Web-search model set (apps pick it up within ~60s)');
      queryClient.invalidateQueries({ queryKey: ['gateway-models'] });
      queryClient.invalidateQueries({ queryKey: ['system-status'] });
    },
    onError: (e: unknown) => toast.error(`Set web-search model failed: ${String(e)}`),
  });

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <Globe className="h-4 w-4" /> Web Search
        </CardTitle>
        <CardDescription>
          The agent&apos;s <span className="font-mono">console_web_search</span> tool runs an isolated
          search and returns a grounded answer with sources, using a web-search-capable gateway model.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-1.5">
          <label className="text-sm font-medium">Search provider</label>
          <Select value="gateway">
            <SelectTrigger className="w-full max-w-md">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="gateway">Model Gateway (built-in)</SelectItem>
            </SelectContent>
          </Select>
        </div>

        <div className="space-y-1.5">
          <label className="text-sm font-medium">Search model</label>
          {available ? (
            <>
              <Select
                value={active?.model_id ?? undefined}
                onValueChange={(modelId) => modelId && mutation.mutate(modelId)}
                disabled={mutation.isPending}
              >
                <SelectTrigger className="w-full max-w-md">
                  <SelectValue placeholder="Select a search model" />
                </SelectTrigger>
                <SelectContent>
                  {capable.map((m) => (
                    <SelectItem key={m.model_id ?? m.model_name} value={m.model_id ?? ''}>
                      <span>{m.model_name}</span>
                      {m.model_id === autoModel?.model_id && (
                        <Badge variant="outline" className="ml-1">
                          cheapest
                        </Badge>
                      )}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">
                Active: <span className="font-medium">{active?.model_name}</span>{' '}
                {selected ? '(selected)' : '(auto-selected — cheapest capable)'}.
              </p>
            </>
          ) : (
            <p className="text-sm text-muted-foreground">
              No web-search-capable model is registered, so web search is off. Register a
              web-search-capable model (e.g. a Gemini model) to enable it.
            </p>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
