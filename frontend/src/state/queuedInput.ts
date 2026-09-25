/** 入队成功即结束提交；快照同步作为独立的界面更新继续运行。 */
export async function submitQueuedInput(
  threadId: string,
  message: string,
  attachmentIds: string[],
  messageId: string,
  actions: {
    enqueue: (
      owner: string,
      text: string,
      attachments: string[],
      identity: string,
    ) => Promise<unknown>;
    refresh: (owner: string) => Promise<void>;
    onRefreshError: (reason: unknown) => void;
  },
): Promise<void> {
  await actions.enqueue(threadId, message, attachmentIds, messageId);
  void actions.refresh(threadId).catch(actions.onRefreshError);
}
