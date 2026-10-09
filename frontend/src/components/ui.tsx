import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode } from "react";
import { tone } from "../format";

export function Card({ title, right, children, className = "", pad = true }: { title?: ReactNode; right?: ReactNode; children: ReactNode; className?: string; pad?: boolean }) {
  return (
    <section className={`animate-in min-w-0 rounded-xl border border-line bg-panel ${className}`}>
      {(title || right) && (
        <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-2.5">
          <h2 className="text-[13px] font-semibold tracking-wide text-ink">{title}</h2>
          <div className="flex items-center gap-2">{right}</div>
        </header>
      )}
      <div className={pad ? "p-4" : ""}>{children}</div>
    </section>
  );
}

export function Stat({ label, value, sub, valueClass = "", testId }: { label: string; value: ReactNode; sub?: ReactNode; valueClass?: string; testId?: string }) {
  return (
    <div className="animate-in rounded-xl border border-line bg-panel px-4 py-3" data-testid={testId}>
      <div className="text-[11px] font-medium uppercase tracking-wider text-muted">{label}</div>
      <div className={`num mt-1 text-[22px] font-semibold leading-tight ${valueClass}`}>{value}</div>
      {sub !== undefined && <div className="mt-0.5 text-xs text-muted">{sub}</div>}
    </div>
  );
}

type Variant = "primary" | "ghost" | "danger" | "subtle";
export function Button({ variant = "subtle", className = "", ...p }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  const base = "inline-flex items-center justify-center gap-1.5 rounded-lg px-3 py-1.5 text-[13px] font-medium transition disabled:cursor-not-allowed disabled:opacity-40";
  const v: Record<Variant, string> = {
    primary: "bg-accent text-accentink hover:brightness-110",
    ghost: "text-muted hover:bg-panel2 hover:text-ink",
    danger: "bg-down/15 text-down ring-1 ring-down/40 hover:bg-down/25",
    subtle: "bg-panel2 text-ink ring-1 ring-line hover:ring-muted/50",
  };
  return <button type="button" className={`${base} ${v[variant]} ${className}`} {...p} />;
}

export function Badge({ children, color = "muted", title }: { children: ReactNode; color?: "muted" | "up" | "down" | "warn" | "info" | "accent"; title?: string }) {
  const map: Record<string, string> = {
    muted: "bg-panel2 text-muted ring-line",
    up: "bg-up/10 text-up ring-up/30",
    down: "bg-down/10 text-down ring-down/30",
    warn: "bg-warn/10 text-warn ring-warn/30",
    info: "bg-info/10 text-info ring-info/30",
    accent: "bg-accent/10 text-accent ring-accent/30",
  };
  return (
    <span title={title} className={`inline-flex items-center gap-1 whitespace-nowrap rounded-md px-1.5 py-0.5 text-[11px] font-medium ring-1 ${map[color]}`}>
      {children}
    </span>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="block text-xs">
      <span className="mb-1 block font-medium text-muted">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-[11px] leading-snug text-muted/80">{hint}</span>}
    </label>
  );
}

export function TextInput({ className = "", ...p }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={`num w-full rounded-lg border border-line bg-bg px-2.5 py-1.5 text-[13px] text-ink placeholder:text-muted/60 focus:border-accent ${className}`} {...p} />;
}

export function Select({ className = "", children, ...p }: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return <select className={`w-full rounded-lg border border-line bg-bg px-2.5 py-1.5 text-[13px] text-ink focus:border-accent ${className}`} {...p}>{children}</select>;
}

export function Toggle({ checked, onChange, label, disabled }: { checked: boolean; onChange: (v: boolean) => void; label: string; disabled?: boolean }) {
  return (
    <button type="button" role="switch" aria-checked={checked} aria-label={label} disabled={disabled} onClick={() => onChange(!checked)}
      className={`relative h-5 w-9 shrink-0 rounded-full transition disabled:opacity-40 ${checked ? "bg-accent" : "bg-line"}`}>
      <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-all ${checked ? "left-[18px]" : "left-0.5"}`} />
    </button>
  );
}

export function PnL({ value, text, className = "" }: { value: number | null | undefined; text: string; className?: string }) {
  return <span className={`num ${tone(value)} ${className}`}>{text}</span>;
}

export function ErrorBanner({ message, onClose }: { message: string | null | undefined; onClose?: () => void }) {
  if (!message) return null;
  return (
    <div role="alert" className="mb-3 flex items-start justify-between gap-3 rounded-lg border border-down/40 bg-down/10 px-3 py-2 text-[13px] text-down">
      <span>{message}</span>
      {onClose && <button aria-label="Dismiss" onClick={onClose} className="text-down/80 hover:text-down">x</button>}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="px-4 py-10 text-center text-sm text-muted">{children}</div>;
}

export function Progress({ value, className = "" }: { value: number; className?: string }) {
  return (
    <div className={`h-1.5 w-full overflow-hidden rounded-full bg-line ${className}`} role="progressbar" aria-valuenow={Math.round(value * 100)} aria-valuemin={0} aria-valuemax={100}>
      <div className="h-full rounded-full bg-accent transition-all duration-500" style={{ width: `${Math.max(0, Math.min(1, value)) * 100}%` }} />
    </div>
  );
}

export function Icon({ name, className = "h-4 w-4" }: { name: string; className?: string }) {
  const paths: Record<string, string> = {
    dashboard: "M3 13h8V3H3v10zm0 8h8v-6H3v6zm10 0h8V11h-8v10zm0-18v6h8V3h-8z",
    opportunities: "M12 3l2.6 5.6 6.1.7-4.5 4.2 1.2 6L12 16.5 6.6 19.5l1.2-6L3.3 9.3l6.1-.7L12 3z",
    portfolio: "M4 7h16v12H4zM9 7V5a1 1 0 011-1h4a1 1 0 011 1v2M4 12h16",
    scanner: "M11 4a7 7 0 105.2 11.7l4.1 4.1 1.4-1.4-4.1-4.1A7 7 0 0011 4zm0 2a5 5 0 110 10 5 5 0 010-10z",
    stock: "M3 17l5-6 4 4 8-10M3 21h18",
    positions: "M4 6h16M4 12h16M4 18h10",
    history: "M12 8v5l3 2M3 12a9 9 0 109-9 9 9 0 00-7 3.5M3 4v4h4",
    lab: "M9 3v6L4 19a2 2 0 002 3h12a2 2 0 002-3l-5-10V3M8 3h8",
    performance: "M4 20V10m6 10V4m6 16v-8m4 8H2",
    settings: "M4 7h9m4 0h3M4 17h3m4 0h9M15 4v6M9 14v6",
  };
  return (
    <svg viewBox="0 0 24 24" className={className} fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={paths[name] ?? ""} />
    </svg>
  );
}
