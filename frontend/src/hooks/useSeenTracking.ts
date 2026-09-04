/** The read watermark, from the client side.
 *
 * A row counts as seen when it has been at least 50% visible for 800ms
 * continuously. The alternatives are both wrong in ways users would feel:
 *
 *   App-open overcounts. Opening and closing the app would silently clear
 *   every unread, which breaks the one promise the product makes.
 *
 *   Tap-to-detail undercounts. People read a list without tapping anything,
 *   so rows they have plainly read stay flagged and the digest fills up with
 *   things they already know.
 *
 * Getting this wrong is not a UI bug. It corrupts the watermark, and the
 * watermark is what "since you last checked" is measured against.
 */
import { useCallback, useEffect, useRef } from 'react';
import { api, beaconSeen } from '../api/client';

const VISIBLE_FRACTION = 0.5;
const DWELL_MS = 800;
const FLUSH_DEBOUNCE_MS = 2000;
const FLUSH_RETRY_MS = 5000;

export interface SeenEntry {
  symbol: string;
  seen_at: string;
  price: number | null;
}

interface Options {
  /** Called once per symbol the moment it is locally considered seen, so the
   *  row can stop advertising itself as unread without waiting for a round
   *  trip. Reconciled by the next digest fetch. */
  onSeen?: (symbol: string) => void;
  enabled?: boolean;
}

export function useSeenTracking({ onSeen, enabled = true }: Options = {}) {
  const buffer = useRef<Map<string, SeenEntry>>(new Map());
  const dwellTimers = useRef<Map<string, number>>(new Map());
  const flushTimer = useRef<number | null>(null);
  const observer = useRef<IntersectionObserver | null>(null);
  const meta = useRef<Map<Element, { symbol: string; price: number | null }>>(new Map());
  const locallySeen = useRef<Set<string>>(new Set());

  // onSeen is held in a ref rather than used as a dependency. Callers pass an
  // inline arrow, so depending on it gives markSeen a new identity every
  // render, which re-runs the effect below -- and that effect's cleanup
  // clears every in-flight dwell timer. Under a live stream the digest
  // re-renders on each event, so no row would ever survive its own 800ms and
  // nothing would be marked read while the market was moving.
  const onSeenRef = useRef(onSeen);
  onSeenRef.current = onSeen;

  // Lets flush re-arm itself after a failure without depending on
  // scheduleFlush, which is defined below it.
  const flushRef = useRef<() => void>(() => {});

  /** Send whatever is buffered. On failure the buffer is kept, not dropped:
   *  a lost flush means the user re-reads a row they have already read, and
   *  the monotonic upsert on the server makes a retry harmless. */
  const flush = useCallback(async () => {
    if (flushTimer.current !== null) {
      window.clearTimeout(flushTimer.current);
      flushTimer.current = null;
    }
    const entries = Array.from(buffer.current.values());
    if (entries.length === 0) return;

    // Cleared optimistically so rows seen during the request are not lost,
    // and restored below if the request fails.
    buffer.current.clear();
    try {
      await api.markSeen(entries);
    } catch {
      for (const entry of entries) {
        // Anything newer that arrived meanwhile wins.
        if (!buffer.current.has(entry.symbol)) buffer.current.set(entry.symbol, entry);
      }
      // Re-armed explicitly. Restoring the buffer is not enough on its own:
      // the only other things that flush are a new row being seen, the page
      // hiding, and unmount. A user who has stopped scrolling triggers none
      // of them, and their reads would sit unsent indefinitely.
      flushTimer.current = window.setTimeout(() => flushRef.current(), FLUSH_RETRY_MS);
    }
  }, []);

  flushRef.current = () => {
    void flush();
  };

  const scheduleFlush = useCallback(() => {
    if (flushTimer.current !== null) window.clearTimeout(flushTimer.current);
    flushTimer.current = window.setTimeout(flush, FLUSH_DEBOUNCE_MS);
  }, [flush]);

  const markSeen = useCallback(
    (symbol: string, price: number | null) => {
      buffer.current.set(symbol, {
        symbol,
        seen_at: new Date().toISOString(),
        price,
      });
      if (!locallySeen.current.has(symbol)) {
        locallySeen.current.add(symbol);
        onSeenRef.current?.(symbol);
      }
      scheduleFlush();
    },
    [scheduleFlush],
  );

  useEffect(() => {
    if (!enabled || typeof IntersectionObserver === 'undefined') return;

    observer.current = new IntersectionObserver(
      (entries) => {
        // Nothing is being read while the tab is in the background, so the
        // dwell timer must not run there.
        if (document.visibilityState !== 'visible') return;

        for (const entry of entries) {
          const info = meta.current.get(entry.target);
          if (!info) continue;

          const enough = entry.isIntersecting && entry.intersectionRatio >= VISIBLE_FRACTION;
          const existing = dwellTimers.current.get(info.symbol);

          if (enough) {
            if (existing !== undefined) continue; // already counting down
            const timer = window.setTimeout(() => {
              dwellTimers.current.delete(info.symbol);
              markSeen(info.symbol, info.price);
            }, DWELL_MS);
            dwellTimers.current.set(info.symbol, timer);
          } else if (existing !== undefined) {
            // Scrolled away before the dwell completed: it was not read.
            window.clearTimeout(existing);
            dwellTimers.current.delete(info.symbol);
          }
        }
      },
      { threshold: [VISIBLE_FRACTION] },
    );

    // Rows mount before effects run, so every ref callback that fired during
    // this commit found observer.current still null and quietly observed
    // nothing. Picking them up here is what makes the hook work at all --
    // without it the dwell timer never starts, no row is ever marked read,
    // and it fails silently while looking exactly like a working feature.
    for (const element of meta.current.keys()) observer.current.observe(element);

    const onVisibility = () => {
      if (document.visibilityState !== 'hidden') return;

      // Any row mid-dwell has not been read. Drop those timers rather than
      // letting them fire against a page nobody is looking at.
      for (const timer of dwellTimers.current.values()) window.clearTimeout(timer);
      dwellTimers.current.clear();

      const entries = Array.from(buffer.current.values());
      if (entries.length === 0) return;

      // sendBeacon, not fetch. A normal request is cancelled when a mobile
      // tab is backgrounded, and that is precisely the moment this write
      // matters most -- closing the app is when "what have I read" is being
      // recorded. Falls back to the ordinary flush if the browser refuses.
      if (beaconSeen(entries)) {
        buffer.current.clear();
      } else {
        void flush();
      }
    };

    const onUnload = () => {
      const entries = Array.from(buffer.current.values());
      if (entries.length > 0 && beaconSeen(entries)) buffer.current.clear();
    };

    document.addEventListener('visibilitychange', onVisibility);
    window.addEventListener('beforeunload', onUnload);

    return () => {
      document.removeEventListener('visibilitychange', onVisibility);
      window.removeEventListener('beforeunload', onUnload);
      for (const timer of dwellTimers.current.values()) window.clearTimeout(timer);
      dwellTimers.current.clear();
      observer.current?.disconnect();
      observer.current = null;
      // Unmounting is not a reason to lose reads.
      void flush();
    };
  }, [enabled, flush, markSeen]);

  /** Ref callback for a row element. */
  const track = useCallback(
    (symbol: string, price: number | null) => (element: HTMLElement | null) => {
      if (!element) return;
      meta.current.set(element, { symbol, price });
      observer.current?.observe(element);
    },
    [],
  );

  const hasSeen = useCallback((symbol: string) => locallySeen.current.has(symbol), []);

  return { track, hasSeen, flush };
}
