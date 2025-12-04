import { useAuth } from '../contexts/AuthContext';

export function WelcomePage() {
  const { user } = useAuth();

  return (
    <div>
      <h1>Welcome {user?.email}</h1>
    </div>
  );
}
