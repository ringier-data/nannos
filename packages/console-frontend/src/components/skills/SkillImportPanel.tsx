import { useState, useMemo, useEffect } from 'react';
import { useQuery, useMutation } from '@tanstack/react-query';
import {
  Search,
  Download,
  ExternalLink,
  GitBranch,
  Shield,
  ShieldAlert,
  AlertTriangle,
  Loader2,
  ArrowLeft,
  Package,
  ChevronDown,
  ChevronRight,
  CheckSquare,
  Square,
} from 'lucide-react';
import { toast } from 'sonner';
import {
  searchSkillsApiV1SkillsRegistrySearchGetOptions,
  browseRepoApiV1SkillsRegistryBrowseGetOptions,
  importSkillApiV1SkillsRegistryImportPostMutation,
} from '@/api/generated/@tanstack/react-query.gen';
import type { SkillSearchResult, SkillSecurityVerdict, SkillSecurityIndicator } from '@/api/generated/types.gen';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { config } from '@/config';

// --- Security Verdict Badge ---

function SecurityVerdictBadge({ verdict }: { verdict: SkillSecurityVerdict }) {
  const [expanded, setExpanded] = useState(false);

  const config = {
    safe: { icon: Shield, className: 'bg-green-100 text-green-800 border-green-200', label: 'Safe' },
    caution: { icon: AlertTriangle, className: 'bg-yellow-100 text-yellow-800 border-yellow-200', label: 'Caution' },
    unsafe: { icon: ShieldAlert, className: 'bg-red-100 text-red-800 border-red-200', label: 'Unsafe' },
  }[verdict.verdict] ?? { icon: Shield, className: 'bg-gray-100 text-gray-800 border-gray-200', label: verdict.verdict };

  const Icon = config.icon;
  const hasIndicators = verdict.indicators && verdict.indicators.length > 0;

  return (
    <div className="space-y-1">
      <button
        onClick={() => hasIndicators && setExpanded(!expanded)}
        className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-md border text-xs font-medium ${config.className} ${hasIndicators ? 'cursor-pointer hover:opacity-80' : 'cursor-default'}`}
      >
        <Icon className="h-3 w-3" />
        {config.label}
        {hasIndicators && (
          expanded ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />
        )}
      </button>
      {expanded && verdict.indicators && (
        <div className="ml-2 space-y-1.5 text-xs">
          <p className="text-muted-foreground">{verdict.reasoning}</p>
          {verdict.indicators.map((ind: SkillSecurityIndicator, i: number) => (
            <div key={i} className="flex items-start gap-2 pl-2 border-l-2 border-muted">
              <Badge
                variant="outline"
                className={`text-[9px] shrink-0 ${ind.risk_level === 'high' ? 'border-red-300 text-red-700' : 'border-yellow-300 text-yellow-700'}`}
              >
                {ind.risk_level}
              </Badge>
              <div>
                <span className="font-medium">{ind.category}</span>
                <span className="text-muted-foreground ml-1">— {ind.description}</span>
                {ind.evidence && ind.evidence.length > 0 && (
                  <div className="mt-0.5 font-mono text-[10px] text-muted-foreground bg-muted rounded px-1.5 py-0.5">
                    {ind.evidence.slice(0, 3).join(', ')}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// --- Skill Result Card ---

function SkillResultCard({
  result,
  isSelected,
  onClick,
}: {
  result: SkillSearchResult;
  isSelected: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`w-full text-left p-3 rounded-lg border transition-colors ${
        isSelected ? 'border-primary bg-accent' : 'border-border hover:border-primary/50 hover:bg-accent/50'
      }`}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <Package className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
            <span className="font-medium text-sm truncate">{result.name}</span>
          </div>
          <p className="text-xs text-muted-foreground mt-0.5 truncate">{result.source}</p>
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          {result.installs != null && result.installs > 0 && (
            <Badge variant="secondary" className="text-[10px] px-1.5 py-0">
              {result.installs} installs
            </Badge>
          )}
          {result.source_type && (
            <Badge variant="outline" className="text-[10px] px-1.5 py-0">
              {result.source_type}
            </Badge>
          )}
        </div>
      </div>
    </button>
  );
}

// --- Main Panel ---

interface SkillImportPanelProps {
  onClose: () => void;
  onImported: (skillName: string) => void;
}

export function SkillImportPanel({ onClose, onImported }: SkillImportPanelProps) {
  // Search state
  const [searchQuery, setSearchQuery] = useState('');
  const [debouncedQuery, setDebouncedQuery] = useState('');
  const showCommunityTab = config.features.externalSkillSearch;
  const [mode, setMode] = useState<'external' | 'browse'>(showCommunityTab ? 'external' : 'browse');

  // Browse repo state
  const [repoInput, setRepoInput] = useState('');
  const [browseRepo, setBrowseRepo] = useState('');
  const [browseRef, setBrowseRef] = useState('');
  const [browseOffset, setBrowseOffset] = useState(0);
  const [accumulatedBrowse, setAccumulatedBrowse] = useState<SkillSearchResult[]>([]);
  const [browseHasMore, setBrowseHasMore] = useState(false);
  const [browseTotal, setBrowseTotal] = useState(0);

  // Selection + import state
  const [selectedResult, setSelectedResult] = useState<SkillSearchResult | null>(null);
  const [selectedResults, setSelectedResults] = useState<Set<string>>(new Set());
  const [importVisibility, setImportVisibility] = useState<'public' | 'private'>('public');
  const [overwrite, setOverwrite] = useState(false);
  const [showUnsafeDialog, setShowUnsafeDialog] = useState(false);
  const [unsafeMessage, setUnsafeMessage] = useState('');
  const [bulkImporting, setBulkImporting] = useState(false);

  // Debounced search
  const debounceTimer = useMemo(() => {
    let timer: ReturnType<typeof setTimeout>;
    return (value: string) => {
      clearTimeout(timer);
      timer = setTimeout(() => setDebouncedQuery(value), 300);
    };
  }, []);

  const handleSearchChange = (value: string) => {
    setSearchQuery(value);
    debounceTimer(value);
  };

  // Search query
  const searchEnabled = mode !== 'browse' && debouncedQuery.length >= 2;
  const { data: searchData, isLoading: searchLoading } = useQuery({
    ...searchSkillsApiV1SkillsRegistrySearchGetOptions({
      query: {
        q: debouncedQuery,
        source: 'external',
        limit: 20,
      },
    }),
    enabled: searchEnabled,
  });

  // Browse query
  const BROWSE_PAGE_SIZE = 50;
  const browseEnabled = mode === 'browse' && browseRepo.length > 0;
  const { data: browseData, isLoading: browseLoading } = useQuery({
    ...browseRepoApiV1SkillsRegistryBrowseGetOptions({
      query: {
        repo: browseRepo,
        ...(browseRef ? { ref: browseRef } : {}),
        limit: BROWSE_PAGE_SIZE,
        offset: browseOffset,
      } as any,
    }),
    enabled: browseEnabled,
  });

  // Accumulate browse results as pages load
  useEffect(() => {
    if (mode !== 'browse' || !browseData) return;
    const newData = browseData.data ?? [];
    setAccumulatedBrowse((prev) => (browseOffset === 0 ? newData : [...prev, ...newData]));
    setBrowseHasMore((browseData as any).has_more ?? false);
    setBrowseTotal((browseData as any).total ?? newData.length);
  }, [browseData, browseOffset, mode]);

  const results = mode === 'browse' ? accumulatedBrowse : searchData?.data;
  const isLoading = mode === 'browse' ? browseLoading && browseOffset === 0 : searchLoading;
  const loadingMore = mode === 'browse' && browseLoading && browseOffset > 0;

  // Import mutation
  const importMutation = useMutation({
    ...importSkillApiV1SkillsRegistryImportPostMutation(),
    onSuccess: (data) => {
      toast.success(`Added "${data.skill_name}" to registry`);
      onImported(data.skill_name);
    },
    onError: (error: any) => {
      const detail = error?.detail ?? error?.body?.detail ?? error?.message ?? 'Import failed';
      // Security assessment failure: detail is an object with verdict/indicators
      if (typeof detail === 'object' && detail?.verdict === 'unsafe') {
        const msg = detail.message || 'Skill failed security assessment';
        const reasoning = detail.reasoning ? `\n\n${detail.reasoning}` : '';
        setUnsafeMessage(`${msg}${reasoning}`);
        setShowUnsafeDialog(true);
      } else if (typeof detail === 'object' && detail?.message?.includes?.('already exists')) {
        toast.error('Skill already exists. Enable "Overwrite" and try again.');
      } else {
        toast.error(typeof detail === 'string' ? detail : detail?.message || 'Import failed');
      }
    },
  });

  const buildImportBody = (result: SkillSearchResult, force = false) => {
    const isRegistryResult = result.source_type === 'well-known';
    return isRegistryResult
      ? {
          registry_id: result.id,
          visibility: importVisibility,
          overwrite,
          force,
        }
      : {
          repo: result.source,
          skill: result.slug,
          visibility: importVisibility,
          overwrite,
          force,
        };
  };

  const handleImport = (force = false) => {
    if (!selectedResult) return;
    importMutation.mutate({ body: buildImportBody(selectedResult, force) });
  };

  const handleBulkImport = async () => {
    if (!results || selectedResults.size === 0) return;
    const toImport = results.filter((r) => selectedResults.has(r.id));
    setBulkImporting(true);
    let successCount = 0;
    let lastName: string | null = null;
    for (const result of toImport) {
      try {
        const data = await importMutation.mutateAsync({ body: buildImportBody(result, false) });
        successCount++;
        lastName = data.skill_name;
      } catch {
        // Individual failures are handled by the mutation's onError
      }
    }
    setBulkImporting(false);
    setSelectedResults(new Set());
    if (successCount > 0) {
      toast.success(`Imported ${successCount} of ${toImport.length} skills to registry`);
      if (lastName) onImported(lastName);
    }
  };

  const toggleBulkSelect = (id: string) => {
    setSelectedResults((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleSelectAll = () => {
    if (!results) return;
    if (selectedResults.size === results.length) {
      setSelectedResults(new Set());
    } else {
      setSelectedResults(new Set(results.map((r) => r.id)));
    }
  };

  const handleBrowse = () => {
    if (!repoInput.includes('/')) {
      toast.error('Enter a valid owner/repo (e.g. "vercel-labs/agent-skills")');
      return;
    }
    setBrowseOffset(0);
    setAccumulatedBrowse([]);
    setSelectedResult(null);
    setSelectedResults(new Set());
    setBrowseRepo(repoInput);
  };

  return (
    <div className="flex-1 min-w-0 flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-2 border-b bg-muted/30">
        <div className="flex items-center gap-2">
          <Button variant="ghost" size="sm" className="h-7 w-7 p-0" onClick={onClose}>
            <ArrowLeft className="h-4 w-4" />
          </Button>
          <Download className="h-4 w-4 text-muted-foreground" />
          <span className="font-medium text-sm">Import to Registry</span>
        </div>
      </div>

      <div className="flex-1 min-h-0 flex flex-col p-4 gap-4 overflow-y-auto">
        {/* Source mode toggle */}
        {showCommunityTab && (
        <div className="flex items-center gap-2">
          <div className="inline-flex rounded-md border">
            {(['external', 'browse'] as const).map((m) => (
              <button
                key={m}
                onClick={() => { setMode(m); setSelectedResult(null); }}
                className={`px-3 py-1.5 text-xs font-medium transition-colors first:rounded-l-md last:rounded-r-md ${
                  mode === m ? 'bg-primary text-primary-foreground' : 'hover:bg-accent'
                }`}
              >
                {m === 'external' ? 'Community' : 'Browse Repo'}
              </button>
            ))}
          </div>
        </div>
        )}

        {/* Search / Browse input */}
        {mode !== 'browse' ? (
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
            <Input
              value={searchQuery}
              onChange={(e) => handleSearchChange(e.target.value)}
              placeholder="Search community skills (skills.sh)..."
              className="pl-9"
              autoFocus
            />
          </div>
        ) : (
          <div className="flex gap-2">
            <Input
              value={repoInput}
              onChange={(e) => setRepoInput(e.target.value)}
              placeholder="owner/repo (e.g. vercel-labs/agent-skills)"
              className="flex-1 font-mono text-sm"
              onKeyDown={(e) => e.key === 'Enter' && handleBrowse()}
              autoFocus
            />
            <Input
              value={browseRef}
              onChange={(e) => setBrowseRef(e.target.value)}
              placeholder="ref (main)"
              className="w-28 font-mono text-sm"
              onKeyDown={(e) => e.key === 'Enter' && handleBrowse()}
            />
            <Button size="sm" onClick={handleBrowse} disabled={!repoInput.includes('/')}>
              <GitBranch className="h-3.5 w-3.5 mr-1.5" />
              Browse
            </Button>
          </div>
        )}

        {/* Results + Detail split */}
        <div className="flex-1 min-h-0 flex gap-4">
          {/* Results list */}
          <div className="w-1/2 min-w-0 flex flex-col gap-2 overflow-y-auto">
            {isLoading ? (
              <div className="space-y-2">
                {Array.from({ length: 5 }).map((_, i) => (
                  <Skeleton key={i} className="h-16 w-full rounded-lg" />
                ))}
              </div>
            ) : !results || results.length === 0 ? (
              <div className="flex-1 flex items-center justify-center text-muted-foreground">
                <div className="text-center">
                  <Search className="h-8 w-8 mx-auto mb-2 opacity-30" />
                  <p className="text-sm">
                    {mode === 'browse' && !browseRepo
                      ? 'Enter a repository to browse'
                      : searchQuery.length < 2 && mode !== 'browse'
                      ? 'Type at least 2 characters to search'
                      : 'No skills found'}
                  </p>
                </div>
              </div>
            ) : (
              <>
                {/* Bulk select controls */}
                {results.length > 1 && (
                  <div className="flex items-center justify-between pb-2 border-b mb-1">
                    <button
                      onClick={toggleSelectAll}
                      className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground"
                    >
                      {selectedResults.size === results.length ? (
                        <CheckSquare className="h-3.5 w-3.5" />
                      ) : (
                        <Square className="h-3.5 w-3.5" />
                      )}
                      {selectedResults.size > 0 ? `${selectedResults.size} selected` : 'Select all'}
                    </button>
                    {selectedResults.size > 0 && (
                      <Button
                        size="sm"
                        variant="outline"
                        className="h-7 text-xs"
                        onClick={handleBulkImport}
                        disabled={bulkImporting}
                      >
                        {bulkImporting ? (
                          <Loader2 className="mr-1.5 h-3 w-3 animate-spin" />
                        ) : (
                          <Download className="mr-1.5 h-3 w-3" />
                        )}
                        Import {selectedResults.size}
                      </Button>
                    )}
                  </div>
                )}
                {results.map((result) => (
                  <div key={result.id} className="flex items-start gap-1.5">
                    <button
                      onClick={(e) => { e.stopPropagation(); toggleBulkSelect(result.id); }}
                      className="mt-3 shrink-0 text-muted-foreground hover:text-foreground"
                    >
                      {selectedResults.has(result.id) ? (
                        <CheckSquare className="h-3.5 w-3.5 text-primary" />
                      ) : (
                        <Square className="h-3.5 w-3.5" />
                      )}
                    </button>
                    <div className="flex-1 min-w-0">
                      <SkillResultCard
                        result={result}
                        isSelected={selectedResult?.id === result.id}
                        onClick={() => setSelectedResult(result)}
                      />
                    </div>
                  </div>
                ))}
                {mode === 'browse' && browseHasMore && (
                  <div className="pt-1 text-center">
                    <Button
                      variant="outline"
                      size="sm"
                      className="h-8 text-xs"
                      onClick={() => setBrowseOffset((prev) => prev + BROWSE_PAGE_SIZE)}
                      disabled={loadingMore}
                    >
                      {loadingMore ? (
                        <Loader2 className="mr-1.5 h-3 w-3 animate-spin" />
                      ) : null}
                      Load more ({results.length} of {browseTotal})
                    </Button>
                  </div>
                )}
              </>
            )}
          </div>

          {/* Detail / Import panel */}
          <div className="w-1/2 min-w-0 border-l pl-4">
            {selectedResult ? (
              <div className="space-y-4">
                <div>
                  <h3 className="font-semibold text-base">{selectedResult.name}</h3>
                  <p className="text-xs text-muted-foreground mt-0.5">
                    {selectedResult.source}
                    {selectedResult.slug && ` / ${selectedResult.slug}`}
                  </p>
                  {selectedResult.url && (
                    <a
                      href={selectedResult.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1 text-xs text-primary hover:underline mt-1"
                    >
                      <ExternalLink className="h-3 w-3" />
                      View source
                    </a>
                  )}
                </div>

                {/* Import controls */}
                <div className="space-y-3 border rounded-lg p-3">
                  <div className="space-y-2">
                    <label className="text-xs font-medium">Visibility</label>
                    <Select value={importVisibility} onValueChange={(v) => setImportVisibility(v as 'public' | 'private')}>
                      <SelectTrigger className="h-8 text-xs">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="public">Public</SelectItem>
                        <SelectItem value="private">Private</SelectItem>
                      </SelectContent>
                    </Select>
                    <p className="text-[11px] text-muted-foreground">Who can discover this skill in the registry</p>
                  </div>

                  <div className="space-y-2 border-t pt-3">
                    <label className="flex items-center gap-2 text-xs">
                      <input
                        type="checkbox"
                        checked={overwrite}
                        onChange={(e) => setOverwrite(e.target.checked)}
                        className="rounded border-input"
                      />
                      Overwrite if already in registry
                    </label>
                  </div>

                  <Button
                    className="w-full"
                    onClick={() => handleImport(false)}
                    disabled={importMutation.isPending}
                  >
                    {importMutation.isPending ? (
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    ) : (
                      <Download className="mr-2 h-4 w-4" />
                    )}
                    Add to Registry
                  </Button>
                </div>

                {/* Security verdict (shown after import attempt if available) */}
                {importMutation.data?.security && (
                  <div className="space-y-1.5">
                    <span className="text-xs font-medium text-muted-foreground">Security Assessment</span>
                    <SecurityVerdictBadge verdict={importMutation.data.security} />
                  </div>
                )}
              </div>
            ) : (
              <div className="flex items-center justify-center h-full text-muted-foreground">
                <div className="text-center">
                  <Package className="h-8 w-8 mx-auto mb-2 opacity-30" />
                  <p className="text-sm">Select a skill to preview</p>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Unsafe import confirmation */}
      <AlertDialog open={showUnsafeDialog} onOpenChange={setShowUnsafeDialog}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle className="flex items-center gap-2">
              <ShieldAlert className="h-5 w-5 text-destructive" />
              Security Warning
            </AlertDialogTitle>
            <AlertDialogDescription>
              {unsafeMessage}
              <br /><br />
              Force importing bypasses the security check. Only proceed if you trust the source.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => { setShowUnsafeDialog(false); handleImport(true); }}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              Force Import
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
