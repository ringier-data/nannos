import { useEffect } from 'react';
import { config } from '../config';

export function LoginRequiredPage() {
  useEffect(() => {
    window.location.href = `${config.apiBaseUrl}/api/v1/auth/login?redirectTo=${encodeURIComponent(window.location.href)}`;
  }, []);

  return null;
}
