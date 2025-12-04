import { createContext, useContext, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { getCurrentUserApiV1AuthMeGetOptions } from '../api/generated/@tanstack/react-query.gen';

interface User {
  id: string;
  email: string;
  name?: string;
  [key: string]: unknown;
}

interface AuthContextType {
  user: User | null;
  isLoading: boolean;
  isAuthenticated: boolean;
  error: Error | null;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const { data, isLoading, error } = useQuery({
    ...getCurrentUserApiV1AuthMeGetOptions(),
    retry: false,
  });

  const user = data as User | null;
  const isAuthenticated = !!user && !error;

  return (
    <AuthContext.Provider
      value={{
        user: isAuthenticated ? user : null,
        isLoading,
        isAuthenticated,
        error: error as Error | null,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextType {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
}
