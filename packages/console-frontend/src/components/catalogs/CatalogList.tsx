import { LibraryBig } from 'lucide-react';
import { CatalogCard } from './CatalogCard';
import type { Catalog } from '@/api/generated/types.gen';

interface CatalogListProps {
  catalogs: Catalog[];
  onSelect: (catalog: Catalog) => void;
  emptyMessage?: string;
}

export function CatalogList({ catalogs, onSelect, emptyMessage = 'No catalogs found' }: CatalogListProps) {
  if (catalogs.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-12 text-center">
        <LibraryBig className="h-12 w-12 text-muted-foreground/50 mb-4" />
        <p className="text-muted-foreground">{emptyMessage}</p>
      </div>
    );
  }

  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
      {catalogs.map((catalog) => (
        <CatalogCard
          key={catalog.id}
          catalog={catalog}
          onClick={() => onSelect(catalog)}
        />
      ))}
    </div>
  );
}
