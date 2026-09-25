import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import { QueueDock } from "./QueueDock";

it("子 Agent 停止时明确说明队列原因，并保留编辑和删除入口", () => {
  const html = renderToStaticMarkup(
    createElement(QueueDock, {
      rows: [{ message_id: "message-one", text: "补充检查", status: "queued" }],
      canSteer: false,
      blockedReason: "会话已暂停或停止，处理审批或恢复后才会发送。",
      onSteer: async () => {},
      onEdit: async () => {},
      onDelete: async () => {},
    }),
  );
  const buttons = html.match(/<button[^>]*>/g) ?? [];
  const direct = buttons.find((item) => item.includes("请先处理审批或恢复线程"));
  const edit = buttons.find((item) => item.includes('aria-label="编辑排队消息"'));
  const remove = buttons.find((item) => item.includes('aria-label="删除排队消息"'));

  expect(html).toContain("会话已暂停或停止，处理审批或恢复后才会发送。");
  expect(direct).toContain("disabled");
  expect(edit).not.toContain("disabled");
  expect(remove).not.toContain("disabled");
});
