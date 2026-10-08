import { Link, useLocation } from 'react-router';
import { useNannosPageContext } from '@nannos/embed-sdk';
import { Button } from '@/components/ui/button';

/**
 * An /app path no route matches. Rendered inside the layout rather than
 * redirected: a redirect would swap out the layout — and the docked assistant
 * with it — and the assistant, landing here after a navigate, would see /app
 * instead of learning that the address it chose does not exist.
 */
export function NotFoundPage() {
  const { pathname } = useLocation();
  useNannosPageContext({ title: 'Page not found — no console page has this address' });
  return (
    <div className="flex flex-col items-start gap-3 p-6">
      <h1 className="text-xl font-semibold">Page not found</h1>
      <p className="text-sm text-muted-foreground">
        There is no console page at <code className="font-mono">{pathname}</code>.
      </p>
      <Button asChild variant="outline" size="sm">
        <Link to="/app">Go to Settings</Link>
      </Button>
    </div>
  );
}
