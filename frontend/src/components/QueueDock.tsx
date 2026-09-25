import { useEffect, useId, useRef, useState } from "react";
import { Check, ChevronDown, ChevronUp, Pencil, Send, Trash2, X } from "lucide-react";
import { useI18n } from "../i18n";
import styles from "./QueueDock.module.css";

export interface QueueAttachment {
  name: string;
}

export interface QueueRow {
  message_id: string;
  text: string;
  status: "queued" | "pending";
  created_at?: string | null;
  attachments?: QueueAttachment[];
}

export interface QueueDockProps {
  rows: QueueRow[];
  busy?: boolean;
  canSteer?: boolean;
  onSteer: (messageId: string) => Promise<void>;
  onEdit: (messageId: string, text: string) => Promise<void>;
  onDelete: (messageId: string) => Promise<void>;
}

function displayTime(value: string | null | undefined, language: string): string | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? null
    : date.toLocaleTimeString(language === "en" ? "en-US" : "zh-CN", {
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      });
}

export function QueueDock({
  rows,
  busy = false,
  canSteer = true,
  onSteer,
  onEdit,
  onDelete,
}: QueueDockProps) {
  const { t, language } = useI18n();
  const listId = useId();
  const [expanded, setExpanded] = useState(false);
  const [editing, setEditing] = useState<{ id: string; text: string } | null>(null);
  const [workingId, setWorkingId] = useState<string | null>(null);
  const [error, setError] = useState<{ id: string; message: string } | null>(null);
  const editButtons = useRef(new Map<string, HTMLButtonElement>());
  const rowNodes = useRef(new Map<string, HTMLLIElement>());
  const working = useRef(false);
  const rowCount = rows.length;
  const visible = rowCount === 1 || expanded || editing !== null;
  const locked = busy || workingId !== null;

  useEffect(() => {
    if (rowCount === 0) setExpanded(false);
    if (editing && !rows.some((row) => row.message_id === editing.id && row.status === "queued")) {
      setEditing(null);
    }
  }, [editing, rowCount, rows]);

  if (rowCount === 0) return null;

  const focusEditButton = (id: string) => {
    requestAnimationFrame(() => editButtons.current.get(id)?.focus());
  };
  const cancelEdit = () => {
    if (!editing) return;
    const id = editing.id;
    setEditing(null);
    setError(null);
    focusEditButton(id);
  };
  const act = async (id: string, action: () => Promise<void>, fallback: string) => {
    if (busy || working.current) return false;
    working.current = true;
    setWorkingId(id);
    setError(null);
    try {
      await action();
      return true;
    } catch (reason) {
      setError({ id, message: reason instanceof Error ? reason.message : t(fallback) });
      return false;
    } finally {
      working.current = false;
      setWorkingId(null);
    }
  };
  const saveEdit = async () => {
    if (!editing || !editing.text.trim()) return;
    const { id, text } = editing;
    if (await act(id, () => onEdit(id, text), "编辑排队消息失败")) {
      setEditing(null);
      focusEditButton(id);
    }
  };

  return (
    <section className={styles.dock} aria-label={t("待发送消息")}>
      {rowCount > 1 && (
        <button
          type="button"
          className={styles.header}
          aria-controls={listId}
          aria-expanded={visible}
          onClick={() => setExpanded((value) => !value)}
          disabled={editing !== null}
        >
          <span className={styles.count}>{t("{count} 条待发送消息", { count: rowCount })}</span>
          <span className={styles.headerHint}>
            {rows.some((row) => row.status === "pending") && t("等待下一步骤")}
          </span>
          {visible ? (
            <ChevronDown size={15} aria-hidden="true" />
          ) : (
            <ChevronUp size={15} aria-hidden="true" />
          )}
        </button>
      )}
      <ul id={listId} className={styles.list} hidden={!visible}>
        {visible &&
          rows.map((row) => {
            const queued = row.status === "queued";
            const rowEditing = editing?.id === row.message_id;
            const time = displayTime(row.created_at, language);
            return (
              <li
                key={row.message_id}
                className={styles.row}
                data-status={row.status}
                tabIndex={-1}
                ref={(node) => {
                  if (node) rowNodes.current.set(row.message_id, node);
                  else rowNodes.current.delete(row.message_id);
                }}
              >
                <div className={styles.rowTop}>
                  <span className={styles.status} role="status">
                    {workingId === row.message_id
                      ? t("处理中…")
                      : t(queued ? "排队中" : "等待下一步骤")}
                  </span>
                  {time && <time dateTime={row.created_at || undefined}>{time}</time>}
                </div>
                {rowEditing ? (
                  <form
                    className={styles.editor}
                    onSubmit={(event) => {
                      event.preventDefault();
                      void saveEdit();
                    }}
                  >
                    <textarea
                      autoFocus
                      aria-label={t("编辑排队消息")}
                      rows={3}
                      value={editing.text}
                      disabled={locked}
                      onChange={(event) =>
                        setEditing({ id: row.message_id, text: event.target.value })
                      }
                      onKeyDown={(event) => {
                        if (event.key === "Escape") {
                          event.preventDefault();
                          cancelEdit();
                        }
                        if (
                          event.key === "Enter" &&
                          !event.shiftKey &&
                          !event.nativeEvent.isComposing
                        ) {
                          event.preventDefault();
                          void saveEdit();
                        }
                      }}
                    />
                    <div className={styles.editorActions}>
                      <button type="submit" disabled={locked || !editing.text.trim()}>
                        <Check size={14} aria-hidden="true" /> {t("保存")}
                      </button>
                      <button type="button" onClick={cancelEdit} disabled={locked}>
                        <X size={14} aria-hidden="true" /> {t("取消")}
                      </button>
                    </div>
                  </form>
                ) : (
                  <>
                    <p className={styles.preview} title={row.text}>
                      {row.text}
                    </p>
                    {row.attachments && row.attachments.length > 0 && (
                      <div className={styles.attachments} aria-label={t("附件")}>
                        {row.attachments.map((item, index) => (
                          <span key={item.name + ":" + index} title={item.name}>
                            {item.name}
                          </span>
                        ))}
                      </div>
                    )}
                    {queued && (
                      <div className={styles.actions}>
                        <button
                          type="button"
                          className={styles.send}
                          disabled={locked || !canSteer}
                          title={!canSteer ? t("请先处理审批或恢复线程") : undefined}
                          onClick={() => {
                            void act(
                              row.message_id,
                              () => onSteer(row.message_id),
                              "直接发送失败",
                            ).then((ok) => {
                              if (ok) {
                                requestAnimationFrame(() =>
                                  rowNodes.current.get(row.message_id)?.focus(),
                                );
                              }
                            });
                          }}
                        >
                          <Send size={14} aria-hidden="true" /> {t("直接发送")}
                        </button>
                        <button
                          type="button"
                          className={styles.iconAction}
                          aria-label={t("编辑排队消息")}
                          title={t("编辑排队消息")}
                          disabled={locked}
                          ref={(node) => {
                            if (node) editButtons.current.set(row.message_id, node);
                            else editButtons.current.delete(row.message_id);
                          }}
                          onClick={() => {
                            setError(null);
                            setEditing({ id: row.message_id, text: row.text });
                          }}
                        >
                          <Pencil size={14} aria-hidden="true" />
                        </button>
                        <button
                          type="button"
                          className={styles.iconAction}
                          aria-label={t("删除排队消息")}
                          title={t("删除排队消息")}
                          disabled={locked}
                          onClick={() => {
                            const nextId = rows.find(
                              (item) => item.message_id !== row.message_id,
                            )?.message_id;
                            void act(
                              row.message_id,
                              () => onDelete(row.message_id),
                              "删除排队消息失败",
                            ).then((ok) => {
                              if (ok && nextId) {
                                requestAnimationFrame(() => rowNodes.current.get(nextId)?.focus());
                              }
                            });
                          }}
                        >
                          <Trash2 size={14} aria-hidden="true" />
                        </button>
                      </div>
                    )}
                  </>
                )}
                {error?.id === row.message_id && (
                  <p className={styles.error} role="alert">
                    {error.message}
                  </p>
                )}
              </li>
            );
          })}
      </ul>
    </section>
  );
}
