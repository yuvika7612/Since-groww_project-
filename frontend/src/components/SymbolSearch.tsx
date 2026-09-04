/** Debounced symbol search, keyboard navigable. */
import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';

export function SymbolSearch({ onPick }: { onPick: (symbol: string) => void }) {
  const [term, setTerm] = useState('');
  const [debounced, setDebounced] = useState('');
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(term.trim()), 200);
    return () => window.clearTimeout(timer);
  }, [term]);

  const { data: results = [] } = useQuery({
    queryKey: ['symbol-search', debounced],
    queryFn: () => api.searchSymbols(debounced),
    enabled: debounced.length > 0,
  });

  const choose = (symbol: string) => {
    onPick(symbol);
    setTerm('');
    setDebounced('');
    inputRef.current?.focus();
  };

  return (
    <div>
      <label htmlFor="symbol-search" className="muted small">
        Add a symbol
      </label>
      <input
        id="symbol-search"
        ref={inputRef}
        value={term}
        onChange={(event) => {
          setTerm(event.target.value);
          setActive(0);
        }}
        onKeyDown={(event) => {
          if (results.length === 0) return;
          if (event.key === 'ArrowDown') {
            event.preventDefault();
            setActive((i) => Math.min(i + 1, results.length - 1));
          } else if (event.key === 'ArrowUp') {
            event.preventDefault();
            setActive((i) => Math.max(i - 1, 0));
          } else if (event.key === 'Enter') {
            event.preventDefault();
            choose(results[active].symbol);
          } else if (event.key === 'Escape') {
            setTerm('');
          }
        }}
        placeholder="RELIANCE, INFY..."
        autoComplete="off"
        role="combobox"
        aria-expanded={results.length > 0}
        aria-controls="symbol-results"
        style={{ display: 'block', width: '100%', marginTop: '0.375rem' }}
      />

      {results.length > 0 && (
        <ul
          id="symbol-results"
          role="listbox"
          style={{
            listStyle: 'none',
            margin: '0.375rem 0 0',
            padding: 0,
            border: '1px solid var(--rule)',
            borderRadius: 3,
          }}
        >
          {results.map((result, index) => (
            <li key={result.symbol} role="option" aria-selected={index === active}>
              <button
                onClick={() => choose(result.symbol)}
                onMouseEnter={() => setActive(index)}
                style={{
                  display: 'flex',
                  width: '100%',
                  gap: '0.625rem',
                  padding: '0.5rem 0.625rem',
                  textAlign: 'left',
                  background: index === active ? 'var(--rule)' : 'transparent',
                }}
              >
                <span style={{ fontWeight: 600 }}>{result.symbol}</span>
                <span className="muted small">{result.name}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
