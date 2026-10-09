import { chicagoTime, compact, money, num, pct, signedMoney, titleCase, tone } from "../format";

describe("format helpers", () => {
  it("formats money, signs and missing values", () => {
    expect(money(1234.5)).toBe("$1,234.50");
    expect(money(null)).toBe("-");
    expect(signedMoney(12.3)).toBe("+$12.30");
    expect(signedMoney(-12.3)).toBe("-$12.30");
    expect(money(Number.NaN)).toBe("-");
  });
  it("formats percentages and numbers", () => {
    expect(pct(3.456)).toBe("+3.46%");
    expect(pct(-1)).toBe("-1.00%");
    expect(pct(2, 1, false)).toBe("2.0%");
    expect(pct(undefined)).toBe("-");
    expect(num(2.5, 1)).toBe("2.5");
    expect(compact(1_250_000)).toBe("1.3M");
  });
  it("tones gains, losses and zero", () => {
    expect(tone(1)).toBe("text-up");
    expect(tone(-1)).toBe("text-down");
    expect(tone(0)).toBe("text-muted");
    expect(tone(null)).toBe("text-muted");
  });
  it("renders timestamps in America/Chicago, including across DST", () => {
    expect(chicagoTime("2026-10-08T13:30:00+00:00")).toBe("08:30"); // CDT (UTC-5)
    expect(chicagoTime("2026-01-15T14:30:00+00:00")).toBe("08:30"); // CST (UTC-6)
    expect(chicagoTime("garbage")).toBe("-");
    expect(titleCase("opening_momentum")).toBe("Opening Momentum");
  });
});
