import { expect, it } from "vitest";
import { hasEnglish, translate } from "../i18n";
import { trustLevels } from "./trust";

it("输入框和设置页共用的档位具有完整双语说明", () => {
  expect(new Set(trustLevels.map((level) => level.value)).size).toBe(trustLevels.length);
  for (const level of trustLevels) {
    expect(hasEnglish(level.label)).toBe(true);
    expect(hasEnglish(level.detail)).toBe(true);
  }
  const workspaceAuto = trustLevels.find((level) => level.value === "workspace_auto");
  expect(workspaceAuto).toBeDefined();
  expect(workspaceAuto!.detail).toContain("删除");
  expect(workspaceAuto!.detail).toContain("Shell 每次询问");
  expect(translate("en", workspaceAuto!.detail)).toContain("network");
});
