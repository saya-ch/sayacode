import { expect, it } from "vitest";
import { finishModelTest, startModelTest } from "./modelTest";

it("切换测试对象时清除上一模型结果，并忽略迟到的旧响应", () => {
  const first = finishModelTest(startModelTest("model-a"), "model-a", "A passed");
  const second = startModelTest("model-b");
  expect(second.result).toBeNull();
  expect(finishModelTest(second, "model-a", "A late response")).toEqual(second);
  expect(finishModelTest(second, "model-b", "B failed").result).toBe("B failed");
  expect(first.result).toBe("A passed");
});
