import { useState, useMemo, useEffect } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Plus, Edit2, Trash2, Copy, Info } from 'lucide-react';
import { toast } from 'sonner';
import {
  listRateCardEntriesApiV1AdminRateCardsGetOptions,
  createRateCardEntryApiV1AdminRateCardsEntryPostMutation,
  expireRateCardEntryApiV1AdminRateCardsExpireRateIdPostMutation,
} from '@/api/generated/@tanstack/react-query.gen';
import type { RateCardEntry, RateCardEntryCreate } from '@/api/generated';
import { Button } from '@/components/ui/button';

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';

interface GroupedModel {
  provider: string;
  model_name: string;
  model_name_pattern: string | null;
  entries: RateCardEntry[];
  inputPrice?: number;
  outputPrice?: number;
  otherPrices: Array<{ billing_unit: string; price: number }>;
}

export function RateCardsPage() {
  const [selectedProvider, setSelectedProvider] = useState<string>('all');
  const [addModelOpen, setAddModelOpen] = useState(false);
  const [editModel, setEditModel] = useState<GroupedModel | null>(null);

  const queryClient = useQueryClient();

  // Fetch all entries to get available providers (unfiltered)
  const { data: allEntriesData } = useQuery({
    ...listRateCardEntriesApiV1AdminRateCardsGetOptions({
      query: {
        active_only: true,
      },
    }),
  });

  // Fetch filtered entries for display
  const { data: entriesData, isLoading: entriesLoading } = useQuery({
    ...listRateCardEntriesApiV1AdminRateCardsGetOptions({
      query: {
        provider: selectedProvider !== 'all' ? selectedProvider : undefined,
        active_only: true,
      },
    }),
  });

  const createEntryMutation = useMutation({
    ...createRateCardEntryApiV1AdminRateCardsEntryPostMutation(),
    onSuccess: () => {
      toast.success('Rate card created successfully');
      queryClient.invalidateQueries({ queryKey: ['listRateCardEntriesApiV1AdminRateCardsGet'] });
      setAddModelOpen(false);
      setEditModel(null);
    },
    onError: () => {
      toast.error('Failed to create rate card entry');
    },
  });

  const expireMutation = useMutation({
    ...expireRateCardEntryApiV1AdminRateCardsExpireRateIdPostMutation(),
    onSuccess: () => {
      toast.success('Rate card entries expired');
      queryClient.invalidateQueries({ queryKey: ['listRateCardEntriesApiV1AdminRateCardsGet'] });
    },
    onError: () => {
      toast.error('Failed to expire rate cards');
    },
  });

  const entries = entriesData?.entries ?? [];

  // Get all available providers from unfiltered data
  const allProviders = useMemo(() => {
    const allEntries = allEntriesData?.entries ?? [];
    return Array.from(new Set(allEntries.map((e: RateCardEntry) => e.provider))).sort();
  }, [allEntriesData]);

  // Group entries by model
  const groupedModels = useMemo(() => {
    const groups = new Map<string, GroupedModel>();
    
    entries.forEach((entry: RateCardEntry) => {
      const key = `${entry.provider}::${entry.model_name}`;
      
      if (!groups.has(key)) {
        groups.set(key, {
          provider: entry.provider,
          model_name: entry.model_name,
          model_name_pattern: entry.model_name_pattern ?? null,
          entries: [],
          otherPrices: [],
        });
      }
      
      const group = groups.get(key)!;;
      group.entries.push(entry);
      
      // Categorize prices by flow_direction
      if (entry.flow_direction === 'input' && entry.billing_unit === 'base_input_tokens') {
        group.inputPrice = parseFloat(entry.price_per_million);
      } else if (entry.flow_direction === 'output' && entry.billing_unit === 'base_output_tokens') {
        group.outputPrice = parseFloat(entry.price_per_million);
      } else {
        // All other billing unit types (cache_read_input_tokens, cache_creation_input_tokens, reasoning_tokens, etc.)
        group.otherPrices.push({
          billing_unit: entry.billing_unit,
          price: parseFloat(entry.price_per_million),
        });
      }
    });
    
    return Array.from(groups.values());
  }, [entries]);

  const handleExpireModel = (model: GroupedModel) => {
    const effectiveUntil = new Date().toISOString();
    
    // Expire all entries for this model
    Promise.all(
      model.entries.map(entry =>
        expireMutation.mutateAsync({
          path: { rate_id: entry.id },
          query: { effective_until: effectiveUntil },
        })
      )
    ).catch(() => {
      // Error already handled by mutation
    });
  };

  const handleCopyModel = (model: GroupedModel) => {
    // Open add dialog with pre-filled data from this model
    setEditModel(model);
    setAddModelOpen(true);
  };

  return (
    <div className="container mx-auto p-6 space-y-6">
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-3xl font-bold">Rate Cards</h1>
          <p className="text-muted-foreground mt-1">Manage billing unit pricing for cost calculation</p>
        </div>
        <Button onClick={() => { setEditModel(null); setAddModelOpen(true); }}>
          <Plus className="w-4 h-4 mr-2" />
          Add Model Pricing
        </Button>
      </div>

      {/* Info Banner */}
      <Card className="bg-blue-50/50 dark:bg-blue-950/20 border-blue-200 dark:border-blue-800">
        <CardContent className="pt-0">
          <div className="flex gap-3">
            <Info className="w-5 h-5 text-blue-600 dark:text-blue-400 mt-0.5 flex-shrink-0" />
            <div className="space-y-1">
              <p className="text-sm font-medium text-blue-900 dark:text-blue-100">
                Model Variant Matching
              </p>
              <p className="text-sm text-blue-800 dark:text-blue-200">
                Rate cards support regex patterns to match model variants automatically. For example, <code className="text-xs bg-blue-100 dark:bg-blue-900 px-1.5 py-0.5 rounded">^gpt-4o-mini(-\d{'{4}'}-\d{'{2}'}-\d{'{2}'})?$</code> matches both <code className="text-xs bg-blue-100 dark:bg-blue-900 px-1.5 py-0.5 rounded">gpt-4o-mini</code> and <code className="text-xs bg-blue-100 dark:bg-blue-900 px-1.5 py-0.5 rounded">gpt-4o-mini-2024-07-18</code>.
              </p>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Filters */}
      <Card>
        <CardHeader className="pb-4">
          <CardTitle>Filters</CardTitle>
          <CardDescription>Filter rate cards by provider</CardDescription>
        </CardHeader>
        <CardContent className="pb-6">
          <div className="w-64">
            <Label htmlFor="provider-filter">Provider</Label>
            <Select value={selectedProvider} onValueChange={setSelectedProvider}>
              <SelectTrigger id="provider-filter" className="mt-1.5">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All Providers</SelectItem>
                {allProviders.map((p) => (
                  <SelectItem key={p} value={p}>{p}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </CardContent>
      </Card>

      {/* Grouped Models */}
      {entriesLoading ? (
        <div className="text-center py-8">Loading...</div>
      ) : groupedModels.length === 0 ? (
        <Card>
          <CardContent className="pt-6">
            <div className="text-center text-muted-foreground">
              No rate cards found. Add a model to get started.
            </div>
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-4">
          {groupedModels.map((model) => (
            <Card key={`${model.provider}::${model.model_name}`}>
              <CardHeader>
                <div className="flex justify-between items-start">
                  <div>
                    <CardTitle className="flex items-center gap-2">
                      <Badge variant="outline">{model.provider}</Badge>
                      <span className="font-mono">{model.model_name}</span>
                    </CardTitle>
                    <CardDescription className="mt-2">
                      {model.model_name_pattern ? (
                        <div className="flex items-center gap-2">
                          <span>Matches pattern:</span>
                          <code className="text-xs bg-muted px-2 py-1 rounded">{model.model_name_pattern}</code>
                        </div>
                      ) : (
                        <span>Active pricing configuration</span>
                      )}
                    </CardDescription>
                  </div>
                  <div className="flex gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => { setEditModel(model); setAddModelOpen(true); }}
                    >
                      <Edit2 className="w-4 h-4 mr-1" />
                      Edit
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => handleCopyModel(model)}
                    >
                      <Copy className="w-4 h-4 mr-1" />
                      Copy
                    </Button>
                    <Button
                      variant="destructive"
                      size="sm"
                      onClick={() => handleExpireModel(model)}
                    >
                      <Trash2 className="w-4 h-4 mr-1" />
                      Delete
                    </Button>
                  </div>
                </div>
              </CardHeader>
              <CardContent>
                <div className="space-y-6">
                  {/* Base Input/Output Prices */}
                  <div>
                    <div className="text-sm font-medium text-muted-foreground mb-3">Base Pricing</div>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                      {/* Input Price */}
                      <div className="border rounded-lg p-4 bg-blue-50/50 dark:bg-blue-950/20">
                        <div className="text-sm font-medium text-blue-700 dark:text-blue-400 mb-1">Input</div>
                        <div className="text-2xl font-bold text-blue-900 dark:text-blue-300">
                          {model.inputPrice !== undefined 
                            ? `$${model.inputPrice.toFixed(2)}`
                            : <span className="text-muted-foreground text-base">Not set</span>
                          }
                        </div>
                        <div className="text-xs text-muted-foreground mt-1">per 1M units</div>
                      </div>

                      {/* Output Price */}
                      <div className="border rounded-lg p-4 bg-green-50/50 dark:bg-green-950/20">
                        <div className="text-sm font-medium text-green-700 dark:text-green-400 mb-1">Output</div>
                        <div className="text-2xl font-bold text-green-900 dark:text-green-300">
                          {model.outputPrice !== undefined 
                            ? `$${model.outputPrice.toFixed(2)}`
                            : <span className="text-muted-foreground text-base">Not set</span>
                          }
                        </div>
                        <div className="text-xs text-muted-foreground mt-1">per 1M units</div>
                      </div>
                    </div>
                  </div>

                  {/* Other Billing Unit Types - Separated by Input/Output */}
                  {model.otherPrices.length > 0 && (
                    <div>
                      <div className="text-sm font-medium text-muted-foreground mb-3">Additional Unit Types</div>
                      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                        {model.entries
                          .filter(entry => entry.billing_unit !== 'base_input_tokens' && entry.billing_unit !== 'base_output_tokens')
                          .map((entry) => {
                            const bgColor = entry.flow_direction === 'input' 
                              ? 'bg-blue-50/50 dark:bg-blue-950/20 border-blue-200 dark:border-blue-800'
                              : entry.flow_direction === 'output'
                              ? 'bg-green-50/50 dark:bg-green-950/20 border-green-200 dark:border-green-800'
                              : 'bg-gray-50/50 dark:bg-gray-950/20';
                            
                            const textColor = entry.flow_direction === 'input'
                              ? 'text-blue-700 dark:text-blue-400'
                              : entry.flow_direction === 'output'
                              ? 'text-green-700 dark:text-green-400'
                              : 'text-muted-foreground';
                            
                            return (
                              <div key={entry.billing_unit} className={`border rounded-lg p-4 ${bgColor}`}>
                                <div className={`text-sm font-medium mb-1 ${textColor}`}>
                                  {entry.billing_unit.split('_').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ')}
                                </div>
                                <div className="text-2xl font-bold">${parseFloat(entry.price_per_million).toFixed(2)}</div>
                                <div className="text-xs text-muted-foreground mt-1">per 1M units</div>
                              </div>
                            );
                          })}
                      </div>
                    </div>
                  )}
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {/* Add/Edit Model Dialog */}
      <ModelPricingDialog
        open={addModelOpen}
        onOpenChange={(open) => {
          setAddModelOpen(open);
          if (!open) setEditModel(null);
        }}
        onSubmit={async (entries) => {
          // If editing, expire old entries first
          if (editModel) {
            const effectiveUntil = new Date().toISOString();
            await Promise.all(
              editModel.entries.map(entry =>
                expireMutation.mutateAsync({
                  path: { rate_id: entry.id },
                  query: { effective_until: effectiveUntil },
                })
              )
            );
          }
          
          // Create all new entries
          for (const entry of entries) {
            await createEntryMutation.mutateAsync({ body: entry });
          }
        }}
        existingModel={editModel}
      />
    </div>
  );
}

// Model Pricing Dialog Component - Handles both add and edit
function ModelPricingDialog({ open, onOpenChange, onSubmit, existingModel }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSubmit: (entries: RateCardEntryCreate[]) => Promise<void>;
  existingModel?: GroupedModel | null;
}) {
  const isEdit = !!existingModel;
  
  const [formData, setFormData] = useState({
    provider: '',
    model_name: '',
    model_name_pattern: '',
    input_price: '',
    output_price: '',
    input_breakdown: [] as Array<{ billing_unit: string; price: string }>,
    output_breakdown: [] as Array<{ billing_unit: string; price: string }>,
  });

  // Reset or pre-fill form when dialog opens/changes
  useEffect(() => {
    if (open) {
      if (existingModel) {
        // Categorize other prices into input/output breakdown based on flow_direction
        const inputBreakdown: Array<{ billing_unit: string; price: string }> = [];
        const outputBreakdown: Array<{ billing_unit: string; price: string }> = [];
        
        // Find entries by flow_direction from the full entries list
        existingModel.entries.forEach(entry => {
          // Skip base input/output prices as they're handled separately
          if (entry.billing_unit === 'base_input_tokens' || entry.billing_unit === 'base_output_tokens') {
            return;
          }
          
          if (entry.flow_direction === 'input') {
            inputBreakdown.push({ 
              billing_unit: entry.billing_unit, 
              price: parseFloat(entry.price_per_million).toString() 
            });
          } else if (entry.flow_direction === 'output') {
            outputBreakdown.push({ 
              billing_unit: entry.billing_unit, 
              price: parseFloat(entry.price_per_million).toString() 
            });
          }
        });

        setFormData({
          provider: existingModel.provider,
          model_name: existingModel.model_name,
          model_name_pattern: existingModel.model_name_pattern || '',
          input_price: existingModel.inputPrice?.toString() || '',
          output_price: existingModel.outputPrice?.toString() || '',
          input_breakdown: inputBreakdown,
          output_breakdown: outputBreakdown,
        });
      } else {
        setFormData({
          provider: '',
          model_name: '',
          model_name_pattern: '',
          input_price: '',
          output_price: '',
          input_breakdown: [],
          output_breakdown: [],
        });
      }
    }
  }, [open, existingModel]);

  const addInputBreakdown = () => {
    setFormData({
      ...formData,
      input_breakdown: [...formData.input_breakdown, { billing_unit: '', price: '' }],
    });
  };

  const removeInputBreakdown = (index: number) => {
    setFormData({
      ...formData,
      input_breakdown: formData.input_breakdown.filter((_, i) => i !== index),
    });
  };

  const updateInputBreakdown = (index: number, field: 'billing_unit' | 'price', value: string) => {
    const updated = [...formData.input_breakdown];
    updated[index] = { ...updated[index], [field]: value };
    setFormData({ ...formData, input_breakdown: updated });
  };

  const addOutputBreakdown = () => {
    setFormData({
      ...formData,
      output_breakdown: [...formData.output_breakdown, { billing_unit: '', price: '' }],
    });
  };

  const removeOutputBreakdown = (index: number) => {
    setFormData({
      ...formData,
      output_breakdown: formData.output_breakdown.filter((_, i) => i !== index),
    });
  };

  const updateOutputBreakdown = (index: number, field: 'billing_unit' | 'price', value: string) => {
    const updated = [...formData.output_breakdown];
    updated[index] = { ...updated[index], [field]: value };
    setFormData({ ...formData, output_breakdown: updated });
  };

  const handleSubmit = async () => {
    const entries: RateCardEntryCreate[] = [];

    // Add input price if provided
    if (formData.input_price) {
      entries.push({
        provider: formData.provider,
        model_name: formData.model_name,
        billing_unit: 'base_input_tokens',
        flow_direction: 'input',
        price_per_million: parseFloat(formData.input_price),
      });
    }

    // Add output price if provided
    if (formData.output_price) {
      entries.push({
        provider: formData.provider,
        model_name: formData.model_name,
        billing_unit: 'base_output_tokens',
        flow_direction: 'output',
        price_per_million: parseFloat(formData.output_price),
      });
    }

    // Add input breakdown types
    formData.input_breakdown.forEach((type) => {
      if (type.billing_unit && type.price) {
        entries.push({
          provider: formData.provider,
          model_name: formData.model_name,
          billing_unit: type.billing_unit,
          flow_direction: 'input',
          price_per_million: parseFloat(type.price),
        });
      }
    });

    // Add output breakdown types
    formData.output_breakdown.forEach((type) => {
      if (type.billing_unit && type.price) {
        entries.push({
          provider: formData.provider,
          model_name: formData.model_name,
          billing_unit: type.billing_unit,
          flow_direction: 'output',
          price_per_million: parseFloat(type.price),
        });
      }
    });

    if (entries.length === 0) {
      toast.error('Please provide at least one price');
      return;
    }

    await onSubmit(entries);
    onOpenChange(false);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{isEdit ? 'Edit' : 'Add'} Model Pricing</DialogTitle>
          <DialogDescription>
            {isEdit 
              ? 'Update pricing for this model. Changes will expire old rates and create new ones.'
              : 'Set pricing for a new model. Provide input/output prices and optionally add other token types.'
            }
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          {/* Provider and Model Name */}
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-2">
              <Label htmlFor="provider">Provider</Label>
              <Input
                id="provider"
                value={formData.provider}
                onChange={(e) => setFormData({ ...formData, provider: e.target.value })}
                placeholder="e.g., bedrock-anthropic"
                disabled={isEdit}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="model_name">Model Name</Label>
              <Input
                id="model_name"
                value={formData.model_name}
                onChange={(e) => setFormData({ ...formData, model_name: e.target.value })}
                placeholder="e.g., claude-sonnet-4-20250514"
                disabled={isEdit}
              />
            </div>
          </div>

          {/* Model Name Pattern */}
          <div className="space-y-2">
            <Label htmlFor="model_name_pattern">
              Model Name Pattern (Optional)
              <span className="text-xs text-muted-foreground ml-2">Regex pattern for matching model variants</span>
            </Label>
            <Input
              id="model_name_pattern"
              value={formData.model_name_pattern}
              onChange={(e) => setFormData({ ...formData, model_name_pattern: e.target.value })}
              placeholder="e.g., ^gpt-4o-mini(-\d{4}-\d{2}-\d{2})?$"
              disabled={isEdit}
            />
            <p className="text-xs text-muted-foreground">
              Leave empty for exact match only. Use regex to match multiple model versions (e.g., gpt-4o-mini-2024-07-18).
            </p>
          </div>

          {/* Input Pricing Section */}
          <div className="space-y-3 border rounded-lg p-4 bg-muted/30">
            <div className="flex items-center justify-between">
              <Label className="text-base font-semibold">Input Pricing</Label>
            </div>
            <div className="space-y-2">
              <Label htmlFor="input_price">Base Input Price (per 1M units)</Label>
              <Input
                id="input_price"
                type="number"
                step="0.01"
                value={formData.input_price}
                onChange={(e) => setFormData({ ...formData, input_price: e.target.value })}
                placeholder="3.00"
              />
            </div>
            
            {/* Input Breakdown */}
            <div className="space-y-2">
              <div className="flex justify-between items-center">
                <Label className="text-sm text-muted-foreground">Additional Input Types</Label>
                <Button type="button" variant="outline" size="sm" onClick={addInputBreakdown}>
                  <Plus className="w-3 h-3 mr-1" />
                  Add
                </Button>
              </div>
              {formData.input_breakdown.length > 0 && (
                <div className="space-y-2">
                  {formData.input_breakdown.map((type, index) => (
                    <div key={index} className="flex gap-2">
                      <div className="flex-1">
                        <Input
                          value={type.billing_unit}
                          onChange={(e) => updateInputBreakdown(index, 'billing_unit', e.target.value)}
                          placeholder="e.g., cache_creation"
                          className="text-sm"
                        />
                      </div>
                      <div className="flex-1">
                        <Input
                          type="number"
                          step="0.01"
                          value={type.price}
                          onChange={(e) => updateInputBreakdown(index, 'price', e.target.value)}
                          placeholder="Price per 1M"
                          className="text-sm"
                        />
                      </div>
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon"
                        onClick={() => removeInputBreakdown(index)}
                      >
                        <Trash2 className="w-4 h-4" />
                      </Button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>

          {/* Output Pricing Section */}
          <div className="space-y-3 border rounded-lg p-4 bg-muted/30">
            <div className="flex items-center justify-between">
              <Label className="text-base font-semibold">Output Pricing</Label>
            </div>
            <div className="space-y-2">
              <Label htmlFor="output_price">Base Output Price (per 1M units)</Label>
              <Input
                id="output_price"
                type="number"
                step="0.01"
                value={formData.output_price}
                onChange={(e) => setFormData({ ...formData, output_price: e.target.value })}
                placeholder="15.00"
              />
            </div>
            
            {/* Output Breakdown */}
            <div className="space-y-2">
              <div className="flex justify-between items-center">
                <Label className="text-sm text-muted-foreground">Additional Output Types</Label>
                <Button type="button" variant="outline" size="sm" onClick={addOutputBreakdown}>
                  <Plus className="w-3 h-3 mr-1" />
                  Add
                </Button>
              </div>
              {formData.output_breakdown.length > 0 && (
                <div className="space-y-2">
                  {formData.output_breakdown.map((type, index) => (
                    <div key={index} className="flex gap-2">
                      <div className="flex-1">
                        <Input
                          value={type.billing_unit}
                          onChange={(e) => updateOutputBreakdown(index, 'billing_unit', e.target.value)}
                          placeholder="e.g., cache_read"
                          className="text-sm"
                        />
                      </div>
                      <div className="flex-1">
                        <Input
                          type="number"
                          step="0.01"
                          value={type.price}
                          onChange={(e) => updateOutputBreakdown(index, 'price', e.target.value)}
                          placeholder="Price per 1M"
                          className="text-sm"
                        />
                      </div>
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon"
                        onClick={() => removeOutputBreakdown(index)}
                      >
                        <Trash2 className="w-4 h-4" />
                      </Button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button onClick={handleSubmit}>{isEdit ? 'Update' : 'Create'} Pricing</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
