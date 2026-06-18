import { useMemo } from 'react';
import { AlertCircle, Clock } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { CRON_PRESETS, describeCron } from '@/lib/cron';

interface CronFieldProps {
  id?: string;
  value: string;
  onChange: (value: string) => void;
}

/**
 * Cron expression input with preset quick-picks, live validation, and a
 * human-readable description of the schedule.
 */
export function CronField({ id = 'cron', value, onChange }: CronFieldProps) {
  const result = useMemo(() => describeCron(value), [value]);
  const isBlank = value.trim() === '';
  const showError = !isBlank && !result.ok;

  return (
    <div className="grid gap-1.5">
      <Label htmlFor={id}>
        Cron expression{' '}
        <span className="text-muted-foreground text-xs">(e.g. 0 9 * * 1-5)</span>
      </Label>

      <div className="flex flex-wrap gap-1.5">
        {CRON_PRESETS.map((p) => (
          <Button
            key={p.expr}
            type="button"
            variant={value.trim() === p.expr ? 'secondary' : 'outline'}
            size="sm"
            className="h-7 px-2 text-xs"
            onClick={() => onChange(p.expr)}
          >
            {p.label}
          </Button>
        ))}
      </div>

      <Input
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="0 9 * * 1-5"
        className={showError ? 'border-destructive focus-visible:ring-destructive' : undefined}
        aria-invalid={showError}
      />

      {result.ok && (
        <p className="flex items-center gap-1.5 text-xs text-green-600 dark:text-green-500">
          <Clock className="h-3 w-3 shrink-0" />
          {result.text}
        </p>
      )}
      {showError && (
        <p className="flex items-center gap-1.5 text-xs text-destructive">
          <AlertCircle className="h-3 w-3 shrink-0" />
          {result.text}
        </p>
      )}
    </div>
  );
}
