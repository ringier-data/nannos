import { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router';
import { Plus, LibraryBig, Users, AlertTriangle, Search } from 'lucide-react';
import { useQuery, keepPreviousData } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Pagination } from '@/components/admin/Pagination';
import { useDebouncedValue } from '@/hooks/use-debounced-value';
import { useEmbeddingConfigured } from '@/config/models';
import { CatalogList } from '@/components/catalogs/CatalogList';
import { CreateCatalogDialog } from '@/components/catalogs/CreateCatalogDialog';
import { listCatalogsOptions } from '@/api/generated/@tanstack/react-query.gen';
import type { Catalog } from '@/api/generated/types.gen';

const PAGE_SIZE = 20;

type TabId = 'my' | 'accessible';

interface Tab {
  id: TabId;
  label: string;
  icon: typeof LibraryBig;
}

const tabs: Tab[] = [
  { id: 'my', label: 'My Catalogs', icon: LibraryBig },
  { id: 'accessible', label: 'Accessible', icon: Users },
];

const TAB_IDS = new Set<string>(tabs.map((t) => t.id));

function getTabFromHash(): TabId {
  const hash = window.location.hash.replace('#', '');
  return TAB_IDS.has(hash) ? (hash as TabId) : 'my';
}

export function CatalogsPage() {
  const navigate = useNavigate();
  const [activeTab, setActiveTab] = useState<TabId>(getTabFromHash);
  const [showCreateDialog, setShowCreateDialog] = useState(false);
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(1);
  const debouncedSearch = useDebouncedValue(search);
  const { embeddingConfigured } = useEmbeddingConfigured();

  const handleTabChange = useCallback((tab: TabId) => {
    setActiveTab(tab);
    setPage(1);
    window.location.hash = tab;
  }, []);

  const handleSearchChange = (value: string) => {
    setSearch(value);
    setPage(1);
  };

  useEffect(() => {
    const onHashChange = () => setActiveTab(getTabFromHash());
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  // The owned / shared split and the search both happen server-side: dividing a
  // page in the browser would show each tab an arbitrary slice of its contents.
  const { data: catalogsData, isFetching } = useQuery({
    ...listCatalogsOptions({
      query: {
        page,
        limit: PAGE_SIZE,
        search: debouncedSearch || undefined,
        ownership: activeTab === 'my' ? 'owned' : 'shared',
      },
    }),
    placeholderData: keepPreviousData,
  });

  const catalogs = catalogsData?.items ?? [];
  const total = catalogsData?.total ?? 0;

  const getEmptyMessage = (): string => {
    if (debouncedSearch) return 'No catalogs match your search';
    switch (activeTab) {
      case 'my':
        return "You haven't created any catalogs yet";
      case 'accessible':
        return 'No catalogs have been shared with you';
      default:
        return 'No catalogs found';
    }
  };

  const handleSelectCatalog = (catalog: Catalog) => {
    navigate(`/app/catalogs/${catalog.id}`);
  };

  return (
    <div className="flex flex-col gap-6 p-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Catalogs</h1>
          <p className="text-muted-foreground">
            Connect document repositories for semantic search
          </p>
        </div>
        <Button onClick={() => setShowCreateDialog(true)} disabled={!embeddingConfigured}>
          <Plus className="mr-2 h-4 w-4" />
          Create Catalog
        </Button>
      </div>

      {/* Embedding-dependent: catalogs need a default embedding model to index. */}
      {!embeddingConfigured && (
        <div className="flex items-start gap-2 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-900/50 dark:bg-amber-950/40 dark:text-amber-200">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            Catalog indexing is disabled until a default embedding model is configured. An
            administrator can set one in <strong>Admin → Model Gateway</strong> (register an embedding
            model, then “Make default”).
          </span>
        </div>
      )}

      {/* Tabs */}
      <div className="flex gap-1 border-b">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            onClick={() => handleTabChange(tab.id)}
            className={`flex items-center gap-2 px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px ${
              activeTab === tab.id
                ? 'border-primary text-primary'
                : 'border-transparent text-muted-foreground hover:text-foreground hover:border-muted-foreground/50'
            }`}
          >
            <tab.icon className="h-4 w-4" />
            {tab.label}
          </button>
        ))}
      </div>

      {/* Search */}
      <div className="relative">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
        <Input
          placeholder="Search catalogs by name or description..."
          value={search}
          onChange={(e) => handleSearchChange(e.target.value)}
          className="pl-9"
        />
      </div>

      {/* Content */}
      <div className={isFetching ? 'opacity-60 transition-opacity' : 'transition-opacity'}>
        <CatalogList
          catalogs={catalogs}
          onSelect={handleSelectCatalog}
          emptyMessage={getEmptyMessage()}
        />
      </div>

      <Pagination page={page} limit={PAGE_SIZE} total={total} onPageChange={setPage} />

      {/* Create Dialog */}
      <CreateCatalogDialog
        open={showCreateDialog}
        onOpenChange={setShowCreateDialog}
      />
    </div>
  );
}
