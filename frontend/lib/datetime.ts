/**
 * Datetime helpers — timezone-safe.
 *
 * Backend serializes datetime.utcnow() without a tz marker (e.g.
 * "2026-05-10T13:23:51"). JavaScript treats naive ISO strings as LOCAL
 * time per spec — which causes a 5h drift on US-CDT browsers. These
 * helpers force UTC interpretation by appending "Z" when no tz suffix
 * is present, then format in the user's locale.
 */

const TZ_REGEX = /[zZ]|[+-]\d{2}:?\d{2}$/;

export function parseUtcIso(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const normalized = TZ_REGEX.test(iso) ? iso : iso + "Z";
  const d = new Date(normalized);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function formatLocalTime(
  iso: string | null | undefined,
  opts: Intl.DateTimeFormatOptions = {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  },
): string {
  const d = parseUtcIso(iso);
  if (!d) return iso || "—";
  return new Intl.DateTimeFormat(undefined, opts).format(d);
}

export function formatRelative(iso: string | null | undefined): string {
  const d = parseUtcIso(iso);
  if (!d) return "—";
  const diff = Date.now() - d.getTime();
  if (diff < 60_000) return `${Math.max(0, Math.round(diff / 1000))}s ago`;
  if (diff < 3_600_000) return `${Math.round(diff / 60_000)}m ago`;
  return formatLocalTime(iso);
}
