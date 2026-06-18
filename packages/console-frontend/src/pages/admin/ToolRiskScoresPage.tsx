import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { ShieldAlert, Trash2, Loader2, RefreshCw, Plus, Pencil } from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { TableSkeleton } from '@/components/skeletons';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Checkbox } from '@/components/ui/checkbox';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { client } from '@/api/generated/client.gen';

interface RiskFactor {
  risky_values: Record<string, number>;
  default_contribution: number;
}

interface ToolRiskScore {
  tool_name: string;
  server_slug: string;
  schema_hash: string;
  base_score: number;
  risk_factors: Record<string, RiskFactor>;
  allowed_actions: string[];
  updated_at: string;
}

interface UpsertPayload {
  tool_name: string;
  server_slug: string;
  schema_hash: string;
  base_score: number;
  risk_factors: Record<string, RiskFactor>;
  allowed_actions: string[];
}

/** Fetch paginated risk scores. */
async function fetchRiskScores(limit = 100, offset = 0): Promise<{ items: ToolRiskScore[]; total: number }> {
  const res = await client.get({
    url: '/api/mcp/tools/risk-scores',
    query: { limit, offset, sort: 'updated_at:desc' },
  });
  return res.data as { items: ToolRiskScore[]; total: number };
}

/** Upsert a risk score. */
async function upsertRiskScore(payload: UpsertPayload): Promise<void> {
  await client.put({ url: '/api/mcp/tools/risk-scores', body: payload });
}

/** Delete (invalidate) a risk score. */
async function deleteRiskScore(toolName: string, serverSlug: string): Promise<void> {
  await client.delete({
    url: `/api/mcp/tools/risk-scores/${encodeURIComponent(toolName)}/${encodeURIComponent(serverSlug)}`,
  });
}

function getRiskBadge(score: number) {
  if (score >= 0.9) return <Badge variant="destructive">Critical</Badge>;
  if (score >= 0.8) return <Badge className="bg-orange-500 hover:bg-orange-600">High</Badge>;
  if (score >= 0.6) return <Badge className="bg-amber-500 hover:bg-amber-600">Medium</Badge>;
  return <Badge variant="secondary">Low</Badge>;
}

const ALL_ACTIONS = ['approve', 'edit', 'reject'] as const;

/** Form state for add/edit dialog. */
interface FormState {
  tool_name: string;
  server_slug: string;
  schema_hash: string;
  base_score: number;
  risk_factors_json: string;
  allowed_actions: string[];
}

function emptyForm(): FormState {
  return {
    tool_name: '',
    server_slug: 'console',
    schema_hash: '',
    base_score: 0.8,
    risk_factors_json: '{}',
    allowed_actions: ['approve', 'edit', 'reject'],
  };
}

function scoreToForm(score: ToolRiskScore): FormState {
  return {
    tool_name: score.tool_name,
    server_slug: score.server_slug,
    schema_hash: score.schema_hash,
    base_score: score.base_score,
    risk_factors_json: JSON.stringify(score.risk_factors, null, 2),
    allowed_actions: [...score.allowed_actions],
  };
}

export function ToolRiskScoresPage() {
  const queryClient = useQueryClient();
  const [selectedScore, setSelectedScore] = useState<ToolRiskScore | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<ToolRiskScore | null>(null);
  const [editDialogOpen, setEditDialogOpen] = useState(false);
  const [editingExisting, setEditingExisting] = useState(false);
  const [form, setForm] = useState<FormState>(emptyForm());
  const [jsonError, setJsonError] = useState<string | null>(null);

  const { data, isLoading, isRefetching } = useQuery({
    queryKey: ['adminToolRiskScores'],
    queryFn: () => fetchRiskScores(100, 0),
  });

  const deleteMutation = useMutation({
    mutationFn: ({ toolName, serverSlug }: { toolName: string; serverSlug: string }) =>
      deleteRiskScore(toolName, serverSlug),
    onSuccess: () => {
      toast.success('Risk score invalidated — tool will be re-scored on next use');
      queryClient.invalidateQueries({ queryKey: ['adminToolRiskScores'] });
      setConfirmDelete(null);
    },
    onError: () => {
      toast.error('Failed to invalidate risk score');
    },
  });

  const upsertMutation = useMutation({
    mutationFn: upsertRiskScore,
    onSuccess: () => {
      toast.success(editingExisting ? 'Risk score updated' : 'Risk score created');
      queryClient.invalidateQueries({ queryKey: ['adminToolRiskScores'] });
      setEditDialogOpen(false);
    },
    onError: () => {
      toast.error('Failed to save risk score');
    },
  });

  const openAddDialog = () => {
    setForm(emptyForm());
    setEditingExisting(false);
    setJsonError(null);
    setEditDialogOpen(true);
  };

  const openEditDialog = (score: ToolRiskScore) => {
    setForm(scoreToForm(score));
    setEditingExisting(true);
    setJsonError(null);
    setSelectedScore(null);
    setEditDialogOpen(true);
  };

  const handleSave = () => {
    // Validate JSON
    let riskFactors: Record<string, RiskFactor>;
    try {
      riskFactors = JSON.parse(form.risk_factors_json);
      setJsonError(null);
    } catch {
      setJsonError('Invalid JSON for risk factors');
      return;
    }

    if (!form.tool_name.trim()) {
      toast.error('Tool name is required');
      return;
    }

    upsertMutation.mutate({
      tool_name: form.tool_name.trim(),
      server_slug: form.server_slug.trim() || 'console',
      schema_hash: form.schema_hash,
      base_score: form.base_score,
      risk_factors: riskFactors,
      allowed_actions: form.allowed_actions,
    });
  };

  const toggleAction = (action: string) => {
    setForm((prev) => ({
      ...prev,
      allowed_actions: prev.allowed_actions.includes(action)
        ? prev.allowed_actions.filter((a) => a !== action)
        : [...prev.allowed_actions, action],
    }));
  };

  const scores = data?.items ?? [];

  return (
    <div className="flex flex-col gap-6 p-4 pb-8">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
            <ShieldAlert className="h-6 w-6" />
            Tool Risk Scores
          </h1>
          <p className="text-muted-foreground mt-1">
            View and manage risk scores for MCP tools. Invalidating a score forces re-scoring on next use.
          </p>
        </div>
        <div className="flex gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => queryClient.invalidateQueries({ queryKey: ['adminToolRiskScores'] })}
            disabled={isRefetching}
          >
            {isRefetching ? <Loader2 className="h-4 w-4 animate-spin mr-1" /> : <RefreshCw className="h-4 w-4 mr-1" />}
            Refresh
          </Button>
          <Button size="sm" onClick={openAddDialog}>
            <Plus className="h-4 w-4 mr-1" />
            Add Score
          </Button>
        </div>
      </div>

      {/* Table */}
      {isLoading ? (
        <TableSkeleton columns={6} />
      ) : scores.length === 0 ? (
        <Card>
          <CardContent className="py-8 text-center text-muted-foreground">
            No tool risk scores found. Scores are generated automatically when tools are first used.
          </CardContent>
        </Card>
      ) : (
        <div className="rounded-md border">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b bg-muted/50">
                <th className="text-left px-4 py-2 font-medium">Tool</th>
                <th className="text-left px-4 py-2 font-medium">Server</th>
                <th className="text-left px-4 py-2 font-medium">Risk</th>
                <th className="text-left px-4 py-2 font-medium">Factors</th>
                <th className="text-left px-4 py-2 font-medium">Actions</th>
                <th className="text-left px-4 py-2 font-medium">Last Scored</th>
                <th className="text-right px-4 py-2 font-medium"></th>
              </tr>
            </thead>
            <tbody>
              {scores.map((score) => (
                <tr
                  key={`${score.tool_name}::${score.server_slug}`}
                  className="border-b hover:bg-muted/30 cursor-pointer"
                  onClick={() => setSelectedScore(score)}
                >
                  <td className="px-4 py-2 font-mono text-xs">{score.tool_name}</td>
                  <td className="px-4 py-2 text-muted-foreground">{score.server_slug}</td>
                  <td className="px-4 py-2">
                    <div className="flex items-center gap-2">
                      {getRiskBadge(score.base_score)}
                      <span className="text-xs text-muted-foreground">{Math.round(score.base_score * 100)}%</span>
                    </div>
                  </td>
                  <td className="px-4 py-2 text-muted-foreground">
                    {Object.keys(score.risk_factors).length || '—'}
                  </td>
                  <td className="px-4 py-2">
                    <div className="flex gap-1">
                      {score.allowed_actions.map((a) => (
                        <Badge key={a} variant="outline" className="text-xs">
                          {a}
                        </Badge>
                      ))}
                    </div>
                  </td>
                  <td className="px-4 py-2 text-xs text-muted-foreground">
                    {new Date(score.updated_at).toLocaleDateString()}
                  </td>
                  <td className="px-4 py-2 text-right">
                    <div className="flex gap-1 justify-end">
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <Button
                            variant="ghost"
                            size="icon"
                            className="h-7 w-7"
                            onClick={(e) => {
                              e.stopPropagation();
                              openEditDialog(score);
                            }}
                          >
                            <Pencil className="h-4 w-4" />
                          </Button>
                        </TooltipTrigger>
                        <TooltipContent>Edit score</TooltipContent>
                      </Tooltip>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <Button
                            variant="ghost"
                            size="icon"
                            className="h-7 w-7 text-destructive hover:text-destructive"
                            onClick={(e) => {
                              e.stopPropagation();
                              setConfirmDelete(score);
                            }}
                          >
                            <Trash2 className="h-4 w-4" />
                          </Button>
                        </TooltipTrigger>
                        <TooltipContent>Invalidate (force re-score)</TooltipContent>
                      </Tooltip>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Detail Dialog */}
      <Dialog open={!!selectedScore} onOpenChange={() => setSelectedScore(null)}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle className="font-mono text-sm">{selectedScore?.tool_name}</DialogTitle>
            <DialogDescription>
              Server: {selectedScore?.server_slug} · Schema hash: {selectedScore?.schema_hash?.slice(0, 12)}...
            </DialogDescription>
          </DialogHeader>
          {selectedScore && (
            <div className="space-y-4">
              <div className="flex items-center gap-3">
                <span className="text-sm font-medium">Base Score:</span>
                {getRiskBadge(selectedScore.base_score)}
                <span className="text-sm">{selectedScore.base_score.toFixed(3)}</span>
              </div>

              <div>
                <h4 className="text-sm font-medium mb-2">Allowed Actions</h4>
                <div className="flex gap-1">
                  {selectedScore.allowed_actions.map((a) => (
                    <Badge key={a} variant="outline">{a}</Badge>
                  ))}
                </div>
              </div>

              {Object.keys(selectedScore.risk_factors).length > 0 && (
                <div>
                  <h4 className="text-sm font-medium mb-2">Risk Factors</h4>
                  <div className="space-y-2">
                    {Object.entries(selectedScore.risk_factors).map(([param, profile]) => (
                      <Card key={param}>
                        <CardHeader className="py-2 px-3">
                          <CardTitle className="text-xs font-mono">{param}</CardTitle>
                        </CardHeader>
                        <CardContent className="py-2 px-3 text-xs space-y-1">
                          <p className="text-muted-foreground">
                            Default contribution: {profile.default_contribution.toFixed(2)}
                          </p>
                          {Object.entries(profile.risky_values).length > 0 && (
                            <div>
                              <span className="text-muted-foreground">Patterns:</span>
                              <ul className="mt-1 space-y-0.5">
                                {Object.entries(profile.risky_values).map(([pattern, risk]) => (
                                  <li key={pattern} className="flex justify-between">
                                    <code className="bg-muted px-1 rounded">{pattern}</code>
                                    <span className="text-destructive font-medium">{Math.round(risk * 100)}%</span>
                                  </li>
                                ))}
                              </ul>
                            </div>
                          )}
                        </CardContent>
                      </Card>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setSelectedScore(null)}>
              Close
            </Button>
            <Button
              variant="secondary"
              onClick={() => {
                if (selectedScore) openEditDialog(selectedScore);
              }}
            >
              <Pencil className="h-4 w-4 mr-1" />
              Edit
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                if (selectedScore) {
                  setConfirmDelete(selectedScore);
                  setSelectedScore(null);
                }
              }}
            >
              Invalidate
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Confirm Delete Dialog */}
      <Dialog open={!!confirmDelete} onOpenChange={() => setConfirmDelete(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Invalidate Risk Score</DialogTitle>
            <DialogDescription>
              This will remove the cached risk score for{' '}
              <code className="font-mono">{confirmDelete?.tool_name}</code>. The tool will be re-scored
              by the LLM on next use. This action cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmDelete(null)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                if (confirmDelete) {
                  deleteMutation.mutate({
                    toolName: confirmDelete.tool_name,
                    serverSlug: confirmDelete.server_slug,
                  });
                }
              }}
              disabled={deleteMutation.isPending}
            >
              {deleteMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin mr-1" /> : null}
              Invalidate
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Add/Edit Dialog */}
      <Dialog open={editDialogOpen} onOpenChange={setEditDialogOpen}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>{editingExisting ? 'Edit Risk Score' : 'Add Risk Score'}</DialogTitle>
            <DialogDescription>
              {editingExisting
                ? 'Modify the risk score configuration for this tool.'
                : 'Manually add a risk score entry. Useful for static guards that should always require approval.'}
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4">
            {/* Tool Name */}
            <div className="space-y-2">
              <Label htmlFor="edit-tool-name">Tool Name</Label>
              <Input
                id="edit-tool-name"
                value={form.tool_name}
                onChange={(e) => setForm((f) => ({ ...f, tool_name: e.target.value }))}
                placeholder="e.g. console_create_skill"
                disabled={editingExisting}
                className="font-mono text-sm"
              />
            </div>

            {/* Server Slug */}
            <div className="space-y-2">
              <Label htmlFor="edit-server-slug">Server Slug</Label>
              <Input
                id="edit-server-slug"
                value={form.server_slug}
                onChange={(e) => setForm((f) => ({ ...f, server_slug: e.target.value }))}
                placeholder="console"
                disabled={editingExisting}
                className="font-mono text-sm"
              />
              <p className="text-xs text-muted-foreground">
                MCP server name (e.g. <code>console</code>, <code>github</code>). Use <code>_self</code> for in-process tools only.
              </p>
            </div>

            {/* Base Score */}
            <div className="space-y-2">
              <Label htmlFor="edit-base-score">Base Score: {form.base_score.toFixed(2)}</Label>
              <div className="flex items-center gap-3">
                <input
                  id="edit-base-score"
                  type="range"
                  value={form.base_score}
                  onChange={(e) => setForm((f) => ({ ...f, base_score: parseFloat(e.target.value) }))}
                  min={0}
                  max={1}
                  step={0.05}
                  className="flex-1 h-2 rounded-lg appearance-none cursor-pointer bg-muted"
                />
                {getRiskBadge(form.base_score)}
              </div>
              <p className="text-xs text-muted-foreground">
                1.0 = always interrupt (static guard), 0.0 = never interrupt.
              </p>
            </div>

            {/* Allowed Actions */}
            <div className="space-y-2">
              <Label>Allowed Actions</Label>
              <div className="flex gap-4">
                {ALL_ACTIONS.map((action) => (
                  <label key={action} className="flex items-center gap-2 text-sm">
                    <Checkbox
                      checked={form.allowed_actions.includes(action)}
                      onCheckedChange={() => toggleAction(action)}
                    />
                    {action}
                  </label>
                ))}
              </div>
            </div>

            {/* Risk Factors JSON */}
            <div className="space-y-2">
              <Label htmlFor="edit-risk-factors">Risk Factors (JSON)</Label>
              <Textarea
                id="edit-risk-factors"
                value={form.risk_factors_json}
                onChange={(e) => {
                  setForm((f) => ({ ...f, risk_factors_json: e.target.value }));
                  setJsonError(null);
                }}
                rows={6}
                className="font-mono text-xs resize-y"
                placeholder='{"param_name": {"risky_values": {"DELETE*": 0.95}, "default_contribution": 0.1}}'
              />
              {jsonError && <p className="text-xs text-destructive">{jsonError}</p>}
              <p className="text-xs text-muted-foreground">
                Map parameter names to risk profiles. Each profile has <code>risky_values</code> (glob → score) and <code>default_contribution</code>.
              </p>
            </div>
          </div>

          <DialogFooter>
            <Button variant="outline" onClick={() => setEditDialogOpen(false)}>
              Cancel
            </Button>
            <Button onClick={handleSave} disabled={upsertMutation.isPending}>
              {upsertMutation.isPending && <Loader2 className="h-4 w-4 animate-spin mr-1" />}
              {editingExisting ? 'Save Changes' : 'Create'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
