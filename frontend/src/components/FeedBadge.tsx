import type { Feed } from "../types";
import { Badge } from "./ui";

/** Never present anything but a confirmed real-time feed as real-time. */
export function FeedBadge({ feed, compact = false }: { feed: Feed | undefined; compact?: boolean }) {
  if (!feed) return null;
  if (!feed.available) return <Badge color="down" title={feed.warnings.join(" ")}>NO DATA FEED</Badge>;
  const map = {
    synthetic: { color: "warn" as const, text: "SYNTHETIC" },
    historical: { color: "info" as const, text: "HISTORICAL REPLAY" },
    delayed: { color: "warn" as const, text: `DELAYED${feed.delay_seconds ? ` ~${Math.round(feed.delay_seconds / 60)} MIN` : ""}` },
    realtime: { color: "up" as const, text: "REAL-TIME" },
  }[feed.kind];
  return (
    <Badge color={map.color} title={`${feed.label}${feed.warnings.length ? " - " + feed.warnings.join(" ") : ""}`}>
      <span className={`h-1.5 w-1.5 rounded-full bg-current ${feed.kind === "realtime" ? "pulse-dot" : ""}`} />
      {compact ? map.text : `${map.text} - ${feed.provider}`}
    </Badge>
  );
}
