import { createContext, useCallback, useContext, useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { Button } from "./ui";

// ---------------------------------------------------------------- toasts
type ToastKind = "ok" | "error" | "info";
interface ToastItem { id: number; text: string; kind: ToastKind }
const ToastCtx = createContext<(text: string, kind?: ToastKind) => void>(() => undefined);
export const useToast = () => useContext(ToastCtx);

// ---------------------------------------------------------------- confirmations
export interface ConfirmOpts { title: string; body?: ReactNode; confirmLabel?: string; danger?: boolean }
type ConfirmFn = (o: ConfirmOpts) => Promise<boolean>;
const ConfirmCtx = createContext<ConfirmFn>(async () => true);
export const useConfirm = () => useContext(ConfirmCtx);

export function KitProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const seq = useRef(0);
  const toast = useCallback((text: string, kind: ToastKind = "ok") => {
    const id = ++seq.current;
    setToasts((t) => [...t.slice(-3), { id, text, kind }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), kind === "error" ? 8000 : 4500);
  }, []);

  const [dlg, setDlg] = useState<(ConfirmOpts & { resolve: (v: boolean) => void }) | null>(null);
  const confirm = useCallback<ConfirmFn>((o) => new Promise<boolean>((resolve) => setDlg({ ...o, resolve })), []);
  const close = (v: boolean) => {
    dlg?.resolve(v);
    setDlg(null);
  };

  return (
    <ToastCtx.Provider value={toast}>
      <ConfirmCtx.Provider value={confirm}>
        {children}
        <div className="pointer-events-none fixed bottom-20 right-4 z-[60] flex w-[min(92vw,360px)] flex-col gap-2 md:bottom-4" aria-live="polite" data-testid="toasts">
          {toasts.map((t) => (
            <div key={t.id} role={t.kind === "error" ? "alert" : "status"}
              className={`pointer-events-auto animate-in rounded-lg border px-3 py-2 text-[13px] shadow-lg backdrop-blur ${t.kind === "error" ? "border-down/40 bg-down/15 text-down" : t.kind === "info" ? "border-info/40 bg-panel text-ink" : "border-up/40 bg-panel text-ink"}`}>
              <span className={`mr-2 font-semibold ${t.kind === "error" ? "text-down" : "text-up"}`} aria-hidden>{t.kind === "error" ? "!" : "✓"}</span>{t.text}
            </div>
          ))}
        </div>
        {dlg && <Modal title={dlg.title} onClose={() => close(false)}>
          {dlg.body && <div className="text-[13px] leading-relaxed text-muted">{dlg.body}</div>}
          <div className="mt-5 flex justify-end gap-2">
            <Button variant="ghost" onClick={() => close(false)}>Cancel</Button>
            <Button variant={dlg.danger ? "danger" : "primary"} onClick={() => close(true)} autoFocus>{dlg.confirmLabel ?? "Confirm"}</Button>
          </div>
        </Modal>}
      </ConfirmCtx.Provider>
    </ToastCtx.Provider>
  );
}

// ---------------------------------------------------------------- overlays
function useEscape(onClose: () => void) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose]);
}

export function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  useEscape(onClose);
  const id = useId();
  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/60 p-4" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div role="dialog" aria-modal="true" aria-labelledby={id} className="animate-in w-full max-w-md rounded-2xl border border-line bg-panel p-5 shadow-2xl">
        <h2 id={id} className="mb-2 text-base font-semibold">{title}</h2>
        {children}
      </div>
    </div>
  );
}

export function Drawer({ title, subtitle, onClose, children }: { title: ReactNode; subtitle?: ReactNode; onClose: () => void; children: ReactNode }) {
  useEscape(onClose);
  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-black/50" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <aside role="dialog" aria-modal="true" aria-label="Details" className="animate-in flex h-full w-full max-w-[560px] flex-col border-l border-line bg-bg shadow-2xl">
        <header className="flex items-start justify-between gap-3 border-b border-line bg-panel px-5 py-3">
          <div><h2 className="text-base font-semibold">{title}</h2>{subtitle && <div className="mt-0.5 text-xs text-muted">{subtitle}</div>}</div>
          <button aria-label="Close details" onClick={onClose} className="rounded-md px-2 py-1 text-lg leading-none text-muted hover:bg-panel2 hover:text-ink">×</button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto p-5">{children}</div>
      </aside>
    </div>
  );
}

// ---------------------------------------------------------------- small controls
export function Tip({ text, label = "More info" }: { text: string; label?: string }) {
  const id = useId();
  return (
    <span className="group relative inline-flex align-middle">
      <button type="button" aria-label={label} aria-describedby={id} className="grid h-4 w-4 place-items-center rounded-full text-[10px] font-bold text-muted ring-1 ring-line hover:text-ink focus:text-ink">?</button>
      <span id={id} role="tooltip" className="pointer-events-none invisible absolute left-1/2 top-full z-30 mt-1.5 w-60 -translate-x-1/2 rounded-lg border border-line bg-panel2 px-2.5 py-2 text-left text-[11px] font-normal normal-case leading-snug tracking-normal text-ink opacity-0 shadow-xl transition group-focus-within:visible group-focus-within:opacity-100 group-hover:visible group-hover:opacity-100">{text}</span>
    </span>
  );
}

export function Segmented<T extends string>({ value, onChange, options, label }: { value: T; onChange: (v: T) => void; options: { value: T; label: string; badge?: string }[]; label: string }) {
  return (
    <div role="radiogroup" aria-label={label} className="inline-flex rounded-lg bg-panel2 p-0.5 ring-1 ring-line">
      {options.map((o) => (
        <button key={o.value} type="button" role="radio" aria-checked={value === o.value} onClick={() => onChange(o.value)}
          className={`rounded-md px-3 py-1 text-[13px] font-medium transition ${value === o.value ? "bg-accent text-accentink shadow" : "text-muted hover:text-ink"}`}>
          {o.label}{o.badge && <span className="ml-1.5 text-[10px] opacity-80">{o.badge}</span>}
        </button>
      ))}
    </div>
  );
}

export function Skeleton({ className = "h-4 w-full" }: { className?: string }) {
  return <div className={`animate-pulse rounded-md bg-panel2 ${className}`} aria-hidden />;
}

export function Loading({ rows = 3, label = "Loading" }: { rows?: number; label?: string }) {
  return (
    <div role="status" aria-label={label} className="space-y-2 p-4">
      {Array.from({ length: rows }, (_, i) => <Skeleton key={i} className={`h-4 ${i % 2 ? "w-4/5" : "w-full"}`} />)}
      <span className="sr-only">{label}...</span>
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div role="alert" className="m-4 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-down/40 bg-down/10 px-4 py-3 text-[13px] text-down">
      <span>{message}</span>
      {onRetry && <Button variant="subtle" onClick={onRetry}>Try again</Button>}
    </div>
  );
}

/** Slider with a number box: both always agree, so people can drag OR type. */
export function SliderInput({ value, onChange, min, max, step, unit, label, disabled, invalid }: {
  value: number; onChange: (v: number) => void; min: number; max: number; step: number; unit?: string; label: string; disabled?: boolean; invalid?: boolean;
}) {
  const clamp = (v: number) => Math.min(max, Math.max(min, v));
  const pctFill = ((clamp(value) - min) / (max - min || 1)) * 100;
  return (
    <div className="flex items-center gap-3">
      <input type="range" aria-label={label} min={min} max={max} step={step} value={clamp(value)} disabled={disabled}
        onChange={(e) => onChange(Number(e.target.value))}
        style={{ background: `linear-gradient(to right, var(--accent) ${pctFill}%, var(--border) ${pctFill}%)` }}
        className="h-1.5 w-full cursor-pointer appearance-none rounded-full accent-[var(--accent)] disabled:cursor-not-allowed disabled:opacity-40" />
      <div className="flex shrink-0 items-center gap-1">
        <input type="number" aria-label={`${label} (type a value)`} min={min} max={max} step={step} value={Number.isFinite(value) ? value : ""} disabled={disabled}
          aria-invalid={invalid || undefined}
          onChange={(e) => onChange(e.target.value === "" ? NaN : Number(e.target.value))}
          className={`num w-20 rounded-lg border bg-bg px-2 py-1 text-right text-[13px] focus:border-accent disabled:opacity-40 ${invalid ? "border-down" : "border-line"}`} />
        {unit && <span className="w-6 text-xs text-muted">{unit}</span>}
      </div>
    </div>
  );
}

export function Chip({ active, onClick, children, count }: { active: boolean; onClick: () => void; children: ReactNode; count?: number }) {
  return (
    <button type="button" aria-pressed={active} onClick={onClick}
      className={`rounded-full px-3 py-1 text-xs font-medium ring-1 transition ${active ? "bg-accent/15 text-accent ring-accent/50" : "text-muted ring-line hover:text-ink"}`}>
      {children}{count !== undefined && <span className="num ml-1.5 opacity-70">{count}</span>}
    </button>
  );
}

/** Persisted UI preference (sort, filter, tab...). Storage may be unavailable; it then just doesn't persist. */
export function usePref<T extends string | number | boolean>(key: string, initial: T): [T, (v: T) => void] {
  const full = `mwbot:${key}`;
  const [v, setV] = useState<T>(() => {
    try {
      const raw = window.localStorage.getItem(full);
      return raw === null ? initial : (JSON.parse(raw) as T);
    } catch {
      return initial;
    }
  });
  const set = useCallback((n: T) => {
    setV(n);
    try {
      window.localStorage.setItem(full, JSON.stringify(n));
    } catch {
      /* private mode: preference simply is not remembered */
    }
  }, [full]);
  return useMemo(() => [v, set], [v, set]);
}
