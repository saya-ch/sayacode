// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import { api } from "../api/client";
import { ModelsPanel } from "./models";

it("全局模型连接页明确表示新会话默认，切换默认不会修改已有线程", async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  let active = "original";
  vi.spyOn(api, "models").mockImplementation(async () => ({
    active_profile: active,
    profiles: ["original", "next"].map((name) => ({
      name,
      protocol: "anthropic_messages",
      model_id: name,
      base_url: "https://example.test",
      context_length: 128000,
      max_output_tokens: 4096,
      has_api_key: true,
    })),
  }));
  vi.spyOn(api, "useModel").mockImplementation(async (name) => {
    active = name;
    return { active_profile: name };
  });
  vi.spyOn(api, "setThreadModel");
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(<ModelsPanel />));
    expect(container.textContent).toContain("新会话默认");
    expect(container.textContent).not.toContain("设为当前模型");
    const select = [...container.querySelectorAll<HTMLButtonElement>("button")].find(
      (item) => !item.disabled && item.textContent?.trim() === "设为默认模型",
    );
    await act(async () => select?.click());
    expect(api.useModel).toHaveBeenCalledWith("next");
    expect(api.setThreadModel).not.toHaveBeenCalled();
  } finally {
    await act(async () => root.unmount());
    container.remove();
    vi.restoreAllMocks();
  }
});
