import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { resetLegacyBrowserState } from "./app/storage";
import "./styles/index.css";

resetLegacyBrowserState();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
