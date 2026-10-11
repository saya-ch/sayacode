import type { ThreadContextFeedback } from "../state/useWorkspace";
import { useI18n } from "../i18n";

export function ThreadContextStatus({
  feedback,
  className,
}: {
  feedback: ThreadContextFeedback | null;
  className?: string;
}) {
  const { t } = useI18n();
  if (!feedback) return null;
  const messages = {
    skill_activated: "已激活 Skill：{name}",
    skill_deactivated: "已解除 Skill：{name}",
    compacted: "上下文已压缩",
    compact_unchanged: "当前上下文无需压缩",
  };
  return (
    <p role="status" className={className}>
      {t(messages[feedback.action], { name: feedback.name ?? "" })}
    </p>
  );
}
