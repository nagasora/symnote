import { useEffect, useState } from "react";
import { App } from "./App";
import { NativeCapture } from "./NativeCapture";
import { SESSION_CHANGED } from "./nativeClient";

export function NativeShell() {
  const [screen, setScreen] = useState<"device" | "cloud">("device");
  const [cloudOpened, setCloudOpened] = useState(false);
  const [sessionVersion, setSessionVersion] = useState(0);
  useEffect(() => {
    const reset = () => setSessionVersion((version) => version + 1);
    window.addEventListener(SESSION_CHANGED, reset);
    return () => window.removeEventListener(SESSION_CHANGED, reset);
  }, []);

  return <>
    <nav aria-label="保存先" style={{ padding: "max(8px, env(safe-area-inset-top)) 16px 8px", display: "flex", gap: 12, background: "var(--paper)", position: "sticky", top: 0, zIndex: 100 }}>
      <button className={screen === "device" ? "btn primary" : "btn"} style={{ minHeight: 44 }} aria-pressed={screen === "device"} onClick={() => setScreen("device")}>この端末</button>
      <button className={screen === "cloud" ? "btn primary" : "btn"} style={{ minHeight: 44 }} aria-pressed={screen === "cloud"} onClick={() => { setCloudOpened(true); setScreen("cloud"); }}>クラウド</button>
    </nav>
    {/* Keep the device editor mounted so switching screens never discards unsaved text. */}
    <div hidden={screen !== "device"}><NativeCapture /></div>
    {cloudOpened && <div hidden={screen !== "cloud"}><App key={sessionVersion} /></div>}
  </>;
}
