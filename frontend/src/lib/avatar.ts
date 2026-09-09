/** Avatar initials for a chat message's circular badge - first letter of
 * the first and last name (e.g. "Muhammad Waqar" -> "MW"), falling back to
 * a single letter for a one-word name and to the email's first letter for
 * an account with no full_name at all. */
export function initialsFor(fullName: string | null | undefined, email: string): string {
  const parts = (fullName ?? "").trim().split(/\s+/).filter(Boolean);
  if (parts.length > 0) {
    const first = parts[0][0];
    const last = parts.length > 1 ? parts[parts.length - 1][0] : "";
    return (first + last).toUpperCase();
  }
  return (email[0] ?? "?").toUpperCase();
}
