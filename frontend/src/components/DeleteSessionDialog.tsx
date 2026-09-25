import { useEffect, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { Trash2, X } from "lucide-react";
import { api } from "../api/client";
import type { Session, SessionDeletionPreview } from "../api/types";
import { useI18n } from "../i18n";
import { displaySessionTitle } from "../lib/format";
import shared from "../styles/shared.module.css";
import styles from "./Sidebar.module.css";

interface DeleteSessionDialogProps {
  session: Session;
  onDelete: (threadId: string) => Promise<void>;
  onClose: () => void;
}

export function DeleteSessionDialog({ session, onDelete, onClose }: DeleteSessionDialogProps) {
  const { t } = useI18n();
  const [preview, setPreview] = useState<SessionDeletionPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let current = true;
    void api.sessionDeletionPreview(session.id).then(
      (value) => current && setPreview(value),
      (reason: unknown) =>
        current && setError(reason instanceof Error ? reason.message : t("无法检查会话")),
    );
    return () => {
      current = false;
    };
  }, [session.id, t]);

  async function remove() {
    if (!preview?.allowed) return;
    setBusy(true);
    setError(null);
    try {
      await onDelete(session.id);
      onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t("删除会话失败"));
      setPreview(await api.sessionDeletionPreview(session.id).catch(() => null));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog.Root open onOpenChange={(open) => !open && !busy && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className={styles.dialogOverlay} />
        <Dialog.Content className={styles.deleteDialog}>
          <div className={styles.dialogHeader}>
            <div>
              <Dialog.Title>{t("删除会话")}</Dialog.Title>
              <Dialog.Description>{displaySessionTitle(session.title, t)}</Dialog.Description>
            </div>
            <Dialog.Close asChild>
              <button
                type="button"
                className={shared.iconButton}
                aria-label={t("关闭删除窗口")}
                disabled={busy}
              >
                <X size={18} />
              </button>
            </Dialog.Close>
          </div>
          {!preview && !error && <p className={styles.dialogHint}>{t("正在检查会话和子任务")}</p>}
          {preview && (
            <>
              <p className={styles.deleteDescription}>
                {t("删除会话会清除对话、检查点和所属子 Agent 记录，无法恢复。")}
              </p>
              <p className={styles.deleteDescription}>
                {t("工作区文件不会回退；本地审计记录和长期记忆会保留。")}
              </p>
              {preview.child_task_count > 0 && (
                <p className={styles.deleteDescription}>
                  {t("将一并删除的子 Agent 数量：")} {preview.child_task_count}
                </p>
              )}
              {preview.warnings.map((warning) => (
                <p className={styles.dialogHint} key={warning}>
                  {warning}
                </p>
              ))}
              {preview.blockers.length > 0 && (
                <div className={styles.deleteBlockers} role="status">
                  <strong>{t("暂时不能删除")}</strong>
                  <ul>
                    {preview.blockers.map((blocker) => (
                      <li key={blocker}>{blocker}</li>
                    ))}
                  </ul>
                </div>
              )}
            </>
          )}
          {error && (
            <p className={styles.dialogError} role="alert">
              {error}
            </p>
          )}
          <div className={styles.dialogActions}>
            <button type="button" className={shared.ghostButton} onClick={onClose} disabled={busy}>
              {t("取消")}
            </button>
            <button
              type="button"
              className={shared.dangerButton}
              disabled={busy || !preview?.allowed}
              onClick={() => void remove()}
            >
              <Trash2 size={15} /> {t("删除会话")}
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
