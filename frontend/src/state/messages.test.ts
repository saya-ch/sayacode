import { expect, it } from "vitest";
import type { ThreadSnapshot } from "../api/types";
import { appendUserMessage } from "./messages";

it("跨标签即时用户事件与本地 POST 回执只保留同一条消息", () => {
  const snapshot: ThreadSnapshot = {
    thread_id: "thread-1",
    workspace_id: "w",
    title: "SAYA",
    status: "idle",
    messages: [],
    todos: [],
    tasks: [],
  };
  const first = appendUserMessage(snapshot, "thread-1", "run-1", "检查仓库");
  const second = appendUserMessage(first, "thread-1", "run-1", "检查仓库");
  expect(second?.messages).toHaveLength(1);
  expect(appendUserMessage(second, "thread-1", "run-2", "检查仓库")?.messages).toHaveLength(2);
});
