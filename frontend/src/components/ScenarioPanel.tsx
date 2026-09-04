/** Demo controls. Replay provider only.
 *
 * Renders only when /health reports provider === "replay", so it cannot
 * appear in front of real market data. This is what the live demo is driven
 * from: one button per scenario, nothing to remember under pressure.
 */
import { useQuery, useQueryClient, useMutation } from '@tanstack/react-query';
import { api } from '../api/client';
import { digestKey } from '../hooks/useDigest';

export function ScenarioPanel({ provider }: { provider: string | undefined }) {
  const queryClient = useQueryClient();
  const enabled = provider === 'replay';

  const { data: scenarios } = useQuery({
    queryKey: ['scenarios'],
    queryFn: () => api.scenarios(),
    enabled,
  });

  const run = useMutation({
    mutationFn: (key: string) => api.runScenario(key),
    onSuccess: () => {
      // The clock jumped; everything on screen is about a different moment.
      void queryClient.invalidateQueries({ queryKey: digestKey });
    },
  });

  if (!enabled || !scenarios?.length) return null;

  return (
    <aside
      aria-label="Demo scenarios"
      style={{
        position: 'fixed',
        right: '1rem',
        bottom: '1rem',
        maxWidth: 220,
        padding: '0.75rem',
        background: 'var(--paper)',
        border: '1px solid var(--rule)',
        borderRadius: 4,
        zIndex: 10,
      }}
    >
      <p className="muted small" style={{ margin: '0 0 0.5rem' }}>
        Replay scenarios
      </p>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '0.25rem' }}>
        {scenarios.map((scenario) => (
          <button
            key={scenario.key}
            onClick={() => run.mutate(scenario.key)}
            disabled={run.isPending}
            title={scenario.description}
            className="small"
            style={{ textAlign: 'left', padding: '0.25rem 0', color: 'var(--unseen)' }}
          >
            {scenario.title}
          </button>
        ))}
      </div>
      {run.isPending && (
        <p className="muted small" style={{ margin: '0.5rem 0 0' }}>
          Seeking...
        </p>
      )}
    </aside>
  );
}
