import * as Dialog from "@radix-ui/react-dialog";
import {
  BrainCircuit,
  Cable,
  HeartPulse,
  LibraryBig,
  History,
  ScanSearch,
  Settings2,
  SlidersHorizontal,
  X,
} from "lucide-react";
import { ModelsPanel } from "./models";
import { McpPanel } from "./mcp";
import { SkillsPanel } from "./skills";
import { MemoryPanel } from "./memory";
import { DiagnosticsPanel } from "./diagnostics";
import { ReviewerPanel } from "./reviewer";
import { SessionPanel } from "./session";
import { useI18n } from "../i18n";
import { useState } from "react";
import shared from "../styles/shared.module.css";
import styles from "./ProductDialog.module.css";

type ProductTab = "models" | "mcp" | "skills" | "memory" | "diagnostics" | "reviewer" | "session";

interface ProductDialogProps {
  open: boolean;
  onClose: () => void;
  workspaceId: string | null;
  threadId: string | null;
  onChanged?: () => Promise<void>;
}

export function ProductDialog({
  open,
  onClose,
  workspaceId,
  threadId,
  onChanged,
}: ProductDialogProps) {
  const { t } = useI18n();
  const [tab, setTab] = useState<ProductTab>("models");
  const tabs: { id: ProductTab; label: string; icon: React.ReactNode }[] = [
    { id: "models", label: "模型", icon: <SlidersHorizontal size={17} /> },
    { id: "mcp", label: "MCP", icon: <Cable size={17} /> },
    { id: "skills", label: "Skill", icon: <LibraryBig size={17} /> },
    { id: "memory", label: "记忆", icon: <BrainCircuit size={17} /> },
    { id: "diagnostics", label: "诊断", icon: <ScanSearch size={17} /> },
    { id: "reviewer", label: "Jev 审理", icon: <HeartPulse size={17} /> },
    { id: "session", label: "会话与运行", icon: <History size={17} /> },
  ];
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
              <Dialog.Title>{t("SAYACODE 设置与扩展")}</Dialog.Title>
              <Dialog.Description id="product-description">
                {t("配置模型、扩展和记忆，检查当前工作区状态。")}
              </Dialog.Description>
            </div>
            <Dialog.Close asChild>
              <button className={shared.iconButton} aria-label={t("关闭设置窗口")}>
                <X size={19} />
              </button>
            </Dialog.Close>
          </div>
          <div className={styles.body}>
            <nav className={styles.nav} aria-label={t("设置分类")}>
              {tabs.map((item) => (
                <button
                  key={item.id}
                  className={tab === item.id ? styles.activeNav : ""}
                  onClick={() => setTab(item.id)}
                  aria-current={tab === item.id ? "page" : undefined}
                >
                  {item.icon}
                  <span>{t(item.label)}</span>
                </button>
              ))}
            </nav>
            <div className={styles.panel}>
              {tab === "models" && <ModelsPanel />}
              {tab === "mcp" && <McpPanel workspaceId={workspaceId} />}
              {tab === "skills" && <SkillsPanel workspaceId={workspaceId} threadId={threadId} />}
              {tab === "memory" && <MemoryPanel workspaceId={workspaceId} />}
              {tab === "diagnostics" && <DiagnosticsPanel workspaceId={workspaceId} />}
              {tab === "reviewer" && <ReviewerPanel />}
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
