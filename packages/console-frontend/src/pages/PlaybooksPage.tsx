import { useState, useCallback } from 'react';
import { useSearchParams } from 'react-router';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Save,
  Loader2,
  Users,
  User as UserIcon,
  Bot,
} from 'lucide-react';
import { toast } from 'sonner';
import {
  getPlaybookApiV1PlaybooksAgentsAgentNameGetOptions,
  getPlaybookApiV1PlaybooksAgentsAgentNameGetQueryKey,
  updatePlaybookApiV1PlaybooksAgentsAgentNameScopePutMutation,
  consoleListSubAgentsOptions,
  listMyGroupsApiV1GroupsGetOptions,
} from '@/api/generated/@tanstack/react-query.gen';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import { Separator } from '@/components/ui/separator';
import { Textarea } from '@/components/ui/textarea';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';

/** "personal" or a group ID string */
type ScopeSelection = string;
const PERSONAL_SCOPE = 'personal';

export function PlaybooksPage() {
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();

  const selectedAgent = searchParams.get('agent') || 'orchestrator';
  const selectedScope: ScopeSelection = searchParams.get('scope') || PERSONAL_SCOPE;
  const [editedContent, setEditedContent] = useState<string | null>(null);

  const updateParams = useCallback(
    (updates: Record<string, string>) => {
      setSearchParams((prev) => {
        const next = new URLSearchParams(prev);
        for (const [k, v] of Object.entries(updates)) {
          if (
            (k === 'agent' && v === 'orchestrator') ||
            (k === 'scope' && v === PERSONAL_SCOPE)
          ) {
            next.delete(k);
          } else {
            next.set(k, v);
          }
        }
        return next;
      }, { replace: true });
    },
    [setSearchParams],
  );

  const isPersonalScope = selectedScope === PERSONAL_SCOPE;
  const apiScope = isPersonalScope ? 'personal' : 'group';
  const groupIdParam = isPersonalScope ? undefined : selectedScope;

  const handleAgentChange = (agent: string) => {
    updateParams({ agent });
    setEditedContent(null);
  };

  const handleScopeChange = (scope: ScopeSelection) => {
    updateParams({ scope });
    setEditedContent(null);
  };

  // Fetch sub-agents for the agent selector
  const { data: subAgentsData } = useQuery(consoleListSubAgentsOptions());

  // Fetch user's groups
  const { data: myGroupsData } = useQuery(listMyGroupsApiV1GroupsGetOptions());
  const groups = Array.isArray(myGroupsData) ? myGroupsData : [];
  const selectedGroupName = groups.find((g) => String(g.id) === selectedScope)?.name;

  const agentNames = [
    'orchestrator',
    ...(subAgentsData?.items?.map((a) => a.name).filter(Boolean) as string[] ?? []),
  ];

  // Fetch playbook (AGENTS.md) for the selected agent
  const { data: playbookData, isLoading: playbookLoading } = useQuery({
    ...getPlaybookApiV1PlaybooksAgentsAgentNameGetOptions({
      path: { agent_name: selectedAgent },
    }),
  });

  const serverContent = isPersonalScope
    ? playbookData?.personal?.content ?? ''
    : playbookData?.groups?.find((g) => g.group_id === selectedScope)?.content ?? '';
  const displayContent = editedContent ?? serverContent;
  const hasChanges = editedContent !== null;

  const invalidatePlaybook = () =>
    queryClient.invalidateQueries({
      queryKey: getPlaybookApiV1PlaybooksAgentsAgentNameGetQueryKey({
        path: { agent_name: selectedAgent },
      }),
    });

  const updatePlaybookMutation = useMutation({
    ...updatePlaybookApiV1PlaybooksAgentsAgentNameScopePutMutation(),
    onSuccess: () => {
      toast.success('Playbook saved');
      invalidatePlaybook();
      setEditedContent(null);
    },
    onError: () => toast.error('Failed to save playbook'),
  });

  const handleSavePlaybook = () => {
    updatePlaybookMutation.mutate({
      path: { agent_name: selectedAgent, scope: apiScope },
      query: { group_id: groupIdParam },
      body: { content: displayContent },
    });
  };

  const scopeLabel = isPersonalScope ? 'Personal' : selectedGroupName ?? 'Group';

  return (
    <div className="flex flex-col gap-6 p-4 max-w-5xl">
      {/* Page Header */}
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Playbooks</h1>
        <p className="text-sm text-muted-foreground">
          Personalize agent behavior with AGENTS.md playbooks. This allows you to set specific instructions and preferences for each agent, either just for yourself or for your entire team, without needing to modify the agent's core configuration.
        </p>
      </div>

      {/* Context Bar */}
      <Card className="bg-muted/30">
        <CardContent className="flex flex-wrap items-center gap-x-8 gap-y-3 py-3 px-4">
          <div className="flex items-center gap-2">
            <Bot className="h-4 w-4 text-muted-foreground" />
            <Label className="text-sm font-medium text-muted-foreground">Agent</Label>
            <Select value={selectedAgent} onValueChange={handleAgentChange}>
              <SelectTrigger className="w-[220px] bg-background">
                <SelectValue placeholder="Select agent" />
              </SelectTrigger>
              <SelectContent>
                {agentNames.map((name) => (
                  <SelectItem key={name} value={name}>
                    {name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <Separator orientation="vertical" className="h-6 hidden sm:block" />
          <div className="flex items-center gap-2">
            {isPersonalScope ? (
              <UserIcon className="h-4 w-4 text-muted-foreground" />
            ) : (
              <Users className="h-4 w-4 text-muted-foreground" />
            )}
            <Label className="text-sm font-medium text-muted-foreground">Scope</Label>
            <Select value={selectedScope} onValueChange={handleScopeChange}>
              <SelectTrigger className="w-[220px] bg-background">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={PERSONAL_SCOPE}>Personal</SelectItem>
                {groups.map((g) => (
                  <SelectItem key={g.id} value={String(g.id)}>
                    {g.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </CardContent>
      </Card>

      {/* Playbook Editor */}
      {playbookLoading ? (
        <Skeleton className="h-64 w-full" />
      ) : (
        <Card>
          <CardHeader className="flex flex-row items-center gap-2 pb-2">
            {isPersonalScope ? (
              <UserIcon className="h-4 w-4 text-muted-foreground" />
            ) : (
              <Users className="h-4 w-4 text-muted-foreground" />
            )}
            <div className="flex-1">
              <span className="font-medium">{scopeLabel} Playbook</span>
              <span className="text-xs text-muted-foreground ml-2">
                for <code className="bg-muted px-1 rounded text-xs">{selectedAgent}</code>
              </span>
            </div>
            <Badge variant={isPersonalScope ? 'secondary' : 'outline'} className="text-xs">
              {isPersonalScope ? 'Only you' : 'Shared'}
            </Badge>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <p className="text-xs text-muted-foreground">
              {isPersonalScope
                ? 'Your personal instructions. These override group playbooks when they conflict.'
                : `Applies to all members of ${selectedGroupName ?? 'this group'}. Requires write role.`}
            </p>
            <Textarea
              value={displayContent}
              onChange={(e) => setEditedContent(e.target.value)}
              placeholder={
                isPersonalScope
                  ? '# AGENTS.md\n\n## Preferences\n\n- Always respond in bullet points\n- Use formal tone'
                  : '# AGENTS.md\n\n## Team Standards\n\n- Follow company coding guidelines\n- Always include links to sources'
              }
              className="min-h-[250px] font-mono text-sm"
            />
            <div className="flex items-center justify-end gap-2">
              {hasChanges && (
                <Button variant="ghost" size="sm" onClick={() => setEditedContent(null)}>
                  Discard
                </Button>
              )}
              <Button
                onClick={handleSavePlaybook}
                disabled={!hasChanges || updatePlaybookMutation.isPending}
                size="sm"
              >
                {updatePlaybookMutation.isPending ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <Save className="mr-2 h-4 w-4" />
                )}
                Save
              </Button>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
