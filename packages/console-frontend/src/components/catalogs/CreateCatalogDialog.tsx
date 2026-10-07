import { useState } from 'react';
import { useNavigate } from 'react-router';
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
import { createCatalogMutation, listCatalogsQueryKey } from '@/api/generated/@tanstack/react-query.gen';
import { getErrorMessage } from '@/lib/utils';
import { NannosForm } from '@/components/nannos/NannosForm';
import type { SubmitOutcome } from '@nannos/embed-sdk';

interface CreateCatalogDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function CreateCatalogDialog({ open, onOpenChange }: CreateCatalogDialogProps) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');

  const createMutation = useMutation({
    ...createCatalogMutation(),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: listCatalogsQueryKey() });
      toast.success('Catalog created');
      onOpenChange(false);
      resetForm();
      navigate(`/app/catalogs/${data.id}`);
    },
    onError: (err) => {
      toast.error('Failed to create catalog', { description: getErrorMessage(err) });
    },
  });

  const resetForm = () => {
    setName('');
    setDescription('');
  };

  const save = async (): Promise<SubmitOutcome> => {
    if (!name.trim()) {
      toast.error('Name is required');
      return { ok: false, detail: 'Name is required' };
    }
    try {
      await createMutation.mutateAsync({
        body: {
          name: name.trim(),
          description: description.trim() || undefined,
          source_type: 'google_drive',
          source_config: {},
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
    <Dialog open={open} onOpenChange={(o) => { if (!o) resetForm(); onOpenChange(o); }}>
      <DialogContent className="sm:max-w-md">
        <NannosForm
          type="Catalog"
          id={undefined}
          fields={{ name: [name, setName], description: [description, setDescription] }}
          submit={save}
        />
        <form onSubmit={handleSubmit}>
          <DialogHeader>
            <DialogTitle>Create Catalog</DialogTitle>
            <DialogDescription>
              Create a new catalog to connect a Google Drive folder for semantic search.
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-4 py-4">
            <div className="space-y-2">
              <Label htmlFor="catalog-name">Name</Label>
              <Input
                id="catalog-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g., Sales Presentations Q1"
                autoFocus
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="catalog-description">Description (optional)</Label>
              <Textarea
                id="catalog-description"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="What documents does this catalog contain?"
                rows={3}
              />
            </div>
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={createMutation.isPending}>
              {createMutation.isPending ? 'Creating...' : 'Create'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
