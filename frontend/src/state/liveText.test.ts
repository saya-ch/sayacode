import { expect, it } from "vitest";
import { emptyLiveText, reduceLiveText } from "./liveText";

it("重同步后不把丢失前缀与新 delta 拼成伪完整回复", () => {
  const before = reduceLiveText(emptyLiveText, "delta", "前半句");
  const interrupted = reduceLiveText(before, "resync");
  expect(interrupted).toEqual({ text: "", suppressed: true, resynced: true });
  expect(reduceLiveText(interrupted, "delta", "后半句")).toEqual(interrupted);
  expect(reduceLiveText(interrupted, "settled")).toEqual(emptyLiveText);
});
