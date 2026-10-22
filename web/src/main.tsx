import React from "react";
import { createRoot } from "react-dom/client";
import "./styles/app.css";
import { App } from "./App";
import { Capacitor } from "@capacitor/core";
import { NativeShell } from "./NativeShell";

const rootEl = document.getElementById("root");
if (rootEl) {
  createRoot(rootEl).render(
    <React.StrictMode>
      {Capacitor.isNativePlatform() ? <NativeShell /> : <App />}
    </React.StrictMode>
  );
}
