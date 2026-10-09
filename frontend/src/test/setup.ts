import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  window.location.hash = "";
});

// jsdom has no ResizeObserver (recharts / charts need it) and no canvas (lightweight-charts).
class RO {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(globalThis as unknown as { ResizeObserver: typeof RO }).ResizeObserver = RO;

vi.mock("lightweight-charts", () => {
  const series = () => ({ setData: vi.fn(), setMarkers: vi.fn(), priceScale: () => ({ applyOptions: vi.fn() }), createPriceLine: vi.fn(() => ({})), removePriceLine: vi.fn() });
  return {
    ColorType: { Solid: "solid" },
    CrosshairMode: { Normal: 0 },
    createChart: vi.fn(() => ({
      addCandlestickSeries: series, addHistogramSeries: series, addLineSeries: series, addAreaSeries: series,
      timeScale: () => ({ fitContent: vi.fn() }), applyOptions: vi.fn(), remove: vi.fn(),
    })),
  };
});
