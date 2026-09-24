import { expect, it } from "vitest";
import type { Task } from "../api/types";
import { agentColor, displaySessionTitle } from "./format";
import { descendantTasks } from "./tasks";

const task = (id: string, parent: string): Task => ({
  id,
  thread_id: id,
  parent_thread_id: parent,
  title: id,
  role: "builder",
  status: "completed",
  workspace_id: "w",
  created_at: null,
  updated_at: null,
});

it("只展示所选主会话的真实后代，颜色不随列表顺序变化", () => {
  const items = [task("a1", "root-a"), task("b1", "root-b"), task("a2", "a1")];
  expect(descendantTasks(items, "root-a").map((item) => item.id)).toEqual(["a1", "a2"]);
  expect(agentColor(items[0]!.thread_id)).toBe(agentColor([...items].reverse()[2]!.thread_id));
});

it("仅本地化默认会话标题，用户自定义标题原样展示", () => {
  const t = (key: string) => (key === "新会话" ? "New session" : key);
  expect(displaySessionTitle("New session", (key) => key)).toBe("新会话");
  expect(displaySessionTitle("New session", t)).toBe("New session");
  expect(displaySessionTitle("后端排查", t)).toBe("后端排查");
});
