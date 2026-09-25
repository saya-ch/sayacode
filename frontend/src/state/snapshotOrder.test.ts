import { expect, it } from "vitest";
import { SnapshotRequestOrder } from "./snapshotOrder";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((finish) => {
    resolve = finish;
  });
  return { promise, resolve };
}

it("同一子线程的新快照先返回后，迟到的旧快照不能覆盖对话", async () => {
  const requests = new SnapshotRequestOrder();
  const oldResponse = deferred<string>();
  const newResponse = deferred<string>();
  let rendered = "初始消息";

  const load = async (response: Promise<string>) => {
    const sequence = requests.begin("child");
    const value = await response;
    if (requests.isLatest("child", sequence)) rendered = value;
  };
  const oldLoad = load(oldResponse.promise);
  const newLoad = load(newResponse.promise);

  newResponse.resolve("子 Agent 已完成的回复");
  await newLoad;
  oldResponse.resolve("旧的运行中快照");
  await oldLoad;

  expect(rendered).toBe("子 Agent 已完成的回复");
});

it("不同线程的快照请求互不失效", () => {
  const requests = new SnapshotRequestOrder();
  const parent = requests.begin("parent");
  requests.begin("child");
  expect(requests.isLatest("parent", parent)).toBe(true);
});

it("旧快照先返回但其他数据迟到时，提交前仍须重新检查顺序", async () => {
  const requests = new SnapshotRequestOrder();
  const otherData = deferred<void>();
  let rendered = "初始消息";

  const oldSequence = requests.begin("child");
  const oldLoad = Promise.all([Promise.resolve("旧快照"), otherData.promise]).then(([snapshot]) => {
    if (requests.isLatest("child", oldSequence)) rendered = snapshot;
  });
  await Promise.resolve();

  const newSequence = requests.begin("child");
  if (requests.isLatest("child", newSequence)) rendered = "最新回复";
  otherData.resolve();
  await oldLoad;

  expect(rendered).toBe("最新回复");
});
