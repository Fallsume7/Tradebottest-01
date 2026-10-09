import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useApp } from "../App";
import { ApiError, api } from "../api";
import { ErrorState, Loading, SliderInput, Tip, useConfirm, usePref, useToast } from "../components/kit";
import { Badge, Button, Card, Select, TextInput, Toggle } from "../components/ui";
import { readText } from "../format";
import type { ApiFieldError, Effect, Json, SchemaField, SettingsSchema } from "../types";

// ---------------------------------------------------------------- value helpers
export function sameValue(a: Json | undefined, b: Json | undefined): boolean {
  if (typeof a === "number" && typeof b === "number") return Math.abs(a - b) < 1e-9;
  return JSON.stringify(a) === JSON.stringify(b);
}

const display = (f: SchemaField, v: Json): string => {
  if (v === null || v === undefined) return "off";
  if (Array.isArray(v)) return v.join(", ") || "none";
  if (f.control === "select") return f.options.find((o) => o.value === v)?.label ?? String(v);
  if (f.control === "switch") return v ? "on" : "off";
  return `${v}${f.unit && f.unit !== "$" ? f.unit : ""}`.replace(/^(\d)/, f.unit === "$" ? "$$1" : "$1");
};

/** Browser-side twin of the server's validation (same bounds come from the schema). */
export function validateField(f: SchemaField, v: Json): string | null {
  if (v === null || v === undefined) return f.nullable ? null : "A value is required.";
  switch (f.control) {
    case "slider":
    case "number":
      if (typeof v !== "number" || Number.isNaN(v)) return "Enter a number.";
      if (f.min !== null && v < f.min) return `Must be at least ${f.min}${f.unit === "$" ? "" : f.unit}.`;
      if (f.max !== null && v > f.max) return `Must be at most ${f.max}${f.unit === "$" ? "" : f.unit}.`;
      return null;
    case "time":
      return typeof v === "string" && /^([01]\d|2[0-3]):[0-5]\d$/.test(v) ? null : "Use 24-hour HH:MM, e.g. 14:45.";
    case "select":
      return f.options.some((o) => o.value === v) ? null : "Pick one of the options.";
    case "multiselect":
      return Array.isArray(v) && v.length > 0 ? null : "Pick at least one.";
    default:
      return null;
  }
}

// ---------------------------------------------------------------- one control
function Control({ f, value, onChange, disabled, error }: { f: SchemaField; value: Json; onChange: (v: Json) => void; disabled: boolean; error: boolean }) {
  const off = value === null;
  switch (f.control) {
    case "slider":
      return (
        <div className="flex items-center gap-3">
          {f.nullable && <Toggle checked={!off} onChange={(on) => onChange(on ? (f.recommended as number | null) ?? f.min ?? 0 : null)} label={`${f.label} on or off`} disabled={disabled} />}
          {off ? <span className="text-xs text-muted">Off</span> : (
            <div className="min-w-0 flex-1"><SliderInput label={f.label} value={value as number} onChange={onChange} min={f.min ?? 0} max={f.max ?? 100} step={f.step ?? 1} unit={f.unit} disabled={disabled} invalid={error} /></div>
          )}
        </div>
      );
    case "number":
      return (
        <div className="flex items-center gap-2">
          {f.nullable && <Toggle checked={!off} onChange={(on) => onChange(on ? (f.recommended as number | null) ?? f.min ?? 0 : null)} label={`${f.label} on or off`} disabled={disabled} />}
          {off ? <span className="text-xs text-muted">No limit</span> : (
            <>
              <TextInput type="number" aria-label={f.label} aria-invalid={error || undefined} className={`max-w-[160px] ${error ? "!border-down" : ""}`} disabled={disabled}
                min={f.min ?? undefined} max={f.max ?? undefined} step={f.step ?? "any"} value={Number.isFinite(value as number) ? String(value) : ""}
                onChange={(e) => onChange(e.target.value === "" ? NaN : Number(e.target.value))} />
              {f.unit && <span className="text-xs text-muted">{f.unit}</span>}
            </>
          )}
        </div>
      );
    case "switch":
      return <Toggle checked={Boolean(value)} onChange={onChange} label={f.label} disabled={disabled} />;
    case "select":
      return <Select aria-label={f.label} className="max-w-sm" value={String(value)} disabled={disabled} onChange={(e) => onChange(e.target.value)}>{f.options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}</Select>;
    case "multiselect": {
      const cur = (value as string[]) ?? [];
      return (
        <div className="flex flex-wrap gap-x-4 gap-y-1.5" role="group" aria-label={f.label}>
          {f.options.map((o) => (
            <label key={o.value} className="flex items-center gap-1.5 text-[13px]">
              <input type="checkbox" disabled={disabled} checked={cur.includes(o.value)} onChange={(e) => onChange(e.target.checked ? [...cur, o.value] : cur.filter((x) => x !== o.value))} />{o.label}
            </label>
          ))}
        </div>
      );
    }
    case "time":
      return <input type="time" aria-label={f.label} aria-invalid={error || undefined} disabled={disabled} value={String(value ?? "")} onChange={(e) => onChange(e.target.value)} className={`num rounded-lg border bg-bg px-2.5 py-1.5 text-[13px] focus:border-accent ${error ? "border-down" : "border-line"}`} />;
    default:
      return <TextInput aria-label={f.label} className="max-w-sm" disabled={disabled} value={Array.isArray(value) ? value.join(", ") : String(value ?? "")} onChange={(e) => onChange(e.target.value)} />;
  }
}

// ---------------------------------------------------------------- page
export default function SettingsPage() {
  const { status, reload, refresh, version } = useApp();
  const toast = useToast();
  const confirm = useConfirm();
  const { data: schema, error, reload: reloadSchema } = usePoll_(version);
  const [cat, setCat] = usePref<string>("settings-cat", "strategy");
  const [draft, setDraft] = useState<Record<string, Json>>({});
  const [serverErrors, setServerErrors] = useState<Record<string, string>>({});
  const [effects, setEffects] = useState<Effect[]>([]);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<string | null>(null);
  const [justSaved, setJustSaved] = useState<Set<string>>(new Set());
  const [q, setQ] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  const simple = status?.ui.simple_mode ?? true;

  const fields = schema?.fields ?? [];
  const byPath = useMemo(() => new Map(fields.map((f) => [f.path, f])), [fields]);
  const valueOf = (f: SchemaField): Json => (f.path in draft ? draft[f.path] : f.value);
  const clientErrors = useMemo(() => {
    const out: Record<string, string> = {};
    for (const [path, v] of Object.entries(draft)) {
      const f = byPath.get(path);
      const m = f && validateField(f, v);
      if (m) out[path] = m;
    }
    return out;
  }, [draft, byPath]);
  const errors = { ...serverErrors, ...clientErrors };
  const dirty = Object.keys(draft).length;
  const invalid = Object.keys(clientErrors).length > 0;

  // live "what will this do?" preview while the user edits (the server also validates it)
  useEffect(() => {
    if (!dirty || invalid) { setEffects([]); return; }
    const t = setTimeout(() => {
      api.previewChanges(draft).then((r) => {
        setEffects(r.effects);
        setServerErrors(Object.fromEntries(r.errors.map((e: ApiFieldError) => [e.path, e.message])));
      }).catch(() => undefined);
    }, 350);
    return () => clearTimeout(t);
  }, [draft, dirty, invalid]);

  const edit = (f: SchemaField, v: Json) => {
    setServerErrors((e) => { const { [f.path]: _drop, ...rest } = e; return rest; });
    setDraft((d) => {
      if (sameValue(v, f.value)) { const { [f.path]: _drop, ...rest } = d; return rest; }
      return { ...d, [f.path]: v };
    });
  };

  const save = useCallback(async () => {
    setSaving(true);
    try {
      const res = await api.saveChanges(draft);
      setJustSaved(new Set(Object.keys(draft)));
      setTimeout(() => setJustSaved(new Set()), 3000);
      setDraft({}); setServerErrors({}); setEffects([]);
      setSavedAt(res.saved_at);
      toast(res.effects.some((e) => e.effect === "next_session") ? "Saved. Some changes take effect next session." : "Settings saved");
      reloadSchema(); refresh();
    } catch (e) {
      if (e instanceof ApiError && e.fieldErrors.length) setServerErrors(Object.fromEntries(e.fieldErrors.map((x) => [x.path, x.message])));
      toast(e instanceof Error ? e.message : String(e), "error");
    } finally {
      setSaving(false);
    }
  }, [draft, reloadSchema, refresh, toast]);

  const resetTo = async (category?: string) => {
    const what = category ? schema?.categories.find((c) => c.id === category)?.label : "everything";
    if (!(await confirm({ title: `Reset ${what} to the recommended settings?`, body: "Your trading mode and data source are kept. Open positions keep the plan they were opened with. Unsaved edits here are discarded.", confirmLabel: "Reset to recommended" }))) return;
    try { await api.resetSettings(category); toast("Recommended settings restored"); setDraft({}); setServerErrors({}); reloadSchema(); refresh(); } catch (e) { toast(e instanceof Error ? e.message : String(e), "error"); }
  };
  const exportSettings = async () => {
    try {
      const data = await api.exportSettings();
      const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
      const a = document.createElement("a"); a.href = url; a.download = "marketwatchbot_settings.json"; a.click(); URL.revokeObjectURL(url);
      toast("Settings exported (no keys or account data included)");
    } catch (e) { toast(e instanceof Error ? e.message : String(e), "error"); }
  };
  const importSettings = async (file: File) => {
    try {
      const payload = JSON.parse(await readText(file));
      const dry = await api.importSettings(payload, false);
      const ok = await confirm({
        title: dry.n_changes ? `Apply ${dry.n_changes} change(s) from this file?` : "Nothing would change",
        body: dry.n_changes ? <ul className="list-disc space-y-1 pl-5">{dry.effects.slice(0, 8).map((e) => <li key={e.path}><b>{e.label}</b>: {e.message}</li>)}{dry.effects.length > 8 && <li>...and {dry.effects.length - 8} more</li>}</ul> : "The file matches your current settings.",
        confirmLabel: dry.n_changes ? "Import settings" : "OK",
      });
      if (ok && dry.n_changes) { await api.importSettings(payload, true); toast("Settings imported"); setDraft({}); reloadSchema(); refresh(); }
    } catch (e) { toast(e instanceof SyntaxError ? "That file is not valid JSON." : e instanceof Error ? e.message : String(e), "error"); }
    if (fileRef.current) fileRef.current.value = "";
  };
  const setSimple = async (on: boolean) => {
    try { await api.saveChanges({ "ui.simple_mode": on }); reload(); } catch (e) { toast(e instanceof Error ? e.message : String(e), "error"); }
  };
  const pickMode = async (id: string, label: string) => {
    if (!schema || schema.mode.active === id) return;
    const pos = schema.open_positions;
    if (!(await confirm({ title: `Switch to ${label}?`, body: pos ? `New trades use ${label} rules. Your ${pos} open position(s) keep their original plan; history is unchanged.` : `New trades use ${label} rules. History is unchanged.`, confirmLabel: "Switch mode" }))) return;
    try { const r = await api.setMode(id); toast(r.mode.message); setDraft((d) => Object.fromEntries(Object.entries(d).filter(([k]) => !k.startsWith("mode.") && !k.startsWith("risk.")))); reloadSchema(); refresh(); }
    catch (e) { toast(e instanceof Error ? e.message : String(e), "error"); }
  };

  if (error && !schema) return <ErrorState message={error} onRetry={reloadSchema} />;
  if (!schema) return <Loading rows={8} label="Loading settings" />;

  const lockedNow = schema.session_running || schema.open_positions > 0;
  const needle = q.trim().toLowerCase();
  const visible = (f: SchemaField) => f.path !== "trading.mode" && (simple ? !f.advanced : true) && (needle ? (f.label + f.help + f.group).toLowerCase().includes(needle) : f.category === cat);
  const shown = fields.filter(visible);
  const groups = [...new Set(shown.map((f) => (needle ? schema.categories.find((c) => c.id === f.category)?.label + " · " + f.group : f.group)))];
  const differing = fields.filter((f) => f.path !== "trading.mode" && !f.is_recommended);
  const catDiff = (id: string) => differing.filter((f) => f.category === id).length;
  const cur = schema.categories.find((c) => c.id === cat)!;

  return (
    <div className="space-y-4">
      {differing.length === 0 ? (
        <div role="status" className="flex items-center gap-2 rounded-lg border border-up/30 bg-up/10 px-4 py-2.5 text-[13px] text-up"><span aria-hidden>✓</span>You are using the recommended settings for {schema.mode.label}. Nothing here needs changing.</div>
      ) : (
        <div role="status" className="flex flex-wrap items-center gap-3 rounded-lg border border-line bg-panel px-4 py-2.5 text-[13px]"><span>{differing.length} setting(s) differ from the recommended defaults.</span><Button onClick={() => resetTo(undefined)}>Reset all to recommended</Button></div>
      )}

      <div className="flex flex-wrap items-center gap-3">
        <div className="w-full sm:w-64"><TextInput aria-label="Search settings" placeholder="Search settings" value={q} onChange={(e) => setQ(e.target.value)} /></div>
        <label className="flex items-center gap-2 text-[13px]"><Toggle checked={!simple} onChange={(on) => setSimple(!on)} label="Show advanced settings" />Show advanced settings</label>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <span className="text-xs text-muted" role="status" data-testid="save-indicator">{saving ? "Saving..." : dirty ? `${dirty} unsaved change(s)` : savedAt || schema.saved_at ? `All changes saved ✓` : "No changes"}</span>
          <input ref={fileRef} type="file" accept="application/json,.json" className="sr-only" aria-label="Import settings file" onChange={(e) => e.target.files?.[0] && importSettings(e.target.files[0])} />
          <Button onClick={() => fileRef.current?.click()}>Import</Button>
          <Button onClick={exportSettings}>Export</Button>
        </div>
      </div>

      <div className="grid items-start gap-4 lg:grid-cols-[220px_1fr]">
        <nav aria-label="Settings categories" className="flex gap-1 overflow-x-auto lg:flex-col lg:overflow-visible">
          {schema.categories.map((c) => (
            <button key={c.id} onClick={() => { setCat(c.id); setQ(""); }} aria-current={!needle && cat === c.id ? "page" : undefined}
              className={`flex shrink-0 items-center justify-between gap-2 rounded-lg px-3 py-2 text-left text-[13px] transition ${!needle && cat === c.id ? "bg-accent/10 font-medium text-accent" : "text-muted hover:bg-panel2 hover:text-ink"}`}>
              {c.label}{catDiff(c.id) > 0 && <span className="num rounded-full bg-panel2 px-1.5 text-[10px] text-muted" title="differs from recommended">{catDiff(c.id)}</span>}
            </button>
          ))}
        </nav>

        <div className="min-w-0 space-y-4">
          {!needle && (
            <Card title={cur.label} right={<Button variant="ghost" onClick={() => resetTo(cat)}>Reset this section</Button>}>
              <p className="text-[13px] text-muted">{cur.blurb}</p>
              {cat === "strategy" && (
                <div role="radiogroup" aria-label="Trading mode" className="mt-4 grid gap-3 md:grid-cols-2">
                  {schema.mode.modes.map((m) => (
                    <button key={m.id} role="radio" aria-checked={schema.mode.active === m.id} onClick={() => pickMode(m.id, m.label)}
                      className={`rounded-xl border p-4 text-left transition ${schema.mode.active === m.id ? "border-accent bg-accent/10" : "border-line hover:border-muted/60"}`}>
                      <div className="flex items-center gap-2 text-[14px] font-semibold">{m.label}{m.recommended && <Badge color="accent">recommended</Badge>}{schema.mode.active === m.id && <Badge color="up">active</Badge>}</div>
                      <p className="mt-1 text-xs leading-relaxed text-muted">{m.summary}</p>
                    </button>
                  ))}
                </div>
              )}
            </Card>
          )}

          {shown.length === 0 && <Card><p className="text-[13px] text-muted">{needle ? "No settings match your search." : simple ? "This section only has advanced settings. Turn on “Show advanced settings” to see them." : "Nothing here."}</p></Card>}

          {groups.map((g) => (
            <Card key={g} title={g}>
              <div className="divide-y divide-line">
                {shown.filter((f) => (needle ? schema.categories.find((c) => c.id === f.category)?.label + " · " + f.group : f.group) === g).map((f) => {
                  const v = valueOf(f);
                  const locked = f.effect === "idle_only" && lockedNow;
                  const err = errors[f.path];
                  const changed = f.path in draft;
                  return (
                    <div key={f.path} className="grid gap-2 py-3 first:pt-0 last:pb-0 md:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)] md:gap-6" data-testid={`field-${f.path}`}>
                      <div>
                        <div className="flex flex-wrap items-center gap-2 text-[13px] font-medium">
                          {f.label}<Tip text={`${f.help} ${f.effect_text}`} label={`About ${f.label}`} />
                          {changed && <Badge color="info">edited</Badge>}
                          {justSaved.has(f.path) && <span className="text-xs text-up" role="status">Saved ✓</span>}
                          {!f.is_recommended && !changed && <Badge color="muted" title={`Recommended: ${display(f, f.recommended)}`}>custom</Badge>}
                        </div>
                        <p className="mt-0.5 text-xs leading-snug text-muted">{f.help}</p>
                        <p className="mt-0.5 text-[11px] text-muted/80">{f.effect_text}</p>
                      </div>
                      <div className="min-w-0">
                        <Control f={f} value={v} onChange={(nv) => edit(f, nv)} disabled={locked} error={Boolean(err)} />
                        {locked && <p className="mt-1 text-xs text-warn">Locked while a session runs or positions are open.</p>}
                        {err && <p role="alert" className="mt-1 text-xs text-down">{err}</p>}
                        {!sameValue(v, f.recommended) && !err && (
                          <button type="button" className="mt-1 text-[11px] text-accent hover:underline" onClick={() => edit(f, f.recommended)}>Use recommended ({display(f, f.recommended)})</button>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            </Card>
          ))}
        </div>
      </div>

      {dirty > 0 && (
        <div className="sticky bottom-16 z-20 md:bottom-3" role="region" aria-label="Unsaved changes">
          <div className="mx-auto max-w-3xl rounded-xl border border-accent/50 bg-panel p-3 shadow-2xl">
            <div className="flex flex-wrap items-center gap-3">
              <span className="text-[13px] font-medium">{dirty} unsaved change(s)</span>
              {invalid && <span className="text-xs text-down">Fix the highlighted fields to save.</span>}
              <div className="ml-auto flex gap-2">
                <Button variant="ghost" onClick={() => { setDraft({}); setServerErrors({}); }}>Discard</Button>
                <Button variant="primary" onClick={save} disabled={saving || invalid}>{saving ? "Saving..." : "Save changes"}</Button>
              </div>
            </div>
            {effects.length > 0 && (
              <ul className="mt-2 max-h-40 space-y-1 overflow-auto border-t border-line pt-2 text-xs text-muted" data-testid="effects">
                {effects.map((e) => <li key={e.path} className={e.blocked ? "text-down" : ""}><b className="text-ink">{e.label}:</b> {e.message}</li>)}
              </ul>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// small wrapper so the page reloads its schema when something elsewhere changes it
import { usePoll } from "../hooks";
function usePoll_(version: number) {
  return usePoll<SettingsSchema>(api.settingsSchema, null, [version]);
}
