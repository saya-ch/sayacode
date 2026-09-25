import type { ApprovalDecision, ApprovalGrant, PendingApproval } from "../api/types";

export type ApprovalChoice = "once" | "remember" | "reject" | null;

export function canRememberApproval(trustLevel: string | null | undefined): boolean {
  return trustLevel === "ask";
}

export function approvalSubmission(
  approval: PendingApproval,
  choices: ApprovalChoice[],
  message: string,
  trustLevel: string | null | undefined,
): { decisions: ApprovalDecision[]; grants: ApprovalGrant[] } {
  if (choices.length !== approval.actions.length || choices.some((choice) => choice === null)) {
    throw new Error("每项操作都需要明确决定");
  }
  const decisions: ApprovalDecision[] = choices.map((choice) => ({
    type: choice === "reject" ? "reject" : "approve",
    ...(choice === "reject" && message.trim() ? { message: message.trim() } : {}),
  }));
  const grants = canRememberApproval(trustLevel)
    ? choices.flatMap((choice, index) =>
        choice === "remember" ? [{ index, tool_name: approval.actions[index]!.name }] : [],
      )
    : [];
  return { decisions, grants };
}
