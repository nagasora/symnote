import React from "react";
import { createRoot } from "react-dom/client";
import "./styles/fonts.css";
import "./styles/tokens.css";
import "./styles/base.css";
import "./styles/motion.css";
import "./styles/game.css";
import { App } from "./App";
import { Capacitor } from "@capacitor/core";
import { NativeShell } from "./NativeShell";
import { applyTheme, initialTheme } from "./lib/theme";

applyTheme(initialTheme());

const rootEl = document.getElementById("root");
if (rootEl) {
  createRoot(rootEl).render(
    <React.StrictMode>
      {Capacitor.isNativePlatform() ? <NativeShell /> : <App />}
    </React.StrictMode>,
  );
}
