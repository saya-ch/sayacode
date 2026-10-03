/** 每种资源只接纳最后发起的请求，避免慢响应覆盖新状态。 */
export class RequestOrder {
  private readonly latest = new Map<string, number>();

  begin(key: string): number {
    const sequence = (this.latest.get(key) ?? 0) + 1;
    this.latest.set(key, sequence);
    return sequence;
  }

  isLatest(key: string, sequence: number): boolean {
    return this.latest.get(key) === sequence;
  }
}
