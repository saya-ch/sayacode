import { Fragment, useState } from "react";
import {
  ChevronDown,
  CirclePlus,
  FolderClosed,
  PanelLeftClose,
  Pencil,
  Search,
  SquarePen,
  Terminal,
} from "lucide-react";
import type { WorkspaceState } from "../state/useWorkspace";
import { displaySessionTitle, relativeTime, statusLabel } from "../lib/format";
import shared from "../styles/shared.module.css";
import styles from "./Sidebar.module.css";
import { useI18n } from "../i18n";

interface SidebarProps {
  state: WorkspaceState;
  onClose: () => void;
}

function RenameForm({
  value,
  onChange,
  onSave,
  onCancel,
  busy,
  label,
}: {
  value: string;
  onChange: (value: string) => void;
  onSave: () => void;
  onCancel: () => void;
  busy: boolean;
  label: string;
}) {
  const { t } = useI18n();
  return (
    <form
      className={styles.renameForm}
      onSubmit={(event) => {
        event.preventDefault();
        onSave();
      }}
    >
      <label className={shared.label}>
        {label}
        <input
          className={shared.field}
          autoFocus
          value={value}
          onChange={(event) => onChange(event.target.value)}
          maxLength={160}
          required
        />
      </label>
      <div className={styles.formActions}>
        <button
          className={`${shared.ghostButton} ${shared.small}`}
          type="button"
          onClick={onCancel}
        >
          {t("取消")}
        </button>
        <button className={`${shared.button} ${shared.small}`} disabled={busy || !value.trim()}>
          {t("保存")}
        </button>
      </div>
    </form>
  );
}

export function Sidebar({ state, onClose }: SidebarProps) {
  const { t, language } = useI18n();
  const [search, setSearch] = useState("");
  const [adding, setAdding] = useState(false);
  const [path, setPath] = useState("");
  const [name, setName] = useState("");
  const [renaming, setRenaming] = useState<{
    kind: "workspace" | "session";
    id: string;
    value: string;
  } | null>(null);
  const filteredWorkspaces = state.workspaces.filter((item) =>
    `${item.name} ${item.path}`.toLocaleLowerCase().includes(search.toLocaleLowerCase()),
  );
  const filteredSessions = state.sessions.filter((item) =>
    item.title.toLocaleLowerCase().includes(search.toLocaleLowerCase()),
  );

  return (
    <aside className={styles.sidebar} aria-label={t("工作区与会话")}>
      <div className={styles.brandRow}>
        <div className={styles.mark} aria-hidden="true">
          <Terminal size={17} strokeWidth={2.5} />
        </div>
        <div className={styles.brandText}>
          <strong>SAYACODE</strong>
          <span>LOCAL AGENT WORKSPACE</span>
        </div>
        <button
          className={`${shared.iconButton} ${styles.mobileClose}`}
          onClick={onClose}
          aria-label={t("关闭工作区列表")}
        >
          <PanelLeftClose size={18} />
        </button>
      </div>

      <div className={styles.searchWrap}>
        <Search size={15} aria-hidden="true" />
        <input
          aria-label={t("搜索工作区和当前会话")}
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder={t("搜索工作区与会话")}
        />
      </div>

      <div className={styles.sectionHead}>
        <span>{t("工作区")}</span>
        <button
          className={shared.iconButton}
          onClick={() => setAdding((value) => !value)}
          aria-label={t("添加工作区")}
          title={t("添加工作区")}
        >
          <CirclePlus size={17} />
        </button>
      </div>
      {adding && (
        <form
          className={styles.addForm}
          onSubmit={(event) => {
            event.preventDefault();
            if (!path.trim()) return;
            void state
              .addWorkspace(path.trim(), name.trim() || undefined)
              .then(() => {
                setAdding(false);
                setPath("");
                setName("");
              })
              .catch(() => {});
          }}
        >
          <label className={shared.label} htmlFor="workspace-path">
            {t("本机目录路径")}
          </label>
          <input
            id="workspace-path"
            className={shared.field}
            value={path}
            onChange={(event) => setPath(event.target.value)}
            placeholder="C:\\projects\\example"
            required
          />
          <label className={shared.label} htmlFor="workspace-name">
            {t("显示名称（可选）")}
          </label>
          <input
            id="workspace-name"
            className={shared.field}
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder={t("项目名称")}
          />
          <div className={styles.formActions}>
            <button
              type="button"
              className={`${shared.ghostButton} ${shared.small}`}
              onClick={() => setAdding(false)}
            >
              {t("取消")}
            </button>
            <button
              type="submit"
              className={`${shared.button} ${shared.small}`}
              disabled={state.busy}
            >
              {t("添加")}
            </button>
          </div>
        </form>
      )}
      <div className={styles.workspaceList}>
        {filteredWorkspaces.map((workspace) => (
          <Fragment key={workspace.id}>
            <div className={styles.listRow}>
              <button
                className={`${styles.workspaceItem} ${workspace.id === state.workspaceId ? styles.activeWorkspace : ""}`}
                onClick={() => state.selectWorkspace(workspace.id)}
                aria-current={workspace.id === state.workspaceId ? "true" : undefined}
                title={workspace.path}
              >
                <FolderClosed size={16} aria-hidden="true" />
                <span className={styles.workspaceName}>
                  {workspace.name}
                  <small>{workspace.path}</small>
                </span>
                {workspace.id === state.workspaceId && <ChevronDown size={14} aria-hidden="true" />}
              </button>
              <button
                className={`${shared.iconButton} ${styles.renameButton}`}
                aria-label={t("重命名工作区")}
                title={t("重命名工作区")}
                onClick={() =>
                  setRenaming({ kind: "workspace", id: workspace.id, value: workspace.name })
                }
              >
                <Pencil size={14} />
              </button>
            </div>
            {renaming?.kind === "workspace" && renaming.id === workspace.id && (
              <RenameForm
                label={t("工作区名称")}
                value={renaming.value}
                onChange={(value) => setRenaming({ ...renaming, value })}
                onSave={() => {
                  void state
                    .renameWorkspace(workspace.id, renaming.value.trim())
                    .then(() => setRenaming(null))
                    .catch(() => {});
                }}
                onCancel={() => setRenaming(null)}
                busy={state.busy}
              />
            )}
          </Fragment>
        ))}
        {filteredWorkspaces.length === 0 && (
          <div className={styles.listEmpty}>
            {t(state.workspaces.length ? "没有匹配的工作区" : "尚未添加工作区")}
          </div>
        )}
      </div>

      <div className={styles.sectionHead}>
        <span>
          {t("当前工作区会话")} <em>{state.sessions.length}</em>
        </span>
        <button
          className={shared.iconButton}
          onClick={() => {
            void state.newSession().catch(() => {});
          }}
          disabled={!state.workspaceId || state.busy}
          aria-label={t("新建会话")}
          title={t("新建会话")}
        >
          <SquarePen size={17} />
        </button>
      </div>
      <nav className={styles.sessionList} aria-label={t("当前工作区会话")}>
        {filteredSessions.map((session) => (
          <Fragment key={session.id}>
            <div className={styles.listRow}>
              <button
                className={`${styles.sessionItem} ${session.id === state.sessionId ? styles.selectedSession : ""}`}
                onClick={() => state.selectSession(session.id)}
                aria-current={session.id === state.sessionId ? "page" : undefined}
              >
                <span
                  className={shared.statusDot}
                  data-status={session.status}
                  aria-hidden="true"
                />
                <span className={styles.sessionBody}>
                  <strong>{displaySessionTitle(session.title, t)}</strong>
                  <small>
                    {statusLabel(session.status, t)}
                    {session.updated_at
                      ? ` · ${relativeTime(session.updated_at, t, language)}`
                      : ""}
                  </small>
                </span>
              </button>
              <button
                className={`${shared.iconButton} ${styles.renameButton}`}
                aria-label={t("重命名会话")}
                title={t("重命名会话")}
                onClick={() =>
                  setRenaming({ kind: "session", id: session.id, value: session.title })
                }
              >
                <Pencil size={14} />
              </button>
            </div>
            {renaming?.kind === "session" && renaming.id === session.id && (
              <RenameForm
                label={t("会话标题")}
                value={renaming.value}
                onChange={(value) => setRenaming({ ...renaming, value })}
                onSave={() => {
                  void state
                    .renameSession(session.id, renaming.value.trim())
                    .then(() => setRenaming(null))
                    .catch(() => {});
                }}
                onCancel={() => setRenaming(null)}
                busy={state.busy}
              />
            )}
          </Fragment>
        ))}
        {filteredSessions.length === 0 && (
          <div className={styles.listEmpty}>
            {t(state.sessions.length ? "没有匹配的会话" : "还没有会话，可以新建一个。")}
          </div>
        )}
      </nav>

      <div className={styles.sidebarFooter}>
        <span
          className={shared.statusDot}
          data-status={state.connection === "connected" ? "running" : "paused"}
          aria-hidden="true"
        />
        <span>{t(state.connection === "connected" ? "已连接本机运行时" : "正在重连事件流")}</span>
        <span className={styles.localTag}>LOCAL</span>
      </div>
    </aside>
  );
}
