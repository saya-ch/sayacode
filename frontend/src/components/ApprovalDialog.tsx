import { useEffect, useState } from "react";
import * as AlertDialog from "@radix-ui/react-alert-dialog";
import { ShieldAlert, X } from "lucide-react";
import type { ApprovalDecision, ApprovalGrant, PendingApproval } from "../api/types";
import { readableJson } from "../lib/format";
import shared from "../styles/shared.module.css";
import styles from "./ApprovalDialog.module.css";
import { useI18n } from "../i18n";

interface ApprovalDialogProps {
  open: boolean;
  approval: PendingApproval | null | undefined;
  busy: boolean;
  onClose: () => void;
  onSubmit: (decisions: ApprovalDecision[], grants: ApprovalGrant[]) => Promise<void>;
}

export function ApprovalReviewDialog({
  open,
  approval,
  busy,
  onClose,
  onSubmit,
}: ApprovalDialogProps) {
  const { t } = useI18n();
  const [choices, setChoices] = useState<Array<"once" | "remember" | "reject" | null>>([]);
  const [message, setMessage] = useState("");
  useEffect(() => {
    setChoices(approval?.actions.map(() => null) ?? []);
    setMessage("");
  }, [approval?.checkpoint_id]);
  const setChoice = (index: number, value: "once" | "remember" | "reject") =>
    setChoices((old) => old.map((item, position) => (position === index ? value : item)));
  const ready =
    Boolean(approval?.actions.length) &&
    choices.length === approval?.actions.length &&
    choices.every(Boolean);
  const submit = () => {
    if (!ready) return;
    const decisions = choices.map((choice) => ({
      type: choice === "reject" ? ("reject" as const) : ("approve" as const),
      ...(choice === "reject" && message.trim() ? { message: message.trim() } : {}),
    }));
    const grants = choices.flatMap((choice, index) =>
      choice === "remember" && approval?.actions[index]
        ? [{ index, tool_name: approval.actions[index]!.name }]
        : [],
    );
    void onSubmit(decisions, grants)
      .then(onClose)
      .catch(() => {});
  };
  return (
    <AlertDialog.Root
      open={open && Boolean(approval)}
      onOpenChange={(next) => {
        if (!next && !busy) onClose();
      }}
    >
      <AlertDialog.Portal>
        <AlertDialog.Overlay className={styles.overlay} />
        <AlertDialog.Content className={styles.content}>
          <div className={styles.top}>
            <div className={styles.icon}>
              <ShieldAlert size={20} />
            </div>
            <div className={styles.topText}>
              <AlertDialog.Title>{t("审查待执行操作")}</AlertDialog.Title>
              <AlertDialog.Description>
                {t("每项操作都需要明确决定。批准后由原生检查点恢复执行。")}
              </AlertDialog.Description>
            </div>
            <AlertDialog.Cancel asChild>
              <button className={shared.iconButton} disabled={busy} aria-label={t("关闭审批窗口")}>
                <X size={18} />
              </button>
            </AlertDialog.Cancel>
          </div>
          <div className={styles.actions}>
            {approval?.actions.map((action, index) => (
              <section
                className={styles.action}
                key={`${approval.checkpoint_id}-${index}`}
                aria-label={t("操作 {number}", { number: index + 1 })}
              >
                <div className={styles.actionHeader}>
                  <span className={styles.ordinal}>
                    {index + 1}/{approval.actions.length}
                  </span>
                  <strong>{action.name}</strong>
                </div>
                <pre>{readableJson(action.args, 10000, t)}</pre>
                <fieldset className={styles.choices}>
                  <legend>{t("本项决定")}</legend>
                  <label className={choices[index] === "once" ? styles.chosen : ""}>
                    <input
                      type="radio"
                      name={`approval-${index}`}
                      checked={choices[index] === "once"}
                      onChange={() => setChoice(index, "once")}
                    />
                    {t("仅本次")}
                  </label>
                  <label className={choices[index] === "remember" ? styles.chosen : ""}>
                    <input
                      type="radio"
                      name={`approval-${index}`}
                      checked={choices[index] === "remember"}
                      onChange={() => setChoice(index, "remember")}
                    />
                    {t("本会话记住相同调用")}
                  </label>
                  <label className={choices[index] === "reject" ? styles.rejected : ""}>
                    <input
                      type="radio"
                      name={`approval-${index}`}
                      checked={choices[index] === "reject"}
                      onChange={() => setChoice(index, "reject")}
                    />
                    {t("拒绝执行")}
                  </label>
                </fieldset>
              </section>
            ))}
          </div>
          {choices.includes("reject") && (
            <div className={styles.reason}>
              <label className={shared.label} htmlFor="reject-reason">
                {t("拒绝原因（可选，会传给 Agent）")}
              </label>
              <textarea
                id="reject-reason"
                className={shared.textarea}
                value={message}
                onChange={(event) => setMessage(event.target.value)}
                rows={2}
                placeholder={t("说明拒绝的原因")}
              />
            </div>
          )}
          <div className={styles.footer}>
            <span>{t("记住授权只适用于本会话中完全相同的工具调用。")}</span>
            <AlertDialog.Cancel asChild>
              <button className={shared.secondaryButton} disabled={busy}>
                {t("稍后处理")}
              </button>
            </AlertDialog.Cancel>
            <button className={shared.button} disabled={!ready || busy} onClick={submit}>
              {t(busy ? "正在提交…" : "提交决定并继续")}
            </button>
          </div>
        </AlertDialog.Content>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  );
}
