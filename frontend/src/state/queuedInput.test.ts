import { expect, it, vi } from "vitest";
import { submitQueuedInput } from "./queuedInput";

it("入队提交不等待慢快照，并始终刷新消息所属的子线程", async () => {
  let finishRefresh: (() => void) | undefined;
  const refreshPending = new Promise<void>((resolve) => {
    finishRefresh = resolve;
  });
  const enqueue = vi.fn(async () => ({ status: "queued" }));
  const refresh = vi.fn(() => refreshPending);
  const onRefreshError = vi.fn();

  await submitQueuedInput("task-one", "继续检查", [], "stable-id", {
    enqueue,
    refresh,
    onRefreshError,
  });

  expect(enqueue).toHaveBeenCalledWith("task-one", "继续检查", [], "stable-id");
  expect(refresh).toHaveBeenCalledWith("task-one");
  expect(onRefreshError).not.toHaveBeenCalled();
  finishRefresh?.();
  await refreshPending;
});

it("入队失败留给输入框重试，不能误认为消息已提交", async () => {
  const error = new Error("网络断开");
  const refresh = vi.fn(async () => {});
  await expect(
    submitQueuedInput("task-one", "重试内容", [], "stable-id", {
      enqueue: async () => {
        throw error;
      },
      refresh,
      onRefreshError: vi.fn(),
    }),
  ).rejects.toBe(error);
  expect(refresh).not.toHaveBeenCalled();
});
