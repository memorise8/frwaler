"use client";

interface ScoreGaugeProps {
  score: number;
  size?: number;
}

function getZoneColor(score: number): string {
  if (score >= 0.75) return "#10b981"; // green
  if (score >= 0.55) return "#f59e0b"; // amber
  return "#ef4444"; // red
}

function getStatusLabel(score: number): string {
  if (score >= 0.75) return "사용 가능";
  if (score >= 0.55) return "주의";
  return "부적합";
}

function getStatusEmoji(score: number): string {
  if (score >= 0.75) return "✅";
  if (score >= 0.55) return "⚠️";
  return "❌";
}

export default function ScoreGauge({ score, size = 200 }: ScoreGaugeProps) {
  const cx = size / 2;
  const cy = size / 2;
  const r = size * 0.38;
  const strokeWidth = size * 0.07;

  // Arc helpers — 180° arc from left (180°) to right (0°) going through top
  const toRad = (deg: number) => (deg * Math.PI) / 180;

  const arcPath = (startDeg: number, endDeg: number, radius: number) => {
    const start = {
      x: cx + radius * Math.cos(toRad(startDeg)),
      y: cy + radius * Math.sin(toRad(startDeg)),
    };
    const end = {
      x: cx + radius * Math.cos(toRad(endDeg)),
      y: cy + radius * Math.sin(toRad(endDeg)),
    };
    const largeArc = Math.abs(endDeg - startDeg) > 180 ? 1 : 0;
    return `M ${start.x} ${start.y} A ${radius} ${radius} 0 ${largeArc} 1 ${end.x} ${end.y}`;
  };

  // Zones on 180° arc: 180° → 0° (going counterclockwise through top)
  // score 0=180°, score 1=0°, needle angle = 180 - score*180
  const failEnd = 180 - 0.55 * 180; // 81°
  const cautionEnd = 180 - 0.75 * 180; // 45°

  const needleDeg = 180 - score * 180;
  const needleLen = r * 0.82;
  const needleX = cx + needleLen * Math.cos(toRad(needleDeg));
  const needleY = cy + needleLen * Math.sin(toRad(needleDeg));

  const color = getZoneColor(score);
  const label = getStatusLabel(score);
  const emoji = getStatusEmoji(score);

  const trackRadius = r;
  const sw = strokeWidth;

  return (
    <div className="flex flex-col items-center gap-2">
      <svg
        width={size}
        height={size * 0.62}
        viewBox={`0 0 ${size} ${size * 0.62}`}
        style={{ overflow: "visible" }}
        aria-label={`Score gauge: ${Math.round(score * 100)}`}
      >
        {/* Background track */}
        <path
          d={arcPath(180, 0, trackRadius)}
          fill="none"
          stroke="rgba(255,255,255,0.06)"
          strokeWidth={sw}
          strokeLinecap="round"
        />

        {/* Red zone: 180° → failEnd */}
        <path
          d={arcPath(180, failEnd, trackRadius)}
          fill="none"
          stroke="rgba(239,68,68,0.35)"
          strokeWidth={sw}
          strokeLinecap="butt"
        />

        {/* Amber zone: failEnd → cautionEnd */}
        <path
          d={arcPath(failEnd, cautionEnd, trackRadius)}
          fill="none"
          stroke="rgba(245,158,11,0.35)"
          strokeWidth={sw}
          strokeLinecap="butt"
        />

        {/* Green zone: cautionEnd → 0° */}
        <path
          d={arcPath(cautionEnd, 0, trackRadius)}
          fill="none"
          stroke="rgba(16,185,129,0.35)"
          strokeWidth={sw}
          strokeLinecap="butt"
        />

        {/* Filled arc up to score */}
        <path
          d={arcPath(180, needleDeg, trackRadius)}
          fill="none"
          stroke={color}
          strokeWidth={sw * 0.7}
          strokeLinecap="round"
          style={{ filter: `drop-shadow(0 0 6px ${color}80)` }}
        />

        {/* Zone tick marks */}
        {[0, 0.55, 0.75, 1].map((v) => {
          const tickDeg = 180 - v * 180;
          const inner = trackRadius - sw * 0.9;
          const outer = trackRadius + sw * 0.9;
          return (
            <line
              key={v}
              x1={cx + inner * Math.cos(toRad(tickDeg))}
              y1={cy + inner * Math.sin(toRad(tickDeg))}
              x2={cx + outer * Math.cos(toRad(tickDeg))}
              y2={cy + outer * Math.sin(toRad(tickDeg))}
              stroke="rgba(255,255,255,0.2)"
              strokeWidth={1.5}
            />
          );
        })}

        {/* Needle */}
        <line
          x1={cx}
          y1={cy}
          x2={needleX}
          y2={needleY}
          stroke={color}
          strokeWidth={size * 0.025}
          strokeLinecap="round"
          style={{ filter: `drop-shadow(0 0 4px ${color})` }}
        />

        {/* Needle pivot */}
        <circle
          cx={cx}
          cy={cy}
          r={size * 0.035}
          fill={color}
          style={{ filter: `drop-shadow(0 0 6px ${color})` }}
        />

        {/* Score text */}
        <text
          x={cx}
          y={cy - r * 0.18}
          textAnchor="middle"
          fill="var(--text-primary)"
          fontSize={size * 0.18}
          fontWeight="700"
          fontFamily="'DM Mono', monospace"
        >
          {Math.round(score * 100)}
        </text>

        {/* /100 sub-label */}
        <text
          x={cx}
          y={cy + r * 0.08}
          textAnchor="middle"
          fill="var(--text-dim)"
          fontSize={size * 0.075}
          fontFamily="'DM Mono', monospace"
        >
          / 100
        </text>
      </svg>

      {/* Status label below gauge */}
      <div
        className="flex items-center gap-2"
        style={{
          color,
          fontWeight: 600,
          fontSize: size * 0.075,
          letterSpacing: "0.04em",
        }}
      >
        <span style={{ fontSize: size * 0.09 }}>{emoji}</span>
        <span>{label}</span>
      </div>
    </div>
  );
}
