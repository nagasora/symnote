import React from "react";
import { createRoot } from "react-dom/client";
import "./styles/app.css";
import { App } from "./App";
import { Capacitor } from "@capacitor/core";
import { NativeCapture } from "./NativeCapture";

const rootEl = document.getElementById("root");
if (rootEl) {
  createRoot(rootEl).render(
    <React.StrictMode>
      {Capacitor.isNativePlatform() ? <NativeCapture /> : <App />}
    </React.StrictMode>
  );
}
