import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { AlertCircle, CheckCircle2, ExternalLink, Lightbulb, Loader2, MessageSquare } from 'lucide-react';
import { toast } from 'sonner';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { parseAzps } from '@/components/subagents/embedBinding';
import {
  createEmbedBoundSubAgentApiV1SubAgentsEmbedBindingsPostMutation,
  probeEmbedAuthorityApiV1SubAgentsEmbedBindingsProbePostMutation,
} from '@/api/generated/@tanstack/react-query.gen';
import type { EmbedBindingProbe } from '@/api/generated/types.gen';
import { getErrorMessage } from '@/lib/utils';
import { Link } from 'react-router';

interface EmbeddedAgentFormProps {
  onCancel: () => void;
  /** The sub-agent was created; the page navigates to it. */
  onCreated: (subAgentId: number) => void;
}

/**
 * Create a sub-agent whose definition an application publishes (ADR-0006).
 *
 * The admin gives the authority origin and the OAuth client ids. Everything else — name,
 * description, system prompt and skills — comes from `/.well-known/agent-skills/` on that
 * origin and stays in sync, which is why this form has no fields for them.
 */
export function EmbeddedAgentForm({ onCancel, onCreated }: EmbeddedAgentFormProps) {
  const [baseUrl, setBaseUrl] = useState('');
  const [azpsText, setAzpsText] = useState('');
  const [probe, setProbe] = useState<EmbedBindingProbe | null>(null);

  const azps = parseAzps(azpsText);
  const trimmedUrl = baseUrl.trim();

  const probeMutation = useMutation({
    ...probeEmbedAuthorityApiV1SubAgentsEmbedBindingsProbePostMutation(),
    onSuccess: (result) => setProbe(result),
    onError: (err) => {
      toast.error('Could not test the authority', { description: getErrorMessage(err) });
      setProbe(null);
    },
  });

  const createMutation = useMutation({
    ...createEmbedBoundSubAgentApiV1SubAgentsEmbedBindingsPostMutation(),
    onSuccess: (binding) => {
      toast.success('Embedded agent created');
      onCreated(binding.sub_agent_id);
    },
    onError: (err) => {
      toast.error('Failed to create embedded agent', { description: getErrorMessage(err) });
    },
  });

  const busy = probeMutation.isPending || createMutation.isPending;
  const canTest = trimmedUrl.length > 0 && !busy;
  const canCreate = trimmedUrl.length > 0 && azps.length > 0 && !busy;

  // A new URL invalidates the last test result, so the panel never describes another origin.
  const changeBaseUrl = (value: string) => {
    setBaseUrl(value);
    setProbe(null);
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!canCreate) return;
    createMutation.mutate({ body: { base_url: trimmedUrl, azps } });
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-6">
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Main Form Column */}
        <div className="lg:col-span-2 space-y-6">
          <Card>
            <CardHeader>
              <CardTitle>Application</CardTitle>
              <CardDescription>
                The application publishes this agent under <code>/.well-known/agent-skills/</code>. Nannos reads name,
                description, system prompt and skills from there and keeps them in sync. They cannot be edited here.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="embed-base-url">Base URL *</Label>
                <div className="flex gap-2">
                  <Input
                    id="embed-base-url"
                    value={baseUrl}
                    onChange={(e) => changeBaseUrl(e.target.value)}
                    placeholder="https://my-app.ringier.com"
                    className="font-mono text-sm"
                    autoComplete="off"
                    disabled={busy}
                  />
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => probeMutation.mutate({ body: { base_url: trimmedUrl } })}
                    disabled={!canTest}
                  >
                    {probeMutation.isPending ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
                    Test
                  </Button>
                </div>
                <p className="text-xs text-muted-foreground">
                  Scheme and host only, no path. Test reads the published definition without creating anything.
                </p>
              </div>

              {probe && <ProbeResult probe={probe} />}

              <div className="space-y-2">
                <Label htmlFor="embed-azps">Allowed OAuth client ids (azp claim) *</Label>
                <Textarea
                  id="embed-azps"
                  value={azpsText}
                  onChange={(e) => setAzpsText(e.target.value)}
                  placeholder="nannos-embedded"
                  rows={3}
                  className="font-mono text-sm resize-none"
                  disabled={busy}
                />
                <p className="text-xs text-muted-foreground">
                  When a token is received, we check that its `azp` claim is in this list. One per line, no commas. If
                  matching, this sub-agent will be activated.
                </p>
              </div>
            </CardContent>
          </Card>
        </div>

        {/* Help Sidebar */}
        <div className="lg:col-span-1">
          <div className="sticky top-6 space-y-4">
            <Card className="bg-muted/50">
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-base">
                  <Lightbulb className="h-4 w-4" />
                  What is Nannos Assistant?
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                <div className="text-sm">
                  <p className="text-muted-foreground text-sm">
                    Nannos Assistant, or "embedded" Nannos, is a sub-agent whose prompt and skills is not defined in
                    this console application, but instead in another application. This means an integrating partner does
                    not have to use the Console every time they update their application capabilities.
                  </p>
                </div>
                <div className="text-sm">
                  <p className="font-medium mb-1">Examples and Documentation</p>
                  <p className="text-muted-foreground text-sm">
                    See for example{' '}
                    <Link
                      to="https://riad.alloy.ch/.well-known/agent-skills/index.json"
                      target="_blank"
                      className="text-muted-foreground underline"
                    >
                      https://riad.alloy.ch/.well-known/agent-skills/index.json
                    </Link>{' '}
                    for a published agent definition.
                  </p>
                </div>
              </CardContent>
            </Card>
          </div>
        </div>
      </div>

      <div className="flex justify-end gap-2">
        <Button type="button" variant="outline" onClick={onCancel} disabled={busy}>
          Cancel
        </Button>
        <Button type="submit" disabled={!canCreate}>
          {createMutation.isPending ? (
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
          ) : (
            <MessageSquare className="mr-2 h-4 w-4" />
          )}
          Create Embedded Agent
        </Button>
      </div>
    </form>
  );
}

/** What the authority publishes right now, or why it could not be read. */
function ProbeResult({ probe }: { probe: EmbedBindingProbe }) {
  if (!probe.ok || !probe.agent) {
    return (
      <Alert variant="destructive">
        <AlertCircle className="h-4 w-4" />
        <AlertDescription className="text-xs break-words">
          <span className="font-medium">Could not read {probe.base_url || 'the authority'}.</span> {probe.error}
        </AlertDescription>
      </Alert>
    );
  }

  const agent = probe.agent;
  const skills = probe.skills ?? [];
  const tools = agent.tools;

  return (
    <div className="rounded-lg border border-border bg-muted/30 p-4 space-y-3">
      <div className="flex items-center gap-2 text-sm font-medium">
        <CheckCircle2 className="h-4 w-4 text-green-600 shrink-0" />
        <span>The application publishes this agent.</span>
        <a
          href={probe.index_url}
          target="_blank"
          rel="noreferrer"
          className="ml-auto inline-flex items-center gap-1 text-xs font-normal text-primary hover:underline"
        >
          index.json
          <ExternalLink className="h-3 w-3 opacity-60" />
        </a>
      </div>

      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1.5 text-xs">
        <dt className="text-muted-foreground">Name</dt>
        <dd className="min-w-0">
          {agent.name}
          {agent.organization && <span className="text-muted-foreground"> · {agent.organization}</span>}
        </dd>

        <dt className="text-muted-foreground">Description</dt>
        <dd className="min-w-0 break-words">{agent.description}</dd>

        <dt className="text-muted-foreground">Skills</dt>
        <dd className="flex flex-wrap gap-1">
          {skills.length === 0 ? (
            <span className="text-muted-foreground">none published</span>
          ) : (
            skills.map((skill) => (
              <Badge key={skill.name} variant="secondary" className="font-mono text-[10px] font-normal">
                {skill.name}
              </Badge>
            ))
          )}
        </dd>

        <dt className="text-muted-foreground">Settings</dt>
        <dd className="text-muted-foreground">
          {tools ? `${tools.length} tool${tools.length === 1 ? '' : 's'}` : 'all tools unless set here'}
          {' · '}
          {agent.model_tier ? `${agent.model_tier} tier` : 'model set here'}
          {' · '}
          {agent.thinking_level ? `thinking ${agent.thinking_level}` : 'thinking set here'}
        </dd>
      </dl>
    </div>
  );
}
