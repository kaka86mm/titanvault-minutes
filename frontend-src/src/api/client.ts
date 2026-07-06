import axios, { AxiosError } from "axios";

// Axios base URL.
//
// Production: FastAPI serves frontend/dist + /api on the same origin (single-port),
// so relative "/api" works. Dev: Vite runs on :5174 and proxies /api → backend,
// also relative "/api". Either way we use relative "/api".
function deriveBaseURL(): string {
  return "/api";
}

// Axios instance shared by every API call. The access gate (when
// AHAMVOICE_ACCESS_PASSWORD is set) uses an httpOnly cookie set by
// POST /api/auth/login, so withCredentials must be true for the browser to
// send/receive that cookie.
export const api = axios.create({
  baseURL: deriveBaseURL(),
  timeout: 180_000,
  withCredentials: true,
});

const TOKEN_KEY = "tv.token";

export function getStoredToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setStoredToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* localStorage unavailable (incognito, fs error) — gracefully ignore */
  }
}

// Listeners that get pinged when the API replies 401 so the auth provider can
// clear state without coupling every component to the axios instance.
type UnauthorizedListener = () => void;
const unauthorizedListeners = new Set<UnauthorizedListener>();
export function onUnauthorized(fn: UnauthorizedListener): () => void {
  unauthorizedListeners.add(fn);
  return () => unauthorizedListeners.delete(fn);
}

api.interceptors.request.use((config) => {
  const token = getStoredToken();
  if (token) {
    config.headers.set("Authorization", `Bearer ${token}`);
  }
  return config;
});

api.interceptors.response.use(
  (res) => res,
  (error: AxiosError) => {
    if (error.response?.status === 401) {
      setStoredToken(null);
      for (const fn of unauthorizedListeners) fn();
      // Access gate active and not already on /login → redirect there.
      if (typeof window !== "undefined" && !window.location.pathname.startsWith("/login")) {
        window.location.href = "/login";
      }
    }
    return Promise.reject(error);
  },
);

// Pull a readable error message off whatever axios throws. The backend always
// returns `{ detail: string }` from HTTPException, but we also handle the
// CRM-style `{ ok: false, error, diagnostic }` shape and bare strings.
export function readApiError(err: unknown): string {
  if (axios.isAxiosError(err)) {
    const data = err.response?.data as
      | { detail?: string; error?: string; diagnostic?: string; message?: string }
      | undefined;
    if (data?.detail) return String(data.detail);
    if (data?.error) return String(data.error);
    if (data?.diagnostic) return String(data.diagnostic);
    if (data?.message) return String(data.message);
    if (err.response?.statusText) return `${err.response.status} ${err.response.statusText}`;
    if (err.code === "ECONNABORTED") return "请求超时";
    if (err.message) return err.message;
  }
  if (err instanceof Error) return err.message;
  return String(err);
}
