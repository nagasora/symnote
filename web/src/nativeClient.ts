import { Capacitor, registerPlugin } from "@capacitor/core";
import { makeNativeRequester, type NativeAPIPlugin } from "./nativeTransport";

interface SymNoteNativePlugin extends NativeAPIPlugin {
  authenticate(): Promise<{ authenticated: boolean }>;
  logout(): Promise<{ remoteRevoked?: boolean }>;
}

const native = registerPlugin<SymNoteNativePlugin>("SymNoteNative");
export const isNativeApp = () => Capacitor.isNativePlatform();
export const nativeRequest = makeNativeRequester(native);
export const SESSION_CHANGED = "symnote-native-session-changed";

function sessionChanged() {
  window.dispatchEvent(new Event(SESSION_CHANGED));
}

export async function nativeSignIn() {
  const result = await native.authenticate();
  if (!result.authenticated) throw new Error("ログインできませんでした。");
  sessionChanged();
}

export async function nativeSignOut() {
  const result = await native.logout();
  sessionChanged();
  return result;
}
