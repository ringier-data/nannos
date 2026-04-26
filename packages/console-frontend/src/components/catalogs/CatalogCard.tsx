import { LibraryBig, RefreshCw, AlertCircle, CheckCircle2 } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import type { Catalog } from '@/api/generated/types.gen';

interface CatalogCardProps {
  catalog: Catalog;
  onClick: () => void;
}

function statusBadge(status: string) {
  switch (status) {
    case 'active':
      return <Badge variant="default" className="bg-green-600"><CheckCircle2 className="mr-1 h-3 w-3" />Active</Badge>;
    case 'syncing':
      return <Badge variant="secondary"><RefreshCw className="mr-1 h-3 w-3 animate-spin" />Syncing</Badge>;
    case 'error':
      return <Badge variant="destructive"><AlertCircle className="mr-1 h-3 w-3" />Error</Badge>;
    case 'disabled':
      return <Badge variant="outline">Disabled</Badge>;
    default:
      return <Badge variant="outline">{status}</Badge>;
  }
}

function sourceLabel(sourceType: string) {
  switch (sourceType) {
    case 'google_drive':
      return 'Google Drive';
    default:
      return sourceType;
  }
}

export function CatalogCard({ catalog, onClick }: CatalogCardProps) {
  return (
    <div
      className="group flex flex-col gap-3 rounded-lg border bg-card p-4 hover:bg-accent/50 cursor-pointer transition-colors"
      onClick={onClick}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-start gap-2 min-w-0">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-primary/10">
            <LibraryBig className="h-4 w-4 text-primary" />
          </div>
          <div className="min-w-0">
            <h3 className="font-medium truncate">{catalog.name}</h3>
            <p className="text-xs text-muted-foreground">{sourceLabel(catalog.source_type)}</p>
          </div>
        </div>
        {statusBadge(catalog.status ?? 'active')}
      </div>

      {catalog.description && (
        <p className="text-sm text-muted-foreground line-clamp-2">
          {catalog.description}
        </p>
      )}

      <div className="flex items-center justify-between text-xs text-muted-foreground mt-auto pt-2 border-t">
        {catalog.owner && (
          <span>{catalog.owner.name}</span>
        )}
        {catalog.last_synced_at ? (
          <span>Synced {new Date(catalog.last_synced_at).toLocaleDateString()}</span>
        ) : (
          <span>Never synced</span>
        )}
      </div>
    </div>
  );
}
