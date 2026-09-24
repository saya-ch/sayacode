export interface LiveTextState {
  text: string;
  suppressed: boolean;
  resynced: boolean;
}

export const emptyLiveText: LiveTextState = { text: "", suppressed: false, resynced: false };

export function reduceLiveText(
  state: LiveTextState,
  action: "delta" | "resync" | "new-run" | "settled",
  delta = "",
): LiveTextState {
  if (action === "resync") return { text: "", suppressed: true, resynced: true };
  if (action === "new-run" || action === "settled") return emptyLiveText;
  return state.suppressed ? state : { ...state, text: state.text + delta };
}
