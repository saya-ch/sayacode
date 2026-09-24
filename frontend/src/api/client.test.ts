import { afterEach, expect, it, vi } from "vitest";
import { api, bootstrapAuth } from "./client";

afterEach(() => vi.unstubAllGlobals());

it("启动令牌交换后只向修改请求携带 CSRF，且删除地址中的令牌", async () => {
  const replaceState = vi.fn();
  vi.stubGlobal("window", {
    location: { hash: "#token=local-launch-token", pathname: "/", search: "" },
  });
  vi.stubGlobal("history", { replaceState });
  const fetchMock = vi.fn(async (url: string, init: RequestInit) => {
    if (url === "/api/auth")
      return { ok: true, status: 200, json: async () => ({ csrf_token: "csrf-one" }) };
    if (url === "/api/status")
      return { ok: true, status: 200, json: async () => ({ model: "test" }) };
    if (url === "/api/threads/thread-1/runs") {
      expect(new Headers(init.headers).get("X-CSRF-Token")).toBe("csrf-one");
      expect(init.credentials).toBe("same-origin");
      return {
        ok: true,
        status: 202,
        json: async () => ({ run_id: "run-1", thread_id: "thread-1", status: "running" }),
      };
    }
    throw new Error(`Unexpected URL: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  await bootstrapAuth();
  const receipt = await api.startRun("thread-1", "检查仓库");
  expect(receipt.run_id).toBe("run-1");
  expect(replaceState).toHaveBeenCalledWith(null, "", "/");
  expect(JSON.parse(fetchMock.mock.calls[0]![1].body as string)).toEqual({
    token: "local-launch-token",
  });
});
