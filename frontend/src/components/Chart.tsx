/**
 * A small time-series chart.
 *
 * It deliberately breaks the line when samples are far apart, and shades the
 * gap, instead of drawing a straight segment across it. A charting library
 * would happily connect the two points either side -- which would draw a
 * confident line through a period when the aircraft was not in contact. The
 * gaps are data.
 */

interface Series {
  label: string;
  color: string;
  /** Null values are holes, drawn as holes. */
  points: { t: number; v: number | null }[];
  unit?: string;
}

interface ChartProps {
  series: Series[];
  /** Seconds between samples beyond which the trace is broken. */
  gapThresholdS?: number;
  height?: number;
}

const PAD = { top: 8, right: 34, bottom: 14, left: 34 };

export function Chart({ series, gapThresholdS = 10, height = 112 }: ChartProps) {
  const all = series.flatMap((s) => s.points);
  const withValues = all.filter((p) => p.v !== null) as { t: number; v: number }[];

  if (withValues.length < 2) {
    return (
      <div className="table__empty" style={{ height }}>
        Not enough recorded telemetry to plot.
      </div>
    );
  }

  const width = 320; // viewBox units; the SVG scales to its container
  const tMin = Math.min(...all.map((p) => p.t));
  const tMax = Math.max(...all.map((p) => p.t));
  const vMin = Math.min(...withValues.map((p) => p.v));
  const vMax = Math.max(...withValues.map((p) => p.v));

  const tSpan = tMax - tMin || 1;
  const vSpan = vMax - vMin || 1;
  const plotW = width - PAD.left - PAD.right;
  const plotH = height - PAD.top - PAD.bottom;

  const x = (t: number) => PAD.left + ((t - tMin) / tSpan) * plotW;
  const y = (v: number) => PAD.top + plotH - ((v - vMin) / vSpan) * plotH;

  // Gap bands, derived from the first series (all series share a sample clock).
  const gaps: { from: number; to: number }[] = [];
  const reference = series[0]?.points ?? [];
  for (let i = 1; i < reference.length; i += 1) {
    const previous = reference[i - 1];
    const current = reference[i];
    if (!previous || !current) continue;
    if (current.t - previous.t > gapThresholdS) {
      gaps.push({ from: previous.t, to: current.t });
    }
  }

  const buildPath = (points: Series["points"]): string => {
    let path = "";
    let penDown = false;
    for (let i = 0; i < points.length; i += 1) {
      const point = points[i];
      if (!point || point.v === null) {
        penDown = false;
        continue;
      }
      const previous = points[i - 1];
      const broken =
        !penDown || !previous || previous.v === null || point.t - previous.t > gapThresholdS;
      path += `${broken ? "M" : "L"}${x(point.t).toFixed(1)} ${y(point.v).toFixed(1)}`;
      penDown = true;
    }
    return path;
  };

  return (
    <svg className="chart" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none">
      {/* gap bands first, so traces draw over them */}
      {gaps.map((gap, index) => (
        <rect
          key={index}
          className="chart__gap"
          x={x(gap.from)}
          y={PAD.top}
          width={Math.max(1, x(gap.to) - x(gap.from))}
          height={plotH}
        />
      ))}

      <line
        className="chart__grid"
        x1={PAD.left}
        x2={width - PAD.right}
        y1={PAD.top + plotH / 2}
        y2={PAD.top + plotH / 2}
      />
      <line
        className="chart__axis"
        x1={PAD.left}
        x2={width - PAD.right}
        y1={PAD.top + plotH}
        y2={PAD.top + plotH}
      />

      {series.map((s) => (
        <path
          key={s.label}
          d={buildPath(s.points)}
          fill="none"
          stroke={s.color}
          strokeWidth={1.4}
          strokeLinecap="round"
          vectorEffect="non-scaling-stroke"
        />
      ))}

      <text className="chart__label" x={2} y={PAD.top + 4}>
        {vMax.toFixed(0)}
      </text>
      <text className="chart__label" x={2} y={PAD.top + plotH}>
        {vMin.toFixed(0)}
      </text>
      {gaps.length > 0 ? (
        <text className="chart__label" x={PAD.left} y={height - 3} fill="var(--bad)">
          {gaps.length} link gap{gaps.length === 1 ? "" : "s"} — not bridged
        </text>
      ) : null}
    </svg>
  );
}
