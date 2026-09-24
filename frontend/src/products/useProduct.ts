import { useCallback, useEffect, useRef, useState } from "react";

export function useProduct<T>(key: string, load: () => Promise<T>) {
  const loader = useRef(load);
  loader.current = load;
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setData(await loader.current());
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "读取失败");
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    setData(null);
    void refresh();
  }, [key, refresh]);
  const run = async (work: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      const result = await work();
      await refresh();
      return result;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "操作失败");
      throw reason;
    } finally {
      setBusy(false);
    }
  };
  return { data, loading, busy, error, setError, refresh, run };
}
