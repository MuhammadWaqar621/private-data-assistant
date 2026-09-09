/**
 * Access/refresh token storage.
 *
 * localStorage is fine for this portfolio project (no XSS-hardening
 * requirements here) - see README for the tradeoff this implies. The
 * storage keys are namespaced separately from the sibling
 * private-document-assistant app so that running both on localhost at the
 * same time doesn't have one's session clobber the other's.
 */

const ACCESS_TOKEN_KEY = "pda.access_token";
const REFRESH_TOKEN_KEY = "pda.refresh_token";

export function getAccessToken(): string | null {
  return localStorage.getItem(ACCESS_TOKEN_KEY);
}

export function getRefreshToken(): string | null {
  return localStorage.getItem(REFRESH_TOKEN_KEY);
}

export function setTokens(accessToken: string, refreshToken: string): void {
  localStorage.setItem(ACCESS_TOKEN_KEY, accessToken);
  localStorage.setItem(REFRESH_TOKEN_KEY, refreshToken);
}

export function setAccessToken(accessToken: string): void {
  localStorage.setItem(ACCESS_TOKEN_KEY, accessToken);
}

export function clearTokens(): void {
  localStorage.removeItem(ACCESS_TOKEN_KEY);
  localStorage.removeItem(REFRESH_TOKEN_KEY);
}

export function isLoggedIn(): boolean {
  return getAccessToken() !== null;
}
