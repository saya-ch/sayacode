import { Component, type ReactNode } from "react";
import { resolveLanguage, translate, type Language } from "../i18n";

export function RecoveryScreen({ language }: { language: Language }) {
  const t = (key: string) => translate(language, key);
  return (
    <main style={{ maxWidth: 480, margin: "18vh auto", padding: 24 }} role="alert">
      <h1>{t("界面资源加载失败")}</h1>
      <p>{t("页面已尝试自动刷新。请手动刷新或重新启动 SAYACODE。")}</p>
      <button onClick={() => window.location.reload()}>{t("重新加载页面")}</button>
    </main>
  );
}

export class AppErrorBoundary extends Component<
  { children: ReactNode; languageSetting?: string | null },
  { failed: boolean }
> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    if (this.state.failed) {
      return (
        <RecoveryScreen
          language={resolveLanguage(this.props.languageSetting, navigator.language)}
        />
      );
    }
    return this.props.children;
  }
}
