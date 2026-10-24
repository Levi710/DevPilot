
import React from "react";
import { createRoot } from "react-dom/client";

// App.jsx contains the DevPilot dashboard and its styles.
import App from "./App.jsx";

// Mount the React application into the HTML root element.
createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
