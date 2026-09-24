import { expect, it, vi } from "vitest";
import { recoverPreloadFailure } from "./preloadRecovery";

it("懒加载旧哈希丢失时仅自动重载一次，再失败显示恢复说明", () => {
  const values = new Map<string, string>();
  const storage = {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value);
    },
  };
  const reload = vi.fn();
  const failure = vi.fn();
  const first = new Event("vite:preloadError", { cancelable: true });
  expect(recoverPreloadFailure(first, storage, reload, failure, 100_000)).toBe("reload");
  expect(first.defaultPrevented).toBe(true);
  const second = new Event("vite:preloadError", { cancelable: true });
  expect(recoverPreloadFailure(second, storage, reload, failure, 100_500)).toBe("failure");
  expect(second.defaultPrevented).toBe(true);
  expect(reload).toHaveBeenCalledTimes(1);
  expect(failure).toHaveBeenCalledTimes(1);
});

it("浏览器拒绝会话存储时仍阻止白屏并展示恢复说明", () => {
  const event = new Event("vite:preloadError", { cancelable: true });
  const failure = vi.fn();
  expect(recoverPreloadFailure(event, null, vi.fn(), failure)).toBe("failure");
  expect(event.defaultPrevented).toBe(true);
  expect(failure).toHaveBeenCalledOnce();
});
