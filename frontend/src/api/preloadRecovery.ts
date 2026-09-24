const RELOAD_KEY = "sayacode-preload-recovery-at";
const RELOAD_WINDOW_MS = 60_000;

export function recoverPreloadFailure(
  event: Event,
  storage: Pick<Storage, "getItem" | "setItem"> | null,
  reload: () => void,
  showFailure: () => void,
  now = Date.now(),
): "reload" | "failure" {
  event.preventDefault();
  if (!storage) {
    showFailure();
    return "failure";
  }
  try {
    const prior = Number(storage.getItem(RELOAD_KEY));
    if (prior > 0 && Math.abs(now - prior) < RELOAD_WINDOW_MS) {
      showFailure();
      return "failure";
    }
    storage.setItem(RELOAD_KEY, String(now));
    reload();
    return "reload";
  } catch {
    showFailure();
    return "failure";
  }
}
