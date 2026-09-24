import { useEffect, useId, useRef, useState } from "react";
import {
  ArrowLeft,
  ChevronDown,
  FilePlus2,
  LibraryBig,
  Minimize2,
  Plus,
  Settings2,
  X,
} from "lucide-react";
import { api } from "../api/client";
import type { Attachment, Skill } from "../api/types";
import type { WorkspaceState } from "../state/useWorkspace";
import { useI18n } from "../i18n";
import { useProduct } from "../products/useProduct";
import styles from "./ComposerActions.module.css";

type TrustLevel = "read_only" | "ask" | "jev" | "full";

export interface ComposerActionsProps {
  state: WorkspaceState;
  attachments: Attachment[];
  onAddFiles: (files: FileList | File[]) => Promise<void>;
  onRemoveAttachment: (id: string) => Promise<void>;
  onOpenSettings: () => void;
}

const trustLevels: { value: TrustLevel; label: string }[] = [
  { value: "read_only", label: "只读" },
  { value: "ask", label: "询问" },
  { value: "jev", label: "Jev 自动审理" },
  { value: "full", label: "完全信任" },
];

const textFileTypes =
  "text/*,.md,.txt,.json,.jsonl,.yaml,.yml,.toml,.py,.js,.jsx,.ts,.tsx,.css,.html,.xml,.csv,.log,.sql,.sh,.ps1,.java,.go,.rs,.c,.cpp,.h,.hpp";

export function ComposerActions({
  state,
  attachments,
  onAddFiles,
  onRemoveAttachment,
  onOpenSettings,
}: ComposerActionsProps) {
  const { t } = useI18n();
  const menuId = useId();
  const modelId = useId();
  const trustId = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [view, setView] = useState<"actions" | "skills">("actions");
  const [skills, setSkills] = useState<Skill[]>([]);
  const [skillsLoading, setSkillsLoading] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [removingId, setRemovingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const catalog = useProduct(
    "composer-models:" + (state.settings?.active_profile ?? ""),
    api.models,
  );
  const snapshot = state.snapshot?.thread_id === state.threadId ? state.snapshot : null;
  const effectiveModel = snapshot?.effective_model ?? null;
  const override = snapshot?.profile_override_name ?? "";
  const modelSource =
    snapshot?.model_source === "thread"
      ? t("此线程指定")
      : snapshot?.model_source === "task"
        ? t("子任务派发时继承")
        : t("全局默认");
  const profileExists = catalog.data?.profiles.some((item) => item.name === override);
  const canThreadAction =
    Boolean(snapshot && state.threadId) &&
    Boolean(effectiveModel) &&
    !state.busy &&
    !snapshot?.active_run &&
    !snapshot?.pending_approval &&
    !snapshot?.queued_messages?.length &&
    !["running", "stopping", "pending", "paused", "stopped"].includes(snapshot?.status ?? "");

  useEffect(() => {
    setOpen(false);
    setView("actions");
    setSkills([]);
    setError(null);
  }, [state.workspaceId, state.threadId]);
  useEffect(() => {
    if (!open) return;
    const closeOnOutside = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!menu.current?.contains(target) && !trigger.current?.contains(target)) {
        setOpen(false);
      }
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        setOpen(false);
        trigger.current?.focus();
      }
    };
    document.addEventListener("pointerdown", closeOnOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);
  useEffect(() => {
    if (open)
      requestAnimationFrame(() =>
        menu.current?.querySelector<HTMLElement>("button:not([disabled])")?.focus(),
      );
  }, [open, view, skillsLoading]);

  const closeMenu = () => {
    setOpen(false);
    setView("actions");
    trigger.current?.focus();
  };
  const run = async (work: () => Promise<void>) => {
    setActionBusy(true);
    setError(null);
    try {
      await work();
      closeMenu();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t("操作失败"));
    } finally {
      setActionBusy(false);
    }
  };
  const openSkills = async () => {
    if (!state.workspaceId) return;
    setView("skills");
    setSkills([]);
    setSkillsLoading(true);
    setError(null);
    try {
      setSkills(await api.skills(state.workspaceId));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t("读取失败"));
    } finally {
      setSkillsLoading(false);
    }
  };
  const chooseFiles = (files: FileList | null) => {
    if (!files?.length) return;
    const chosen = Array.from(files);
    if (fileInput.current) fileInput.current.value = "";
    setOpen(false);
    void run(() => onAddFiles(chosen));
  };

  return (
    <div className={styles.root}>
      {attachments.length > 0 && (
        <ul className={styles.attachments} aria-label={t("待发送附件")}>
          {attachments.map((item) => (
            <li key={item.id}>
              <span title={item.name}>{item.name}</span>
              <button
                type="button"
                aria-label={t("移除附件") + ": " + item.name}
                title={t("移除附件")}
                disabled={Boolean(removingId) || state.busy}
                onClick={() => {
                  setRemovingId(item.id);
                  setError(null);
                  void onRemoveAttachment(item.id)
                    .catch((reason: unknown) =>
                      setError(reason instanceof Error ? reason.message : t("操作失败")),
                    )
                    .finally(() => setRemovingId(null));
                }}
              >
                <X size={13} aria-hidden="true" />
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className={styles.controls}>
        <div className={styles.addWrap}>
          <button
            ref={trigger}
            type="button"
            className={styles.addButton}
            aria-label={t("添加操作")}
            title={t("添加操作")}
            aria-expanded={open}
            aria-haspopup="dialog"
            aria-controls={menuId}
            disabled={!state.threadId || actionBusy}
            onClick={() => {
              setError(null);
              setOpen((value) => !value);
              setView("actions");
            }}
          >
            <Plus size={17} aria-hidden="true" />
          </button>
          <input
            ref={fileInput}
            className={styles.fileInput}
            type="file"
            multiple
            accept={textFileTypes}
            tabIndex={-1}
            aria-label={t("添加文件")}
            onChange={(event) => chooseFiles(event.currentTarget.files)}
          />
          {open && (
            <div
              id={menuId}
              ref={menu}
              className={styles.menu}
              role="dialog"
              aria-label={t("快捷操作")}
            >
              {view === "actions" ? (
                <>
                  <strong className={styles.menuHeading}>{t("快捷操作")}</strong>
                  <button
                    type="button"
                    onClick={() => fileInput.current?.click()}
                    disabled={actionBusy}
                  >
                    <FilePlus2 size={16} aria-hidden="true" />
                    <span>
                      <b>{t("添加文件")}</b>
                      <small>{t("附在下一条消息中")}</small>
                    </span>
                  </button>
                  <button
                    type="button"
                    disabled={!canThreadAction || actionBusy}
                    title={!canThreadAction ? t("请在当前线程空闲时压缩上下文") : undefined}
                    onClick={() => void run(() => state.compactCurrent())}
                  >
                    <Minimize2 size={16} aria-hidden="true" />
                    <span>
                      <b>{t("压缩上下文")}</b>
                      <small>{t("整理较早消息为摘要")}</small>
                    </span>
                  </button>
                  <button
                    type="button"
                    disabled={!state.workspaceId || !canThreadAction || actionBusy}
                    onClick={() => void openSkills()}
                  >
                    <LibraryBig size={16} aria-hidden="true" />
                    <span>
                      <b>{t("选择 Skill")}</b>
                      <small>{t("为当前线程激活 Skill")}</small>
                    </span>
                  </button>
                </>
              ) : (
                <>
                  <button type="button" className={styles.back} onClick={() => setView("actions")}>
                    <ArrowLeft size={15} aria-hidden="true" /> {t("返回")}
                  </button>
                  <strong className={styles.menuHeading}>{t("选择 Skill")}</strong>
                  {skillsLoading ? (
                    <p className={styles.empty}>{t("正在读取 Skills…")}</p>
                  ) : error ? (
                    <p className={styles.error} role="alert">
                      {error}
                    </p>
                  ) : skills.length ? (
                    <div className={styles.skillList}>
                      {skills.map((skill) => (
                        <button
                          type="button"
                          key={skill.source + ":" + skill.name}
                          disabled={actionBusy || state.busy}
                          onClick={() => void run(() => state.activateSkill(skill.name))}
                        >
                          <span>
                            <b>{skill.name}</b>
                            <small>{skill.description || skill.source}</small>
                          </span>
                        </button>
                      ))}
                    </div>
                  ) : (
                    <p className={styles.empty}>{t("当前工作区没有可用 Skill。")}</p>
                  )}
                </>
              )}
              {view === "actions" && error && (
                <p className={styles.error} role="alert">
                  {error}
                </p>
              )}
            </div>
          )}
        </div>
        <div className={styles.selectWrap}>
          <label htmlFor={modelId}>{t("模型")}</label>
          <div className={styles.selectFrame}>
            <select
              id={modelId}
              value={override}
              aria-label={t("当前线程模型")}
              title={t("当前生效") + ": " + (effectiveModel || t("未配置")) + " · " + modelSource}
              disabled={!snapshot || state.busy || actionBusy || (catalog.loading && !catalog.data)}
              onFocus={() => void catalog.refresh()}
              onChange={(event) => {
                void state.setThreadModel(event.target.value || null).catch(() => {});
              }}
            >
              <option value="">{t("继承") + ": " + (effectiveModel || t("未配置"))}</option>
              {override && !profileExists && (
                <option value={override}>
                  {override} · {t("配置已移除")}
                </option>
              )}
              {catalog.data?.profiles.map((item) => (
                <option key={item.name} value={item.name}>
                  {item.name}
                </option>
              ))}
            </select>
            <ChevronDown size={12} aria-hidden="true" />
          </div>
          <small title={t("模型更改在下一次运行时生效")}>
            {modelSource} · {t("下次运行生效")}
          </small>
        </div>
        <div className={styles.selectWrap}>
          <label htmlFor={trustId}>{t("信任")}</label>
          <div className={styles.selectFrame}>
            <select
              id={trustId}
              value={snapshot?.trust_level ?? ""}
              aria-label={t("当前线程信任档")}
              disabled={!snapshot || state.busy || actionBusy}
              onChange={(event) => {
                void state.setTrust(event.target.value as TrustLevel).catch(() => {});
              }}
            >
              {!snapshot && <option value="">{t("正在读取")}</option>}
              {trustLevels.map((level) => (
                <option key={level.value} value={level.value}>
                  {t(level.label)}
                </option>
              ))}
            </select>
            <ChevronDown size={12} aria-hidden="true" />
          </div>
          <small>
            {t("当前线程")} · {t("后续操作生效")}
          </small>
        </div>
        <button
          type="button"
          className={styles.settings}
          aria-label={t("打开设置")}
          title={t("打开设置")}
          onClick={() => {
            setOpen(false);
            onOpenSettings();
          }}
        >
          <Settings2 size={16} aria-hidden="true" />
        </button>
      </div>
      {(catalog.error || (!open && error)) && (
        <p className={styles.error} role="alert">
          {catalog.error || error}
        </p>
      )}
    </div>
  );
}
