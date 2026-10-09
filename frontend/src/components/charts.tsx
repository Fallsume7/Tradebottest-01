import {
  ColorType, CrosshairMode, createChart,
  type IChartApi, type ISeriesApi, type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useRef } from "react";
import { CHICAGO } from "../format";
import type { Bar } from "../types";

function css(name: string, fallback: string): string {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

const chicagoFmt = new Intl.DateTimeFormat("en-US", { timeZone: CHICAGO, hour12: false, hour: "2-digit", minute: "2-digit" });
const chicagoDay = new Intl.DateTimeFormat("en-US", { timeZone: CHICAGO, month: "short", day: "2-digit" });

function baseOptions(el: HTMLElement) {
  return {
    width: el.clientWidth,
    height: el.clientHeight,
    layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: css("--muted", "#8b98a8"), fontFamily: "ui-monospace, Menlo, Consolas, monospace", fontSize: 11 },
    grid: { vertLines: { color: css("--border", "#212b37") + "55" }, horzLines: { color: css("--border", "#212b37") + "55" } },
    rightPriceScale: { borderColor: css("--border", "#212b37") },
    timeScale: {
      borderColor: css("--border", "#212b37"), timeVisible: true, secondsVisible: false,
      tickMarkFormatter: (t: number) => chicagoFmt.format(new Date(t * 1000)),
    },
    localization: { timeFormatter: (t: number) => `${chicagoDay.format(new Date(t * 1000))} ${chicagoFmt.format(new Date(t * 1000))} CT` },
    crosshair: { mode: CrosshairMode.Normal },
  } as const;
}

function useResize(chartRef: React.MutableRefObject<IChartApi | null>, host: React.RefObject<HTMLDivElement | null>) {
  useEffect(() => {
    const el = host.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => chartRef.current?.applyOptions({ width: el.clientWidth, height: el.clientHeight }));
    ro.observe(el);
    return () => ro.disconnect();
  }, [chartRef, host]);
}

/** Candles + volume + session VWAP. Times are rendered in America/Chicago. */
export function PriceChart({ bars, openTime, resetKey }: { bars: Bar[]; openTime: number; resetKey?: string }) {
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const candles = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volume = useRef<ISeriesApi<"Histogram"> | null>(null);
  const vwap = useRef<ISeriesApi<"Line"> | null>(null);
  const fitted = useRef<string | null>(null);

  useEffect(() => {
    const el = host.current;
    if (!el) return;
    const c = createChart(el, baseOptions(el));
    chart.current = c;
    fitted.current = null;
    const up = css("--up", "#34d399");
    const down = css("--down", "#f87171");
    candles.current = c.addCandlestickSeries({ upColor: up, downColor: down, borderVisible: false, wickUpColor: up, wickDownColor: down });
    volume.current = c.addHistogramSeries({ priceFormat: { type: "volume" }, priceScaleId: "" });
    volume.current.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    vwap.current = c.addLineSeries({ color: css("--info", "#60a5fa"), lineWidth: 2, priceLineVisible: false, lastValueVisible: false, title: "VWAP" });
    return () => {
      c.remove();
      chart.current = candles.current = volume.current = vwap.current = null;
    };
  }, []);
  useResize(chart, host);

  useEffect(() => {
    if (!candles.current || !volume.current || !vwap.current) return;
    const up = css("--up", "#34d399");
    const down = css("--down", "#f87171");
    candles.current.setData(bars.map((b) => ({ time: b.t as UTCTimestamp, open: b.o, high: b.h, low: b.l, close: b.c })));
    volume.current.setData(bars.map((b) => ({ time: b.t as UTCTimestamp, value: b.v, color: (b.c >= b.o ? up : down) + "88" })));
    let pv = 0, v = 0;
    const line: { time: UTCTimestamp; value: number }[] = [];
    for (const b of bars) {
      if (b.t < openTime) continue;
      pv += ((b.h + b.l + b.c) / 3) * b.v;
      v += b.v;
      if (v > 0) line.push({ time: b.t as UTCTimestamp, value: pv / v });
    }
    vwap.current.setData(line);
    candles.current.setMarkers(bars.some((b) => b.t === openTime) ? [{ time: openTime as UTCTimestamp, position: "aboveBar", color: css("--muted", "#8b98a8"), shape: "arrowDown", text: "open" }] : []);
    // fit once per symbol so live updates do not fight the user's zoom/scroll
    if (bars.length > 0 && fitted.current !== (resetKey ?? "")) {
      chart.current?.timeScale().fitContent();
      fitted.current = resetKey ?? "";
    }
  }, [bars, openTime, resetKey]);

  return <div ref={host} className="h-[460px] w-full" data-testid="price-chart" />;
}

export interface LinePoint { time: number; value: number }

export function EquityChart({ points, baseline, height = 260 }: { points: LinePoint[]; baseline?: number; height?: number }) {
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<ISeriesApi<"Area"> | null>(null);
  const baseLine = useRef<ReturnType<ISeriesApi<"Area">["createPriceLine"]> | null>(null);

  useEffect(() => {
    const el = host.current;
    if (!el) return;
    const c = createChart(el, baseOptions(el));
    chart.current = c;
    series.current = c.addAreaSeries({
      lineColor: css("--accent", "#2dd4bf"), topColor: css("--accent", "#2dd4bf") + "44", bottomColor: css("--accent", "#2dd4bf") + "00",
      lineWidth: 2, priceLineVisible: true,
    });
    return () => {
      c.remove();
      chart.current = series.current = null;
      baseLine.current = null;
    };
  }, []);
  useResize(chart, host);

  useEffect(() => {
    if (!series.current) return;
    // lightweight-charts needs strictly increasing unique times
    const seen = new Set<number>();
    const data = points.filter((p) => (seen.has(p.time) ? false : (seen.add(p.time), true))).sort((a, b) => a.time - b.time);
    series.current.setData(data.map((p) => ({ time: p.time as UTCTimestamp, value: p.value })));
    if (baseLine.current) {
      series.current.removePriceLine(baseLine.current);
      baseLine.current = null;
    }
    if (baseline !== undefined) {
      baseLine.current = series.current.createPriceLine({ price: baseline, color: css("--muted", "#8b98a8"), lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: "start" });
    }
    chart.current?.timeScale().fitContent();
  }, [points, baseline]);

  return <div ref={host} style={{ height }} className="w-full" data-testid="equity-chart" />;
}

export interface NamedLine { name: string; color: string; points: LinePoint[] }

/** Several equity curves on one axis (used to compare modes on identical data). */
export function CompareChart({ lines, height = 240 }: { lines: NamedLine[]; height?: number }) {
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);

  useEffect(() => {
    const el = host.current;
    if (!el) return;
    const c = createChart(el, baseOptions(el));
    chart.current = c;
    for (const l of lines) {
      const s = c.addLineSeries({ color: l.color, lineWidth: 2, priceLineVisible: false, title: l.name });
      const seen = new Set<number>();
      const data = l.points.filter((p) => (seen.has(p.time) ? false : (seen.add(p.time), true))).sort((a, b) => a.time - b.time);
      s.setData(data.map((p) => ({ time: p.time as UTCTimestamp, value: p.value })));
    }
    c.timeScale().fitContent();
    return () => {
      c.remove();
      chart.current = null;
    };
  }, [lines]);
  useResize(chart, host);
  return <div ref={host} style={{ height }} className="w-full" data-testid="compare-chart" />;
}
