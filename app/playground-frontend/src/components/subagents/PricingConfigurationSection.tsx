import { DollarSign, Plus, Trash2, ChevronDown, Info } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible';

interface PricingConfigurationSectionProps {
  isEditing: boolean;
  expanded: boolean;
  onExpandedChange: (expanded: boolean) => void;
  // Edit mode props
  rateCardEntries: Array<{ billing_unit: string; price_per_million: string }>;
  onRateCardEntriesChange: (entries: Array<{ billing_unit: string; price_per_million: string }>) => void;
  onFieldChange?: () => void;
  onFocusAreaChange?: (area: 'version' | 'config' | 'chat' | null) => void;
  disabled?: boolean;
  // View mode props
  pricingConfig?: any;
  // Style props
  asCard?: boolean; // If true, renders as Card component instead of plain Collapsible
}

export function PricingConfigurationSection({
  isEditing,
  expanded,
  onExpandedChange,
  rateCardEntries,
  onRateCardEntriesChange,
  onFieldChange,
  onFocusAreaChange,
  disabled = false,
  pricingConfig,
  asCard = false,
}: PricingConfigurationSectionProps) {
  const filledEntriesCount = rateCardEntries.filter(e => e.billing_unit && e.price_per_million).length;
  
  const content = (
    <>
      <p className="text-xs text-muted-foreground">
        Configure custom pricing for this agent. Leave empty to use system default rates.
      </p>
      <p className="text-xs text-muted-foreground">
        Define rates per billing unit (e.g., input_tokens, output_tokens, requests).
      </p>

      <div className="space-y-3">
        <Label>Rate Card Entries</Label>
        <div className="space-y-2">
          {rateCardEntries.map((entry, idx) => (
            <div key={idx} className="flex gap-2 items-start p-2 bg-muted/30 rounded">
              <div className="flex-1 space-y-1">
                <Label className="text-xs text-muted-foreground">Billing Unit</Label>
                <Input
                  placeholder="e.g., input_tokens"
                  value={entry.billing_unit}
                  onChange={(e) => {
                    const updated = [...rateCardEntries];
                    updated[idx].billing_unit = e.target.value;
                    onRateCardEntriesChange(updated);
                    onFieldChange?.();
                  }}
                  onFocus={() => onFocusAreaChange?.('config')}
                  onBlur={() => onFocusAreaChange?.(null)}
                  disabled={disabled}
                  className="h-9"
                />
              </div>
              <div className="w-32 space-y-1">
                <Label className="text-xs text-muted-foreground">$/Million</Label>
                <Input
                  type="number"
                  step="0.01"
                  min="0"
                  placeholder="0.00"
                  value={entry.price_per_million}
                  onChange={(e) => {
                    const updated = [...rateCardEntries];
                    updated[idx].price_per_million = e.target.value;
                    onRateCardEntriesChange(updated);
                    onFieldChange?.();
                  }}
                  onFocus={() => onFocusAreaChange?.('config')}
                  onBlur={() => onFocusAreaChange?.(null)}
                  disabled={disabled}
                  className="h-9"
                />
              </div>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => {
                  onRateCardEntriesChange(
                    rateCardEntries.filter((_, i) => i !== idx)
                  );
                  onFieldChange?.();
                }}
                disabled={disabled}
                className="mt-6"
              >
                <Trash2 className="h-4 w-4 text-destructive" />
              </Button>
            </div>
          ))}
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => {
              onRateCardEntriesChange([
                ...rateCardEntries,
                { billing_unit: '', price_per_million: '' },
              ]);
              onFieldChange?.();
            }}
            disabled={disabled}
          >
            + Add Billing Unit
          </Button>
        </div>
        <p className="text-xs text-muted-foreground">
          Define custom rates per billing unit (e.g., input_tokens, output_tokens)
        </p>
      </div>
    </>
  );

  // Card wrapper mode (for forms)
  if (asCard) {
    return (
      <Card>
        <Collapsible open={expanded} onOpenChange={onExpandedChange}>
          <CardHeader>
            <CollapsibleTrigger className="flex w-full items-center justify-between hover:opacity-80 transition-opacity [&[data-state=open]>svg]:rotate-180">
              <div className="text-left">
                <CardTitle className="flex items-center gap-2">
                  Pricing Configuration (Optional)
                  {filledEntriesCount > 0 && (
                    <span className="text-sm font-normal text-muted-foreground">
                      ({filledEntriesCount} entries)
                    </span>
                  )}
                </CardTitle>
                <CardDescription>
                  {expanded 
                    ? 'Configure custom pricing for this agent. Defaults to "requests" billing unit.'
                    : `${filledEntriesCount === 0 ? 'No pricing configured' : 'Pricing configured'} - Click to ${expanded ? 'collapse' : 'expand'}`
                  }
                </CardDescription>
              </div>
              <ChevronDown className="h-5 w-5 text-muted-foreground flex-shrink-0 transition-transform duration-200" />
            </CollapsibleTrigger>
          </CardHeader>
          <CollapsibleContent>
            <CardContent className="space-y-4">
              <div className="p-3 bg-muted/50 rounded-md space-y-2">
                <div className="flex items-start gap-2">
                  <Info className="h-4 w-4 mt-0.5 flex-shrink-0 text-muted-foreground" />
                  <div className="text-sm text-muted-foreground space-y-1">
                    <p>Configure custom pricing for this agent. Leave empty to use system default rates.</p>
                    <p>Define rates per billing unit (e.g., input_tokens, output_tokens, requests).</p>
                  </div>
                </div>
              </div>
              {content}
            </CardContent>
          </CollapsibleContent>
        </Collapsible>
      </Card>
    );
  }

  // Plain collapsible mode (for detail page)
  return (
    <Collapsible open={expanded} onOpenChange={onExpandedChange} className="flex-shrink-0">
      <div className="space-y-2">
        <CollapsibleTrigger asChild>
          <Button variant="ghost" className="w-full justify-between p-2 h-auto hover:bg-accent">
            <div className="flex items-center gap-2">
              <DollarSign className="h-4 w-4 text-muted-foreground" />
              <Label className="cursor-pointer font-normal">Custom Pricing (Optional)</Label>
            </div>
            <ChevronDown className={`h-4 w-4 text-muted-foreground transition-transform ${expanded ? 'rotate-180' : ''}`} />
          </Button>
        </CollapsibleTrigger>
        <CollapsibleContent className="space-y-3 pt-2 px-1">
          {isEditing ? (
            <>
              <p className="text-xs text-muted-foreground">
                Configure custom pricing for this agent. Defaults to "requests" billing unit. Leave empty to use system default rates.
              </p>

              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <Label className="text-sm">Rate Card Entries</Label>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => {
                      onRateCardEntriesChange([
                        ...rateCardEntries,
                        { billing_unit: '', price_per_million: '' },
                      ]);
                      onFieldChange?.();
                    }}
                    disabled={disabled}
                  >
                    <Plus className="h-4 w-4 mr-1" />
                    Add Billing Unit
                  </Button>
                </div>

                <div className="space-y-2">
                  {rateCardEntries.map((entry, idx) => (
                    <div key={idx} className="flex gap-2 items-start p-2 bg-muted/30 rounded">
                      <div className="flex-1 space-y-1">
                        <Label className="text-xs text-muted-foreground">Billing Unit</Label>
                        <Input
                          placeholder="e.g., input_tokens"
                          value={entry.billing_unit}
                          onChange={(e) => {
                            const updated = [...rateCardEntries];
                            updated[idx].billing_unit = e.target.value;
                            onRateCardEntriesChange(updated);
                              onFieldChange?.();
                            }}
                            onFocus={() => onFocusAreaChange?.('config')}
                            onBlur={() => onFocusAreaChange?.(null)}
                            disabled={disabled}
                          className="h-9"
                        />
                      </div>
                      <div className="w-32 space-y-1">
                        <Label className="text-xs text-muted-foreground">$/Million</Label>
                        <Input
                          type="number"
                          step="0.01"
                          min="0"
                          placeholder="0.00"
                          value={entry.price_per_million}
                          onChange={(e) => {
                            const updated = [...rateCardEntries];
                            updated[idx].price_per_million = e.target.value;
                            onRateCardEntriesChange(updated);
                              onFieldChange?.();
                            }}
                            onFocus={() => onFocusAreaChange?.('config')}
                            onBlur={() => onFocusAreaChange?.(null)}
                            disabled={disabled}
                          className="h-9"
                        />
                      </div>
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        onClick={() => {
                          onRateCardEntriesChange(
                            rateCardEntries.filter((_, i) => i !== idx)
                          );
                            onFieldChange?.();
                          }}
                          disabled={disabled}
                        className="mt-6"
                      >
                        <Trash2 className="h-4 w-4 text-destructive" />
                      </Button>
                    </div>
                  ))}
                </div>
                <p className="text-xs text-muted-foreground">
                  Define custom rates per billing unit (e.g., input_tokens, output_tokens)
                </p>
              </div>
            </>
          ) : (
            <>
              {pricingConfig ? (
                <div className="bg-muted/50 p-3 rounded-md border space-y-2">
                  <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide">Custom Pricing</p>
                  {Array.isArray((pricingConfig as any)?.rate_card_entries) &&
                  (pricingConfig as any).rate_card_entries.length > 0 ? (
                    <div className="space-y-1.5">
                      {(pricingConfig as any).rate_card_entries.map((entry: any, idx: number) => (
                        <div key={idx} className="flex justify-between items-center text-sm py-1">
                          <code className="text-xs bg-background px-2 py-1 rounded">{entry.billing_unit}</code>
                          <span className="font-medium">${entry.price_per_million}/M</span>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="text-xs text-muted-foreground">No billing units configured</p>
                  )}
                </div>
              ) : (
                <div className="text-center p-3 bg-muted/30 rounded-md border border-dashed">
                  <p className="text-sm text-muted-foreground">Using system default rates</p>
                </div>
              )}
            </>
          )}
        </CollapsibleContent>
      </div>
    </Collapsible>
  );
}
