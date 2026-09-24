import { createRoot } from "react-dom/client";
import { bootstrapAuth } from "./api/client";
import { recoverPreloadFailure } from "./api/preloadRecovery";
import { App } from "./App";
import { resolveLanguage, translate } from "./i18n";
import { RecoveryScreen } from "./components/RecoveryScreen";
import "./styles/tokens.css";

const root = createRoot(document.getElementById("root")!);

window.addEventListener("vite:preloadError", (event) => {
  let storage: Storage | null = null;
  try {
    storage = window.sessionStorage;
  } catch {
    // 禁用存储时直接展示恢复屏，仍先阻止未处理的动态导入异常。
  }
  recoverPreloadFailure(
    event,
    storage,
    () => window.location.reload(),
    () => {
      const language = resolveLanguage("auto", navigator.language);
      root.render(<RecoveryScreen language={language} />);
    },
  );
});

void bootstrapAuth()
  .then((status) => root.render(<App status={status} />))
  .catch((error: unknown) => {
    const language = resolveLanguage("auto", navigator.language);
    const t = (key: string) => translate(language, key);
    const message = error instanceof Error ? error.message : t("无法连接本机服务。");
    root.render(
      <main style={{ maxWidth: 480, margin: "18vh auto", padding: 24 }} role="alert">
        <h1>{t("SAYACODE 无法启动界面")}</h1>
        <p>{message}</p>
        <p>{t("请从运行中的 sayacode 命令重新打开浏览器。")}</p>
      </main>,
    );
  });
