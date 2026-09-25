import * as Dialog from "@radix-ui/react-dialog";
import {
  BrainCircuit,
  Cable,
  LibraryBig,
  History,
  ScanSearch,
  Settings2,
  SlidersHorizontal,
  ShieldCheck,
  X,
} from "lucide-react";
import { ModelsPanel } from "./models";
import { McpPanel } from "./mcp";
import { SkillsPanel } from "./skills";
import { MemoryPanel } from "./memory";
import { DiagnosticsPanel } from "./diagnostics";
import { SessionPanel } from "./session";
import { GlobalPreferences, ThreadPreferences } from "./SettingsPreferences";
import type { WorkspaceState } from "../state/useWorkspace";
import { useI18n } from "../i18n";
import { displaySessionTitle } from "../lib/format";
import { useEffect, useRef, useState } from "react";
import shared from "../styles/shared.module.css";
import styles from "./ProductDialog.module.css";

type ProductTab =
  "global" | "models" | "mcp" | "memory" | "diagnostics" | "thread" | "skills" | "session";

const groups: {
  scope: string;
  tabs: { id: ProductTab; label: string; icon: React.ReactNode }[];
}[] = [
  {
    scope: "全局",
    tabs: [
      { id: "global", label: "默认与界面", icon: <Settings2 size={16} /> },
      { id: "models", label: "模型连接", icon: <SlidersHorizontal size={16} /> },
    ],
  },
  {
    scope: "工作区",
    tabs: [
      { id: "mcp", label: "MCP", icon: <Cable size={16} /> },
      { id: "memory", label: "跨会话记忆", icon: <BrainCircuit size={16} /> },
      { id: "diagnostics", label: "项目诊断", icon: <ScanSearch size={16} /> },
    ],
  },
  {
    scope: "当前线程",
    tabs: [
      { id: "thread", label: "模型与信任", icon: <ShieldCheck size={16} /> },
      { id: "skills", label: "Skill", icon: <LibraryBig size={16} /> },
      { id: "session", label: "运行与历史", icon: <History size={16} /> },
    ],
  },
];

const scopeNotes: Partial<Record<ProductTab, string>> = {
  models: "全局默认模型用于新会话的初始选择；已有线程可单独切换。",
  mcp: "这里同时列出用户级和项目级服务器；条目上的作用域决定可用范围。",
  memory: "用户记忆跨工作区可用，项目记忆只在当前工作区生效。",
  diagnostics: "诊断读取当前工作区，不更改其他工作区。",
  skills: "Skill 从当前工作区发现；激活只作用于当前选中的线程。",
  session: "检查点、批准授权和会话记忆属于当前线程；Hook 属于当前工作区。",
};

interface ProductDialogProps {
  open: boolean;
  onClose: () => void;
  state: WorkspaceState;
  onChanged?: () => Promise<void>;
}

export function ProductDialog({ open, onClose, onChanged, state }: ProductDialogProps) {
  const { t } = useI18n();
  const { workspaceId, threadId } = state;
  const [tab, setTab] = useState<ProductTab>(threadId ? "thread" : "global");
  const panelRef = useRef<HTMLDivElement>(null);
  const workspace = state.workspaces.find((item) => item.id === workspaceId);
  const selectedTask = state.tasks.find((item) => item.thread_id === threadId);
  const threadName =
    selectedTask?.title ||
    (state.snapshot?.thread_id === threadId
      ? displaySessionTitle(state.snapshot.title, t)
      : null) ||
    state.sessions.find((item) => item.id === threadId)?.title ||
    "SAYA";
  useEffect(() => {
    panelRef.current?.scrollTo({ top: 0 });
  }, [tab]);
  const selectTab = (next: ProductTab) => {
    if (tab === "models" && next !== "models" && onChanged) {
      void onChanged().catch(() => {});
    }
    setTab(next);
  };
  return (
    <Dialog.Root
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className={styles.overlay} />
        <Dialog.Content className={styles.content} aria-describedby="product-description">
          <div className={styles.top}>
            <div className={styles.topIcon}>
              <Settings2 size={20} />
            </div>
            <div>
              <Dialog.Title>{t("设置")}</Dialog.Title>
              <Dialog.Description id="product-description">
                {t("全局默认、工作区资源与当前线程设置集中在这里。")}
              </Dialog.Description>
            </div>
            <Dialog.Close asChild>
              <button className={shared.iconButton} aria-label={t("关闭设置窗口")}>
                <X size={19} />
              </button>
            </Dialog.Close>
          </div>
          <div className={styles.contextBar}>
            <span title={workspace?.name}>
              {t("工作区")} <strong>{workspace?.name || t("未选择")}</strong>
            </span>
            <span title={threadId ? threadName : undefined}>
              {t("当前线程")} <strong>{threadId ? threadName : t("未选择")}</strong>
            </span>
          </div>
          <div className={styles.body}>
            <nav className={styles.nav} aria-label={t("设置分类")}>
              {groups.map((group) => (
                <div className={styles.navGroup} key={group.scope}>
                  <div className={styles.navScope}>{t(group.scope)}</div>
                  {group.tabs.map((item) => (
                    <button
                      type="button"
                      key={item.id}
                      className={tab === item.id ? styles.activeNav : ""}
                      onClick={() => selectTab(item.id)}
                      aria-current={tab === item.id ? "page" : undefined}
                    >
                      {item.icon}
                      <span>{t(item.label)}</span>
                    </button>
                  ))}
                </div>
              ))}
            </nav>
            <div className={styles.panel} ref={panelRef}>
              {scopeNotes[tab] && <p className={styles.scopeNote}>{t(scopeNotes[tab])}</p>}
              {tab === "global" && (
                <GlobalPreferences state={state} onOpenModels={() => setTab("models")} />
              )}
              {tab === "models" && <ModelsPanel />}
              {tab === "mcp" && <McpPanel workspaceId={workspaceId} />}
              {tab === "skills" && <SkillsPanel workspaceId={workspaceId} threadId={threadId} />}
              {tab === "memory" && <MemoryPanel workspaceId={workspaceId} />}
              {tab === "diagnostics" && <DiagnosticsPanel workspaceId={workspaceId} />}
              {tab === "thread" && (
                <ThreadPreferences state={state} onOpenModels={() => setTab("models")} />
              )}
              {tab === "session" && (
                <SessionPanel workspaceId={workspaceId} threadId={threadId} onChanged={onChanged} />
              )}
            </div>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
