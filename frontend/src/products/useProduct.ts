import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

interface ProductState<T> {
  key: string;
  data: T | null;
  loading: boolean;
  busy: boolean;
  error: string | null;
}

export function useProduct<T>(key: string, load: () => Promise<T>) {
  const loader = useRef(load);
  const activeResource = useRef<{ key: string } | null>(null);
  const requestVersion = useRef(0);
  const [state, setState] = useState<ProductState<T>>({
    key,
    data: null,
    loading: true,
    busy: false,
    error: null,
  });

  useLayoutEffect(() => {
    loader.current = load;
  });
  useLayoutEffect(() => {
    // 重访同一个 key 也会得到新身份，旧操作不能回写新页面。
    activeResource.current = { key };
    return () => {
      activeResource.current = null;
      requestVersion.current += 1;
    };
  }, [key]);

  const refresh = useCallback(async () => {
    const owner = activeResource.current;
    if (owner?.key !== key) return;
    const version = ++requestVersion.current;
    setState((current) => ({
      key,
      data: current.key === key ? current.data : null,
      loading: true,
      busy: current.key === key && current.busy,
      error: null,
    }));
    try {
      const data = await loader.current();
      if (activeResource.current === owner && requestVersion.current === version) {
        setState((current) => ({ ...current, key, data, loading: false, error: null }));
      }
    } catch (reason) {
      if (activeResource.current === owner && requestVersion.current === version) {
        setState((current) => ({
          ...current,
          key,
          loading: false,
          error: reason instanceof Error ? reason.message : "读取失败",
        }));
      }
    }
  }, [key]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const setError = (error: string | null) => {
    if (activeResource.current?.key !== key) return;
    setState((current) => (current.key === key ? { ...current, error } : current));
  };
  const run = async (work: () => Promise<unknown>) => {
    const owner = activeResource.current;
    if (owner?.key !== key) return;
    setState((current) => ({
      key,
      data: current.key === key ? current.data : null,
      loading: current.key === key ? current.loading : true,
      busy: true,
      error: null,
    }));
    try {
      const result = await work();
      if (activeResource.current === owner) await refresh();
      return result;
    } catch (reason) {
      if (activeResource.current === owner)
        setError(reason instanceof Error ? reason.message : "操作失败");
      throw reason;
    } finally {
      if (activeResource.current === owner)
        setState((current) => (current.key === key ? { ...current, busy: false } : current));
    }
  };
  // 新请求尚未开始时，先隐藏上一个 key 的结果。
  const visible =
    state.key === key ? state : { data: null, loading: true, busy: false, error: null };
  return { ...visible, setError, refresh, run };
}
