import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { updateCatalogMutation, getCatalogQueryKey, listCatalogsQueryKey } from '@/api/generated/@tanstack/react-query.gen';
import type { Catalog } from '@/api/generated/types.gen';
import { getErrorMessage } from '@/lib/utils';
import { NannosForm } from '@/components/nannos/NannosForm';
import type { SubmitOutcome } from '@nannos/embed-sdk';

interface EditCatalogDialogProps {
  catalog: Catalog;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function EditCatalogDialog({ catalog, open, onOpenChange }: EditCatalogDialogProps) {
  const queryClient = useQueryClient();
  const [name, setName] = useState(catalog.name);
  const [description, setDescription] = useState(catalog.description ?? '');

  const updateMutation = useMutation({
    ...updateCatalogMutation(),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: getCatalogQueryKey({ path: { catalog_id: catalog.id } }) });
      queryClient.invalidateQueries({ queryKey: listCatalogsQueryKey() });
      toast.success('Catalog updated');
      onOpenChange(false);
    },
    onError: (err) => {
      toast.error('Failed to update catalog', { description: getErrorMessage(err) });
    },
  });

  const save = async (): Promise<SubmitOutcome> => {
    if (!name.trim()) {
      toast.error('Name is required');
      return { ok: false, detail: 'Name is required' };
    }
    try {
      await updateMutation.mutateAsync({
        path: { catalog_id: catalog.id },
        body: {
          name: name.trim(),
          description: description.trim() || undefined,
        },
      });
      return true;
    } catch (err) {
      // onError has already toasted.
      return { ok: false, detail: getErrorMessage(err) };
    }
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    void save();
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <NannosForm
          type="Catalog"
          id={catalog.id}
          fields={{ name: [name, setName], description: [description, setDescription] }}
          submit={save}
        />
        <form onSubmit={handleSubmit}>
          <DialogHeader>
            <DialogTitle>Edit Catalog</DialogTitle>
            <DialogDescription>
              Update catalog name and description.
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-4 py-4">
            <div className="space-y-2">
              <Label htmlFor="edit-name">Name</Label>
              <Input
                id="edit-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                autoFocus
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="edit-description">Description</Label>
              <Textarea
                id="edit-description"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                rows={3}
              />
            </div>
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={updateMutation.isPending}>
              {updateMutation.isPending ? 'Saving...' : 'Save'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
