import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import type { Session, Task } from "../api/types";
import type { WorkspaceState } from "../state/useWorkspace";
import { Inspector } from "./Inspector";

function task(id: string, parent: string, status: Task["status"]): Task {
  return {
    id,
    thread_id: id,
    parent_thread_id: parent,
    title: id,
    role: "builder",
    status,
    workspace_id: "workspace",
    created_at: null,
    updated_at: null,
  };
}

it("检查器只统计当前会话树，并在父快照加载前使用会话状态", () => {
  const state = {
    sessionId: "root-a",
    threadId: "root-a",
    parentSnapshot: null,
    snapshot: null,
    sessions: [{ id: "root-a", status: "running" } as Session],
    tasks: [
      task("a-running", "root-a", "running"),
      task("a-paused", "a-running", "paused"),
      task("b-running", "root-b", "running"),
    ],
    selectThread: () => {},
  } as unknown as WorkspaceState;

  const html = renderToStaticMarkup(
    createElement(Inspector, { state, onClose: () => {}, onOpenApproval: () => {} }),
  );

  expect(html).toContain("主 Agent · 运行中");
  expect(html).toContain("1 运行中 / 2 子任务");
  expect(html).not.toContain("b-running");
  expect(html).toMatch(/协作<\/span><b>2<\/b>/);
  expect(html).toMatch(/审批<\/span><b>1<\/b>/);
});
