/**
 * Small fetch wrapper for the backend API.
 *
 * - Always sends/parses JSON.
 * - `auth: true` attaches the stored access token as a Bearer header.
 * - On a 401 with `auth: true`, transparently tries POST /api/auth/refresh
 *   once and retries the original request before giving up - this is what
 *   lets a stored refresh token silently extend a session past the (short)
 *   access token lifetime without the user re-entering credentials.
 *
 * The one addition over the sibling private-document-assistant's copy of
 * this file is `patch`, which PATCH /api/chats/{id} needs: that endpoint
 * distinguishes an ABSENT key from a null one (omit `connection_id` to
 * leave the binding alone, send `null` to clear it), so callers build the
 * body themselves and this wrapper must pass it through verbatim.
 */

import { apiUrl } from "./config";
import { clearTokens, getAccessToken, getRefreshToken, setAccessToken } from "./auth";

export class ApiError extends Error {
  status: number;
  body: unknown;

  constructor(status: number, body: unknown) {
    super(errorMessageFrom(body) ?? `Request failed with status ${status}`);
    this.status = status;
    this.body = body;
  }
}

function errorMessageFrom(body: unknown): string | null {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail === "object" && "message" in detail) {
      const message = (detail as { message: unknown }).message;
      if (typeof message === "string") return message;
    }
    // FastAPI's pydantic-validation-error shape: detail is an array of
    // {loc, msg, type} objects. pydantic v2 prefixes a custom validator's
    // ValueError with "Value error, " - strip that, it's an internal
    // implementation detail the user shouldn't see.
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as { msg?: unknown };
      if (typeof first.msg === "string") {
        return first.msg.replace(/^Value error,\s*/, "");
      }
    }
  }
  return null;
}

/** True for the 503 the backend returns when ENCRYPTION_KEY is missing -
 * `POST /api/connections` refuses rather than storing a plaintext
 * password. Surfaced as its own message in ConnectionsModal. */
export function isEncryptionNotConfigured(err: unknown): boolean {
  if (!(err instanceof ApiError)) return false;
  const detail = (err.body as { detail?: { error?: string } } | null)?.detail;
  return detail?.error === "encryption_not_configured";
}

async function parseBody(res: Response): Promise<unknown> {
  const text = await res.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

async function refreshAccessToken(): Promise<boolean> {
  const refreshToken = getRefreshToken();
  if (!refreshToken) return false;

  try {
    const res = await fetch(apiUrl("/api/auth/refresh"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });
    if (!res.ok) return false;
    const data = (await res.json()) as { access_token: string };
    setAccessToken(data.access_token);
    return true;
  } catch {
    return false;
  }
}

type RequestOptions = {
  method?: "GET" | "POST" | "DELETE" | "PUT" | "PATCH";
  body?: unknown;
  auth?: boolean;
};

async function request<T>(path: string, options: RequestOptions = {}, _retry = true): Promise<T> {
  const { method = "GET", body, auth = false } = options;

  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (auth) {
    const token = getAccessToken();
    if (token) headers.Authorization = `Bearer ${token}`;
  }

  const res = await fetch(apiUrl(path), {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  if (res.status === 401 && auth && _retry) {
    const refreshed = await refreshAccessToken();
    if (refreshed) {
      return request<T>(path, options, false);
    }
    clearTokens();
  }

  const parsed = await parseBody(res);
  if (!res.ok) {
    throw new ApiError(res.status, parsed);
  }
  return parsed as T;
}

/**
 * Multipart file upload - used only by POST /api/connections/sqlite, which
 * takes the .sqlite/.db file plus a `name` form field. Kept separate from
 * request() above because a FormData body must NOT get a
 * "Content-Type: application/json" header - the browser sets its own
 * multipart boundary when the body is a FormData instance.
 */
async function requestForm<T>(path: string, formData: FormData, _retry = true): Promise<T> {
  const headers: Record<string, string> = {};
  const token = getAccessToken();
  if (token) headers.Authorization = `Bearer ${token}`;

  const res = await fetch(apiUrl(path), { method: "POST", headers, body: formData });

  if (res.status === 401 && _retry) {
    const refreshed = await refreshAccessToken();
    if (refreshed) {
      return requestForm<T>(path, formData, false);
    }
    clearTokens();
  }

  const parsed = await parseBody(res);
  if (!res.ok) {
    throw new ApiError(res.status, parsed);
  }
  return parsed as T;
}

export const api = {
  get: <T,>(path: string, auth = false): Promise<T> => request<T>(path, { method: "GET", auth }),
  post: <T,>(path: string, body?: unknown, auth = false): Promise<T> =>
    request<T>(path, { method: "POST", body, auth }),
  patch: <T,>(path: string, body?: unknown, auth = false): Promise<T> =>
    request<T>(path, { method: "PATCH", body, auth }),
  del: <T,>(path: string, auth = false): Promise<T> => request<T>(path, { method: "DELETE", auth }),
  uploadFile: <T,>(path: string, formData: FormData): Promise<T> => requestForm<T>(path, formData),
};
