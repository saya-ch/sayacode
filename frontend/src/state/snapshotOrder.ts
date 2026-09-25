/** 同一线程只接纳最后发起的快照请求，防止慢的旧响应覆盖新内容。 */
export class SnapshotRequestOrder {
  private readonly latest = new Map<string, number>();

  begin(threadId: string): number {
    const sequence = (this.latest.get(threadId) ?? 0) + 1;
    this.latest.set(threadId, sequence);
    return sequence;
  }

  isLatest(threadId: string, sequence: number): boolean {
    return this.latest.get(threadId) === sequence;
  }
}
