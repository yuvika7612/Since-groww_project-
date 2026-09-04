import { Link } from 'react-router-dom';
import { WatchlistManager } from '../components/WatchlistManager';

export function Manage() {
  return (
    <div className="shell">
      <nav style={{ marginBottom: '1.5rem' }}>
        <Link to="/" className="small">
          &larr; Digest
        </Link>
      </nav>
      <h1 className="serif" style={{ fontSize: '1.5rem', fontWeight: 500, margin: '0 0 1.5rem' }}>
        Your list
      </h1>
      <WatchlistManager />
    </div>
  );
}
