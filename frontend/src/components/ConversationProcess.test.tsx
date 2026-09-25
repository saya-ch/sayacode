import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import type { TimelineProcessRow } from "../lib/conversationTimeline";
import { ConversationProcess } from "./ConversationProcess";

it("长时间工具调用在对话内显示运行状态，结算后原位变为摘要", () => {
  const row: TimelineProcessRow = {
    kind: "process",
    key: "process:tool:call-1",
    active: true,
    awaitingEvent: false,
    items: [
      {
        key: "tool:call-1",
        kind: "tool",
        callId: "call-1",
        status: "running",
        message: null,
        activity: {
          id: "started",
          type: "tool.started",
          kind: "tool",
          status: "running",
          at: new Date(Date.now() - 5000).toISOString(),
          endedAt: null,
          name: "execute_command_tool",
          summary: null,
          durationMs: null,
          data: { tool_call_id: "call-1", tool_input: { command: "pytest -q" } },
        },
      },
    ],
  };
  const running = renderToStaticMarkup(
    <ConversationProcess
      row={row}
      activeRun={{
        run_id: "run-1",
        started_at: new Date(Date.now() - 5000).toISOString(),
        status: "running",
      }}
    />,
  );
  expect(running).toContain('aria-busy="true"');
  expect(running).toContain("正在执行工具");
  expect(running).toContain("pytest -q");
  expect(running).not.toContain("当前运行活动");

  const stopping = renderToStaticMarkup(
    <ConversationProcess
      row={row}
      runStatus="stopping"
      activeRun={{
        run_id: "run-1",
        started_at: new Date(Date.now() - 5000).toISOString(),
        status: "running",
      }}
    />,
  );
  expect(stopping).toContain("正在停止本轮运行");

  const completed = renderToStaticMarkup(
    <ConversationProcess
      row={{
        ...row,
        active: false,
        items: [{ ...row.items[0]!, status: "completed" }],
      }}
    />,
  );
  expect(completed).toContain('aria-busy="false"');
  expect(completed).toContain("执行了 1 项操作");
});
