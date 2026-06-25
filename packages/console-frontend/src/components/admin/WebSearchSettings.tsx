import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Globe } from 'lucide-react';
import { toast } from 'sonner';

import { getWebSearchConfig, setGatewayModelDefault } from '@/api/model-gateway';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

/**
 * Web Search configuration: pick the Search Provider and (for the gateway-native provider) which
 * web-search-capable model backs the agent's `console_web_search` tool.
 *
 * Purely presentational: the cheapest-capable / selected-vs-auto decision is owned entirely by the
 * backend (services/web_search.py::resolve_web_search_config, exposed at GET .../web-search), so the
 * picker can never disagree with the tool. The only write is setting the `search` model-default.
 */
export function WebSearchSettings() {
  const queryClient = useQueryClient();

  const { data: config, isLoading } = useQuery({
    queryKey: ['web-search-config'],
    queryFn: getWebSearchConfig,
  });

  const mutation = useMutation({
    mutationFn: (modelId: string) => setGatewayModelDefault(modelId, 'search'),
    onSuccess: () => {
      toast.success('Web-search model set (apps pick it up within ~60s)');
      queryClient.invalidateQueries({ queryKey: ['web-search-config'] });
      queryClient.invalidateQueries({ queryKey: ['gateway-models'] });
      queryClient.invalidateQueries({ queryKey: ['system-status'] });
    },
    onError: (e: unknown) => toast.error(`Set web-search model failed: ${String(e)}`),
  });

  const models = config?.models ?? [];
  const available = config?.available ?? false;

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
          {isLoading ? (
            <p className="text-sm text-muted-foreground">Loading…</p>
          ) : available ? (
            <>
              <Select
                value={config?.active_model_id ?? undefined}
                onValueChange={(modelId) => modelId && mutation.mutate(modelId)}
                disabled={mutation.isPending}
              >
                <SelectTrigger className="w-full max-w-md">
                  <SelectValue placeholder="Select a search model" />
                </SelectTrigger>
                <SelectContent>
                  {models
                    .filter((m) => m.model_id)
                    .map((m) => (
                      <SelectItem key={m.model_id} value={m.model_id ?? ''}>
                        <span>{m.model_name}</span>
                        {m.is_cheapest && (
                          <Badge variant="outline" className="ml-1">
                            cheapest
                          </Badge>
                        )}
                      </SelectItem>
                    ))}
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">
                Active: <span className="font-medium">{config?.active_model_name}</span>{' '}
                {config?.source === 'selected'
                  ? '(selected)'
                  : '(auto-selected — cheapest capable)'}
                .
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
