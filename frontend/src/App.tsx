import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import type { StatusResponse } from "./api/types";
import { useWorkspace, type WorkspaceState } from "./state/useWorkspace";
import { I18nProvider, useI18n } from "./i18n";
import { AppErrorBoundary } from "./components/RecoveryScreen";
import { ApprovalReviewDialog } from "./components/ApprovalDialog";
import { Sidebar } from "./components/Sidebar";
import { Conversation } from "./components/Conversation";
import { Inspector } from "./components/Inspector";
import shared from "./styles/shared.module.css";
import styles from "./App.module.css";

const ProductDialog = lazy(() =>
  import("./products/ProductDialog").then((module) => ({ default: module.ProductDialog })),
);

export function App({ status }: { status: StatusResponse }) {
  const state = useWorkspace(status);
  return (
    <I18nProvider setting={state.settings?.language}>
      <AppErrorBoundary languageSetting={state.settings?.language}>
        <AppLayout state={state} />
      </AppErrorBoundary>
    </I18nProvider>
  );
}

function AppLayout({ state }: { state: WorkspaceState }) {
  const { t } = useI18n();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [approvalOpen, setApprovalOpen] = useState(false);
  const [productsOpen, setProductsOpen] = useState(false);
  const [viewportWidth, setViewportWidth] = useState(window.innerWidth);
  const sidebarSlot = useRef<HTMLDivElement>(null);
  const inspectorSlot = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!sidebarOpen && !inspectorOpen) return;
    const target = sidebarOpen ? sidebarSlot.current : inspectorSlot.current;
    const focusFrame = requestAnimationFrame(() =>
      target?.querySelector<HTMLElement>("button, input, [tabindex='0']")?.focus(),
    );
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setSidebarOpen(false);
        setInspectorOpen(false);
        const control = sidebarOpen ? "open-sidebar" : "open-inspector";
        requestAnimationFrame(() =>
          document.querySelector<HTMLElement>(`[data-control="${control}"]`)?.focus(),
        );
      }
      if (event.key === "Tab" && target) {
        const focusable = [
          ...target.querySelectorAll<HTMLElement>(
            "button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex='0']",
          ),
        ];
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first?.focus();
        }
      }
    };
    window.addEventListener("keydown", escape);
    return () => {
      cancelAnimationFrame(focusFrame);
      window.removeEventListener("keydown", escape);
    };
  }, [sidebarOpen, inspectorOpen]);
  useEffect(() => {
    const sync = () => {
      setViewportWidth(window.innerWidth);
      if (window.innerWidth > 900) setSidebarOpen(false);
      if (window.innerWidth > 1180) setInspectorOpen(false);
    };
    window.addEventListener("resize", sync);
    return () => window.removeEventListener("resize", sync);
  }, []);
  const closeSidebar = () => {
    setSidebarOpen(false);
    requestAnimationFrame(() =>
      document.querySelector<HTMLElement>('[data-control="open-sidebar"]')?.focus(),
    );
  };
  const closeInspector = () => {
    setInspectorOpen(false);
    requestAnimationFrame(() =>
      document.querySelector<HTMLElement>('[data-control="open-inspector"]')?.focus(),
    );
  };
  const showSidebar = viewportWidth > 900 || sidebarOpen;
  const showInspector = viewportWidth > 1180 || inspectorOpen;
  return (
    <div className={styles.shell}>
      {(sidebarOpen || inspectorOpen) && (
        <button
          className={styles.scrim}
          onClick={sidebarOpen ? closeSidebar : closeInspector}
          aria-label={t("关闭侧边栏")}
        />
      )}
      <div
        ref={sidebarSlot}
        role={sidebarOpen ? "dialog" : undefined}
        aria-label={sidebarOpen ? t("工作区与会话") : undefined}
        aria-modal={sidebarOpen ? true : undefined}
        className={`${styles.sidebarSlot} ${sidebarOpen ? styles.sidebarOpen : ""}`}
      >
        {showSidebar && <Sidebar state={state} onClose={closeSidebar} />}
      </div>
      <div className={styles.mainSlot} inert={sidebarOpen || inspectorOpen}>
        <Conversation
          state={state}
          onOpenSidebar={() => {
            setInspectorOpen(false);
            setSidebarOpen(true);
          }}
          onOpenInspector={() => {
            setSidebarOpen(false);
            setInspectorOpen(true);
          }}
          onOpenApproval={() => setApprovalOpen(true)}
          onOpenProducts={() => setProductsOpen(true)}
        />
      </div>
      <div
        ref={inspectorSlot}
        role={inspectorOpen ? "dialog" : undefined}
        aria-label={inspectorOpen ? t("任务检查器") : undefined}
        aria-modal={inspectorOpen ? true : undefined}
        className={`${styles.inspectorSlot} ${inspectorOpen ? styles.inspectorOpen : ""}`}
      >
        {showInspector && (
          <Inspector
            state={state}
            onClose={closeInspector}
            onOpenApproval={() => setApprovalOpen(true)}
          />
        )}
      </div>
      {state.error && (
        <div className={styles.error} role="alert">
          <strong>{t("操作未完成")}</strong>
          <span>{state.error}</span>
          <button
            className={shared.iconButton}
            onClick={state.clearError}
            aria-label={t("关闭错误提示")}
          >
            <X size={15} />
          </button>
        </div>
      )}
      <ApprovalReviewDialog
        open={approvalOpen}
        approval={state.snapshot?.pending_approval}
        trustLevel={state.snapshot?.trust_level}
        busy={state.busy}
        onClose={() => setApprovalOpen(false)}
        onSubmit={state.approve}
      />
      {productsOpen && (
        <Suspense fallback={null}>
          <ProductDialog
            open
            onClose={() => {
              setProductsOpen(false);
              void state.refresh().catch(() => {});
            }}
            state={state}
            onChanged={state.refresh}
          />
        </Suspense>
      )}
    </div>
  );
}
