import { useCallback, useEffect, useRef, useState } from "react";

export interface Polled<T> {
  data: T | undefined;
  error: string | null;
  loading: boolean;
  reload: () => void;
}

/** Poll an async function. Keeps the last good value on transient errors and exposes the error. */
export function usePoll<T>(fn: () => Promise<T>, intervalMs: number | null, deps: unknown[] = []): Polled<T> {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const tick = useRef(0);

  const load = useCallback(async () => {
    const mine = ++tick.current;
    try {
      const v = await fnRef.current();
      if (mine === tick.current) {
        setData(v);
        setError(null);
      }
    } catch (e) {
      if (mine === tick.current) setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (mine === tick.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    let alive = true;
    void load();
    if (intervalMs === null) return;
    const id = setInterval(() => {
      if (alive && !document.hidden) void load();
    }, intervalMs);
    return () => {
      alive = false;
      clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, load, ...deps]);

  return { data, error, loading, reload: () => void load() };
}

export function useHashRoute(): [string, (r: string) => void] {
  const read = () => window.location.hash.replace(/^#\/?/, "") || "overview";
  const [route, setRoute] = useState(read);
  useEffect(() => {
    const onHash = () => setRoute(read());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  return [route, (r: string) => (window.location.hash = `/${r}`)];
}

export function safeStorage(): { get: (k: string) => string | null; set: (k: string, v: string) => void } {
  return {
    get: (k) => {
      try {
        return window.localStorage.getItem(k);
      } catch {
        return null;
      }
    },
    set: (k, v) => {
      try {
        window.localStorage.setItem(k, v);
      } catch {
        /* storage unavailable (private mode): the preference just won't persist */
      }
    },
  };
}
