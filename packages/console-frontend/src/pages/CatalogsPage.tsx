import { useState } from 'react';
import { useNavigate } from 'react-router';
import { Plus, LibraryBig, Users } from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import { CatalogList } from '@/components/catalogs/CatalogList';
import { CreateCatalogDialog } from '@/components/catalogs/CreateCatalogDialog';
import { listCatalogsOptions } from '@/api/generated/@tanstack/react-query.gen';
import type { Catalog } from '@/api/generated/types.gen';
import { useAuth } from '@/contexts/AuthContext';

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

export function CatalogsPage() {
  const navigate = useNavigate();
  const [activeTab, setActiveTab] = useState<TabId>('my');
  const [showCreateDialog, setShowCreateDialog] = useState(false);
  const { user } = useAuth();

  const { data: catalogsData } = useQuery({
    ...listCatalogsOptions(),
  });

  const catalogs = catalogsData?.items ?? [];

  const getCatalogsForTab = (): Catalog[] => {
    switch (activeTab) {
      case 'my':
        return catalogs.filter((c) => c.owner_user_id === user?.id);
      case 'accessible':
        return catalogs.filter((c) => c.owner_user_id !== user?.id);
      default:
        return [];
    }
  };

  const getEmptyMessage = (): string => {
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
        <Button onClick={() => setShowCreateDialog(true)}>
          <Plus className="mr-2 h-4 w-4" />
          Create Catalog
        </Button>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 border-b">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
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

      {/* Content */}
      <CatalogList
        catalogs={getCatalogsForTab()}
        onSelect={handleSelectCatalog}
        emptyMessage={getEmptyMessage()}
      />

      {/* Create Dialog */}
      <CreateCatalogDialog
        open={showCreateDialog}
        onOpenChange={setShowCreateDialog}
      />
    </div>
  );
}
