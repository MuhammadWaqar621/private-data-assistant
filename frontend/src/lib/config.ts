/**
 * Backend API base URL.
 *
 * Empty by default, which makes every call relative ("/api/..."). That
 * works out of the box with the Vite dev server proxy (see
 * vite.config.ts) and with any deployment that fronts frontend+backend
 * behind the same origin. Set VITE_API_BASE_URL (see frontend/.env or the
 * VITE_API_BASE_URL build arg in docker-compose.yml) to an absolute URL
 * (e.g. http://localhost:8000) when the frontend and backend are served
 * from different origins, as they are with the default docker-compose
 * setup (frontend on :4173, backend on :8000).
 */
export const API_BASE_URL: string = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/+$/, "");

export function apiUrl(path: string): string {
  return `${API_BASE_URL}${path}`;
}
