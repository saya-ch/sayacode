import { expect, it } from "vitest";
import type { StreamEvent, ThreadSnapshot } from "../api/types";
import { reconcileActivity } from "./activity";

const thread = (
  status: ThreadSnapshot["status"],
  activity: ThreadSnapshot["activity"],
): ThreadSnapshot => ({
  thread_id: "t",
  workspace_id: "w",
  title: "SAYA",
  status,
  messages: [],
  todos: [],
  tasks: [],
  activity,
});
const event = (type: string): StreamEvent => ({
  instance_id: "i",
  seq: 1,
  workspace_id: "w",
  thread_id: "t",
  type,
  data: {},
});

it("运行中保留切入时审计历史和当前实时事件，终态交给审计去重", () => {
  const previous = thread("running", [{ id: "older", type: "model.completed" }]);
  const live = [
    event("model.started"),
    event("tool.completed"),
    event("run.completed"),
    event("approval.requested"),
  ];
  const running = reconcileActivity(
    previous,
    thread("running", [
      { id: "older", type: "model.completed" },
      { id: "duplicate", type: "model.started" },
    ]),
    live,
  );
  expect(running.snapshot.activity?.map((row) => row.id)).toEqual(["older"]);
  expect(running.live).toEqual(live);
  const complete = reconcileActivity(
    running.snapshot,
    thread("completed", [
      { id: "older", type: "model.completed" },
      { id: "settled", type: "tool.completed" },
    ]),
    running.live,
  );
  expect(complete.snapshot.activity?.map((row) => row.id)).toEqual(["older", "settled"]);
  expect(complete.live.map((row) => row.type)).toEqual(["approval.requested"]);
});

it("缓冲缺口强制交接快照，再接收新实时事件", () => {
  const result = reconcileActivity(
    thread("running", []),
    thread("running", [{ id: "recovered", type: "tool.completed" }]),
    [event("tool.completed")],
    true,
  );
  expect(result.snapshot.activity?.[0]?.id).toBe("recovered");
  expect(result.live).toEqual([]);
});
