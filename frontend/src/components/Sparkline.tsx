/**
 * A tiny trend line for a drone card.
 *
 * Fed from recorded telemetry samples, so it shows what the aircraft actually
 * reported. Like the main chart, it breaks the line across a link gap rather
 * than drawing through it, and it renders nothing at all rather than a flat
 * line at zero when there is no data.
 */

interface SparklineProps {
  points: { t: number; v: number | null }[];
  color: string;
  /** Seconds between samples beyond which the trace is broken. */
  gapThresholdS?: number;
  /** Fixed range, when the metric has a natural one (battery: 0-100). */
  min?: number;
  max?: number;
  label?: string;
}

const W = 100;
const H = 26;

export function Sparkline({
  points,
  color,
  gapThresholdS = 15,
  min,
  max,
  label,
}: SparklineProps) {
  const real = points.filter((p): p is { t: number; v: number } => p.v !== null);

  if (real.length < 2) {
    return (
      <svg className="spark" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden="true">
        <text className="spark__empty" x="0" y={H / 2 + 3}>
          no recorded trend
        </text>
      </svg>
    );
  }

  const tMin = Math.min(...real.map((p) => p.t));
  const tMax = Math.max(...real.map((p) => p.t));
  const vMin = min ?? Math.min(...real.map((p) => p.v));
  const vMax = max ?? Math.max(...real.map((p) => p.v));

  const tSpan = tMax - tMin || 1;
  const vSpan = vMax - vMin || 1;

  const x = (t: number) => ((t - tMin) / tSpan) * W;
  const y = (v: number) => H - 2 - ((v - vMin) / vSpan) * (H - 4);

  // Build segments, splitting on holes and on gaps wider than the threshold.
  const segments: string[] = [];
  let current = "";
  for (let i = 0; i < points.length; i += 1) {
    const point = points[i];
    if (!point || point.v === null) {
      if (current) segments.push(current);
      current = "";
      continue;
    }
    const previous = points[i - 1];
    const broken = !current || !previous || previous.v === null || point.t - previous.t > gapThresholdS;
    if (broken) {
      if (current) segments.push(current);
      current = `M${x(point.t).toFixed(1)} ${y(point.v).toFixed(1)}`;
    } else {
      current += `L${x(point.t).toFixed(1)} ${y(point.v).toFixed(1)}`;
    }
  }
  if (current) segments.push(current);

  // Fill only under the longest continuous run, so a fragmented trace does not
  // read as a solid block of coverage.
  const longest = segments.reduce((a, b) => (b.length > a.length ? b : a), "");
  const area = longest
    ? `${longest}L${W} ${H}L0 ${H}Z`
    : "";

  return (
    <svg
      className="spark"
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={label}
    >
      {area ? <path className="spark__area" d={area} fill={color} /> : null}
      {segments.map((d, i) => (
        <path key={i} className="spark__line" d={d} stroke={color} />
      ))}
    </svg>
  );
}
