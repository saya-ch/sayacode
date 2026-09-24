export interface ModelTestState {
  profile: string | null;
  result: string | null;
  busy: boolean;
}

export const noModelTest: ModelTestState = { profile: null, result: null, busy: false };

export function startModelTest(profile: string): ModelTestState {
  return { profile, result: null, busy: true };
}

export function finishModelTest(
  state: ModelTestState,
  profile: string,
  result: string,
): ModelTestState {
  return state.profile === profile ? { profile, result, busy: false } : state;
}
