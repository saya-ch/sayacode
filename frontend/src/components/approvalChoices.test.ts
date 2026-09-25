import { expect, it } from "vitest";
import { approvalSubmission, canRememberApproval } from "./approvalChoices";

const approval = {
  checkpoint_id: "checkpoint-one",
  actions: [{ name: "execute_command_tool", args: { command: "git status" } }],
};

it("工作区自动档每次审批 Shell，不能保存精确调用授权", () => {
  expect(canRememberApproval("workspace_auto")).toBe(false);
  expect(approvalSubmission(approval, ["remember"], "", "workspace_auto")).toEqual({
    decisions: [{ type: "approve" }],
    grants: [],
  });
});

it("询问档仍可记住相同调用，拒绝原因正常提交", () => {
  expect(canRememberApproval("ask")).toBe(true);
  expect(approvalSubmission(approval, ["remember"], "", "ask").grants).toEqual([
    { index: 0, tool_name: "execute_command_tool" },
  ]);
  expect(approvalSubmission(approval, ["reject"], "不执行", "ask").decisions).toEqual([
    { type: "reject", message: "不执行" },
  ]);
});
