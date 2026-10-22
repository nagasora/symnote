import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "./client";

export function useApi<T>(path: string | null, fallback: T) {
  const [data, setData] = useState<T>(fallback);
  const [loading, setLoading] = useState(Boolean(path));
  const [error, setError] = useState("");
  const refresh = useCallback(async () => {
    if (!path) return fallback;
    setLoading(true);
    setError("");
    try {
      const value = await apiFetch<T>(path);
      setData(value);
      return value;
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "読み込めませんでした",
      );
      return fallback;
    } finally {
      setLoading(false);
    }
  }, [path]);
  useEffect(() => {
    void refresh();
  }, [refresh]);
  return { data, setData, loading, error, refresh };
}
