"use client";

type Props = {
  username?: string;
  label?: string;
  className?: string;
};

export default function BMCButton({
  username,
  label = "Buy us a coffee",
  className,
}: Props) {
  const u =
    username ||
    process.env.NEXT_PUBLIC_BMC_USERNAME ||
    "dbutler";
  const href = `https://buymeacoffee.com/${u}`;
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className={`btn btn-bmc ${className || ""}`}
      style={{ textDecoration: "none" }}
      aria-label={`${label} (opens Buy Me a Coffee)`}
    >
      <span aria-hidden="true">☕</span> {label}
    </a>
  );
}
