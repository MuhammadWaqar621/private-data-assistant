import type { ReactNode } from "react";
import { Navigate } from "react-router-dom";

import { isLoggedIn } from "../lib/auth";

/**
 * Redirects to /login when there's no stored access token. This is only
 * the fast client-side check (no token at all); an expired/invalid token
 * is instead caught by the api client's 401 handling inside the page
 * itself (see AppShellPage), which also tries a refresh first.
 */
export default function ProtectedRoute({ children }: { children: ReactNode }) {
  if (!isLoggedIn()) {
    return <Navigate to="/login" replace />;
  }
  return <>{children}</>;
}
