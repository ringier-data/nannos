import { AlertTriangle, X } from 'lucide-react';
import { useAuth } from '@/contexts/AuthContext';
import { Button } from '@/components/ui/button';
import { Alert, AlertDescription } from '@/components/ui/alert';

/**
 * Banner displayed when an admin is impersonating another user.
 * Shows the impersonated user and provides a button to stop impersonation.
 */
export function ImpersonationBanner() {
  const { isImpersonating, user, stopImpersonation } = useAuth();

  if (!isImpersonating || !user) {
    return null;
  }

  const handleStopImpersonation = async () => {
    try {
      await stopImpersonation();
    } catch (error) {
      console.error('Failed to stop impersonation:', error);
      // Toast notification could be added here
    }
  };

  return (
    <Alert className="rounded-none border-x-0 border-t-0 bg-yellow-50 border-yellow-200">
      <AlertTriangle className="h-4 w-4 text-yellow-600" />
      <AlertDescription className="flex items-center justify-between ml-2">
        <span className="text-yellow-800">
          <strong>Impersonating:</strong> {user.email} ({user.id})
        </span>
        <Button
          variant="outline"
          size="sm"
          onClick={handleStopImpersonation}
          className="bg-white hover:bg-yellow-50"
        >
          <X className="h-4 w-4 mr-1" />
          Stop Impersonating
        </Button>
      </AlertDescription>
    </Alert>
  );
}
