import { useState } from 'react';
import { BrowserRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { api, auth } from './api/client';
import { Digest } from './pages/Digest';
import { Manage } from './pages/Manage';
import { SymbolDetail } from './pages/SymbolDetail';

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: true } },
});

/** One field, one button. Real auth was out of scope; the interface is
 *  isolated behind a single dependency on the server, so swapping in OAuth
 *  touches one function there and this screen here. */
function DevLogin({ onDone }: { onDone: () => void }) {
  const [email, setEmail] = useState('demo@since.app');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const { token } = await api.devLogin(email);
      auth.set(token);
      onDone();
    } catch {
      setError('Could not sign in. Is the API running?');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="shell">
      <h1 className="serif" style={{ fontSize: '1.5rem', fontWeight: 500, margin: 0 }}>
        Since
      </h1>
      <p className="serif" style={{ fontSize: '1.75rem', lineHeight: 1.2, margin: '1.5rem 0 0', maxWidth: '18ch' }}>
        What changed while you were away.
      </p>

      <form onSubmit={submit} style={{ marginTop: '2rem', maxWidth: '22rem' }}>
        <label htmlFor="email" className="muted small">
          Email
        </label>
        <input
          id="email"
          type="email"
          required
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          style={{ display: 'block', width: '100%', margin: '0.375rem 0 0.75rem' }}
        />
        <button
          type="submit"
          disabled={busy}
          style={{
            border: '1px solid var(--ink)',
            borderRadius: 3,
            padding: '0.5rem 1rem',
            fontWeight: 500,
          }}
        >
          {busy ? 'Signing in...' : 'Continue'}
        </button>
        {error && (
          <p className="small" style={{ color: 'var(--caution)' }}>
            {error}
          </p>
        )}
        <p className="muted small" style={{ marginTop: '1rem' }}>
          This is a dev login &mdash; no password required.
        </p>
      </form>
    </div>
  );
}

export default function App() {
  const [token, setToken] = useState<string | null>(auth.get());

  if (!token) return <DevLogin onDone={() => setToken(auth.get())} />;

  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<Digest />} />
          <Route path="/manage" element={<Manage />} />
          <Route path="/symbol/:id" element={<SymbolDetail />} />
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
