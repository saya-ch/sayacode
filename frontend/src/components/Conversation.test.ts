import { describe, expect, it } from "vitest";
import { validateTextAttachment } from "./Conversation";

describe("会话附件校验", () => {
  it("接收包含中文的 UTF-8 文本", async () => {
    const file = new File(["项目说明：你好\n"], "README.md", { type: "text/markdown" });
    expect(await validateTextAttachment(file)).toBe("ok");
  });

  it("在上传前拒绝二进制和无效 UTF-8", async () => {
    const image = new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], "logo.png", {
      type: "image/png",
    });
    const invalid = new File([new Uint8Array([0xff])], "broken.txt", {
      type: "text/plain",
    });
    expect(await validateTextAttachment(image)).toBe("not_text");
    expect(await validateTextAttachment(invalid)).toBe("not_text");
  });
});
