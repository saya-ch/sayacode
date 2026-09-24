import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { expect, it } from "vitest";
import { hasEnglish, resolveLanguage, translate } from "./i18n";

function sourceFiles(root: string): string[] {
  return readdirSync(root, { withFileTypes: true }).flatMap((item) => {
    const path = join(root, item.name);
    if (item.isDirectory()) return sourceFiles(path);
    return /\.tsx?$/.test(path) && !/\.test\.tsx?$/.test(path) ? [path] : [];
  });
}

it("auto 依据浏览器语言解析，显式设置立即覆盖", () => {
  expect(resolveLanguage("auto", "zh-CN")).toBe("zh");
  expect(resolveLanguage("auto", "en-US")).toBe("en");
  expect(resolveLanguage("en", "zh-CN")).toBe("en");
  expect(translate("en", "{count} 个子 Agent 运行中", { count: 3 })).toBe("3 child agents running");
});

it("所有 t() 字面键都有英文翻译", () => {
  const missing = sourceFiles(join(process.cwd(), "src")).flatMap((path) => {
    if (path.endsWith("i18n.tsx")) return [];
    const text = readFileSync(path, "utf8");
    return [...text.matchAll(/\bt\(\s*"([^"]+)"/g)]
      .map((match) => match[1]!)
      .filter((key) => !hasEnglish(key))
      .map((key) => `${path}: ${key}`);
  });
  expect(missing).toEqual([]);
});
