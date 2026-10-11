import type { ThreadSnapshot } from "../api/types";

/** 激活、解除与手动压缩都只能在当前线程没有待完成运行时修改检查点。 */
export function canEditThreadContext(state: {
  threadId: string | null;
  snapshot: ThreadSnapshot | null;
  busy: boolean;
}): boolean {
  const snapshot = state.snapshot;
  return Boolean(
    state.threadId &&
    snapshot?.thread_id === state.threadId &&
    snapshot.effective_model &&
    !state.busy &&
    !snapshot.active_run &&
    !snapshot.pending_steps &&
    !snapshot.pending_approval &&
    !snapshot.queued_messages?.length &&
    !["running", "stopping", "pending", "paused", "stopped", "interrupted"].includes(
      snapshot.status,
    ),
  );
}
