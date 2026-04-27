import { ExternalLink } from 'lucide-react';
import { Button } from '@/components/ui/button';

interface GoogleDriveConnectProps {
  catalogId: string;
}

export function GoogleDriveConnect({ catalogId }: GoogleDriveConnectProps) {
  const handleConnect = () => {
    // Redirect to backend OAuth endpoint — it handles the Google consent flow
    const connectUrl = `/api/v1/catalogs/connect?catalog_id=${encodeURIComponent(catalogId)}`;
    window.location.href = connectUrl;
  };

  return (
    <Button onClick={handleConnect}>
      <ExternalLink className="mr-2 h-4 w-4" />
      Connect Google Drive
    </Button>
  );
}
