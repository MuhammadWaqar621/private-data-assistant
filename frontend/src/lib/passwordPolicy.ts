/**
 * Client-side mirror of backend/app/core/security.py's
 * validate_password_strength() - this is a UX convenience only (instant
 * feedback before submitting); the backend re-checks the same policy on
 * every signup/reset-password request regardless, since client-side
 * validation can always be bypassed by calling the API directly.
 */
export const PASSWORD_POLICY_HINT =
  "At least 8 characters, with an uppercase letter, a lowercase letter, a number, and a special character.";

export function passwordPolicyError(password: string): string | null {
  const problems: string[] = [];
  if (password.length < 8) problems.push("at least 8 characters");
  if (!/[A-Z]/.test(password)) problems.push("an uppercase letter");
  if (!/[a-z]/.test(password)) problems.push("a lowercase letter");
  if (!/[0-9]/.test(password)) problems.push("a number");
  if (!/[^A-Za-z0-9]/.test(password)) problems.push("a special character");

  if (problems.length === 0) return null;
  return "Password must contain " + problems.join(", ") + ".";
}
