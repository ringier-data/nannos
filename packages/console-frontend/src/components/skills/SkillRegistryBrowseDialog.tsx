import { useState, useEffect, useMemo } from 'react';
import { Search, Loader2, ExternalLink } from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { searchSkillsApiV1SkillsRegistrySearchGetOptions } from '@/api/generated/@tanstack/react-query.gen';
import type { SkillSearchResult } from '@/api/generated/types.gen';

interface SkillRegistryBrowseDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Label for the title */
  title?: string;
  /** Optional description below the title */
  description?: string;
  /** Label for the action button on each skill row */
  actionLabel?: string;
  /** Called when user clicks the action button for a skill */
  onAction: (skill: SkillSearchResult) => void;
  /** Whether the action is currently pending (disables all action buttons) */
  actionPending?: boolean;
  /** Optional extra content rendered between description and search input (e.g. scope picker) */
  headerContent?: React.ReactNode;
}

export function SkillRegistryBrowseDialog({
  open,
  onOpenChange,
  title = 'Browse skill registry',
  description,
  actionLabel = 'Select',
  onAction,
  actionPending = false,
  headerContent,
}: SkillRegistryBrowseDialogProps) {
  const [searchQuery, setSearchQuery] = useState('');
  const [searchDebounced, setSearchDebounced] = useState('*');
  const [searchOffset, setSearchOffset] = useState(0);
  const [accumulatedResults, setAccumulatedResults] = useState<SkillSearchResult[]>([]);
  const [expandedSkillId, setExpandedSkillId] = useState<string | null>(null);

  // Debounce timer
  const debounceSearch = useMemo(() => {
    let timer: ReturnType<typeof setTimeout>;
    return (value: string) => {
      clearTimeout(timer);
      timer = setTimeout(() => setSearchDebounced(value), 300);
    };
  }, []);

  const { data: searchResults, isLoading: searchLoading } = useQuery({
    ...searchSkillsApiV1SkillsRegistrySearchGetOptions({
      query: { q: searchDebounced, source: 'registry', limit: 20, offset: searchOffset },
    }),
    enabled: open && searchDebounced.length >= 1,
  });

  // Accumulate paginated results
  useEffect(() => {
    if (searchResults?.data) {
      if (searchOffset === 0) {
        setAccumulatedResults(searchResults.data);
      } else {
        setAccumulatedResults((prev) => [...prev, ...searchResults.data!]);
      }
    }
  }, [searchResults, searchOffset]);

  // Sync cached results when dialog reopens
  useEffect(() => {
    if (open && searchResults?.data) {
      setAccumulatedResults(searchResults.data);
    }
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  // Reset offset when search query changes
  useEffect(() => {
    setSearchOffset(0);
  }, [searchDebounced]);

  // Reset state when dialog closes
  useEffect(() => {
    if (!open) {
      setSearchQuery('');
      setSearchDebounced('*');
      setSearchOffset(0);
      setAccumulatedResults([]);
      setExpandedSkillId(null);
    }
  }, [open]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          {description && <DialogDescription>{description}</DialogDescription>}
        </DialogHeader>
        <div className="space-y-4">
          {headerContent}

          {/* Search */}
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
            <Input
              placeholder="Search registry..."
              value={searchQuery}
              onChange={(e) => {
                setSearchQuery(e.target.value);
                debounceSearch(e.target.value || '*');
              }}
              className="pl-9"
              autoFocus
            />
          </div>

          {/* Results */}
          <div className="max-h-72 overflow-y-auto space-y-1.5">
            {searchLoading && searchOffset === 0 ? (
              <div className="flex items-center justify-center py-8">
                <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
              </div>
            ) : accumulatedResults.length === 0 ? (
              <p className="text-center text-xs text-muted-foreground py-8">
                No skills found
              </p>
            ) : (
              <>
                {accumulatedResults.map((skill: SkillSearchResult) => {
                  const isExpanded = expandedSkillId === (skill.id ?? skill.name);
                  return (
                    <div
                      key={skill.id ?? skill.name}
                      className="p-2.5 border rounded-lg hover:bg-accent/50 transition-colors"
                    >
                      <div className="flex items-center justify-between">
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-1.5">
                            <a
                              href={`/app/skill-registry?skill=${skill.slug || skill.id}`}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="text-sm font-medium truncate hover:underline text-primary"
                            >
                              {skill.name}
                            </a>
                            <a
                              href={`/app/skill-registry?skill=${skill.slug || skill.id}`}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="text-muted-foreground hover:text-primary shrink-0"
                            >
                              <ExternalLink className="h-3 w-3" />
                            </a>
                          </div>
                          {skill.description && (
                            <p
                              className={`text-xs text-muted-foreground mt-0.5 cursor-pointer ${isExpanded ? '' : 'line-clamp-2'}`}
                              onClick={() => setExpandedSkillId(isExpanded ? null : (skill.id ?? skill.name) ?? null)}
                            >
                              {skill.description}
                            </p>
                          )}
                          {!skill.description && skill.source && (
                            <p className="text-xs text-muted-foreground truncate">{skill.source}</p>
                          )}
                        </div>
                        <Button
                          size="sm"
                          variant="outline"
                          className="h-7 text-xs ml-3 shrink-0"
                          onClick={() => onAction(skill)}
                          disabled={actionPending || !skill.id}
                        >
                          {actionPending ? (
                            <Loader2 className="h-3 w-3 animate-spin" />
                          ) : (
                            actionLabel
                          )}
                        </Button>
                      </div>
                    </div>
                  );
                })}
                {searchResults?.has_more && (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="w-full text-xs text-muted-foreground"
                    onClick={() => setSearchOffset((prev) => prev + 20)}
                    disabled={searchLoading}
                  >
                    {searchLoading ? (
                      <Loader2 className="h-3 w-3 animate-spin mr-1" />
                    ) : null}
                    Load more
                  </Button>
                )}
              </>
            )}
          </div>
          <div className="border-t pt-3">
            <a
              href="/app/skill-registry"
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-1.5 text-xs text-primary hover:underline"
            >
              <ExternalLink className="h-3 w-3" />
              Create a new skill in the registry
            </a>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
