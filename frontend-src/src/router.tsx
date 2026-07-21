import { createBrowserRouter, Navigate } from "react-router-dom";
import { AppShell } from "@/layouts/AppShell";
import { AuthShell } from "@/layouts/AuthShell";
import { RecordingsList } from "@/pages/app/RecordingsList";
import { RecordingNew } from "@/pages/app/RecordingNew";
import { RecordingDetail } from "@/pages/app/RecordingDetail";
import { Tasks } from "@/pages/app/Tasks";
import { Hotwords } from "@/pages/app/Hotwords";
import { Voiceprints } from "@/pages/app/Voiceprints";
import { Settings } from "@/pages/app/Settings";
import { Login } from "@/pages/Login";
import { NotFound } from "@/pages/NotFound";

// Single-user mode. The optional access gate (AHAMVOICE_ACCESS_PASSWORD) uses
// a cookie set by /api/auth/login; the 401 interceptor in api/client.ts
// redirects to /login when the gate is active and the user is unauthenticated.
export const router = createBrowserRouter([
  {
    path: "/login",
    element: <AuthShell />,
    children: [{ index: true, element: <Login /> }],
  },
  {
    path: "/",
    element: <Navigate to="/app/recordings/new" replace />,
  },
  {
    path: "/app",
    element: <AppShell />,
    children: [
      { index: true, element: <Navigate to="/app/recordings/new" replace /> },
      { path: "workspace", element: <Navigate to="/app/recordings/new" replace /> },
      { path: "recordings", element: <RecordingsList /> },
      { path: "recordings/new", element: <RecordingNew /> },
      { path: "recordings/:id", element: <RecordingDetail /> },
      { path: "tasks", element: <Tasks /> },
      { path: "hotwords", element: <Hotwords /> },
      { path: "voiceprints", element: <Voiceprints /> },
      { path: "settings", element: <Settings /> },
    ],
  },
  {
    path: "*",
    element: <NotFound />,
  },
]);
