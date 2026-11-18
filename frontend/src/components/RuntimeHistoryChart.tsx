import { useId, useMemo } from 'react'
import type { RuntimeSnapshot } from '../api/types'
import { formatApiDateTime } from '../utils/date'
import {
  buildMetricSegments,
  canDrawTimeline,
  countPlottablePoints,
  toTimedSamples,
  type ChartPoint,
  type ChartSegment,
} from '../utils/runtimeHistoryChart'

type RuntimeHistoryChartProps = {
  items: RuntimeSnapshot[]
}

type SeriesSpec = {
  id: string
  label: string
  unit: string
  colorVar: string
  segments: ChartSegment[]
}

const PAD = { top: 12, right: 12, bottom: 28, left: 40 }
const WIDTH = 640
const HEIGHT = 160
const INNER_W = WIDTH - PAD.left - PAD.right
const INNER_H = HEIGHT - PAD.top - PAD.bottom

function extent(segments: ChartSegment[]): {
  tMin: number
  tMax: number
  vMin: number
  vMax: number
} | null {
  let tMin = Infinity
  let tMax = -Infinity
  let vMin = Infinity
  let vMax = -Infinity
  let any = false
  for (const seg of segments) {
    for (const p of seg) {
      any = true
      tMin = Math.min(tMin, p.t)
      tMax = Math.max(tMax, p.t)
      vMin = Math.min(vMin, p.value)
      vMax = Math.max(vMax, p.value)
    }
  }
  if (!any) return null
  if (tMin === tMax) {
    tMin -= 1
    tMax += 1
  }
  if (vMin === vMax) {
    vMin = Math.min(0, vMin)
    vMax = vMax === 0 ? 1 : vMax * 1.1
  }
  return { tMin, tMax, vMin, vMax }
}

function project(
  p: ChartPoint,
  ext: { tMin: number; tMax: number; vMin: number; vMax: number },
): { x: number; y: number } {
  const x =
    PAD.left + ((p.t - ext.tMin) / (ext.tMax - ext.tMin)) * INNER_W
  const y =
    PAD.top +
    (1 - (p.value - ext.vMin) / (ext.vMax - ext.vMin)) * INNER_H
  return { x, y }
}

function pathForSegment(
  seg: ChartSegment,
  ext: { tMin: number; tMax: number; vMin: number; vMax: number },
): string {
  return seg
    .map((p, i) => {
      const { x, y } = project(p, ext)
      return `${i === 0 ? 'M' : 'L'}${x.toFixed(2)} ${y.toFixed(2)}`
    })
    .join(' ')
}

function formatAxisValue(value: number, unit: string): string {
  if (unit === '%') {
    return `${value >= 10 || value === 0 ? value.toFixed(0) : value.toFixed(1)}%`
  }
  return Number.isInteger(value) ? String(value) : value.toFixed(1)
}

function Sparkline({
  title,
  description,
  unit,
  color,
  segments,
}: {
  title: string
  description: string
  unit: string
  color: string
  segments: ChartSegment[]
}) {
  const titleId = useId()
  const descId = useId()
  const ext = extent(segments)
  if (!ext || !canDrawTimeline(segments)) return null

  const yTicks = [ext.vMin, (ext.vMin + ext.vMax) / 2, ext.vMax]
  const xTicks = [ext.tMin, ext.tMax]

  return (
    <figure className="runtime-history-chart__figure">
      <figcaption id={titleId} className="runtime-history-chart__caption">
        {title}
      </figcaption>
      <p id={descId} className="runtime-history-chart__note">
        {description}
      </p>
      <svg
        className="runtime-history-chart__svg"
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        role="img"
        aria-labelledby={`${titleId} ${descId}`}
      >
        <rect
          className="runtime-history-chart__plot"
          x={PAD.left}
          y={PAD.top}
          width={INNER_W}
          height={INNER_H}
        />
        {yTicks.map((v, i) => {
          const y =
            PAD.top + (1 - (v - ext.vMin) / (ext.vMax - ext.vMin)) * INNER_H
          return (
            <g key={`y-${i}`}>
              <line
                className="runtime-history-chart__gridline"
                x1={PAD.left}
                x2={PAD.left + INNER_W}
                y1={y}
                y2={y}
              />
              <text
                className="runtime-history-chart__tick"
                x={PAD.left - 6}
                y={y + 3}
                textAnchor="end"
              >
                {formatAxisValue(v, unit)}
              </text>
            </g>
          )
        })}
        {xTicks.map((t, i) => {
          const x = PAD.left + ((t - ext.tMin) / (ext.tMax - ext.tMin)) * INNER_W
          return (
            <text
              key={`x-${i}`}
              className="runtime-history-chart__tick"
              x={x}
              y={HEIGHT - 8}
              textAnchor={i === 0 ? 'start' : 'end'}
            >
              {formatApiDateTime(new Date(t).toISOString())}
            </text>
          )
        })}
        {segments.map((seg, i) => (
          <path
            key={`seg-${i}`}
            className="runtime-history-chart__line"
            d={pathForSegment(seg, ext)}
            style={{ stroke: color }}
            fill="none"
          />
        ))}
        {segments.flatMap((seg, si) =>
          seg.map((p, pi) => {
            const { x, y } = project(p, ext)
            return (
              <circle
                key={`pt-${si}-${pi}`}
                className="runtime-history-chart__point"
                cx={x}
                cy={y}
                r={2.5}
                style={{ fill: color }}
              >
                <title>
                  {`${formatApiDateTime(p.sampledAt)} · ${formatAxisValue(p.value, unit)}`}
                </title>
              </circle>
            )
          }),
        )}
      </svg>
      <p className="runtime-history-chart__legend" aria-hidden="true">
        <span
          className="runtime-history-chart__swatch"
          style={{ background: color }}
        />
        {title} ({unit}) · 구간 {segments.length} · 점{' '}
        {countPlottablePoints(segments)}
      </p>
    </figure>
  )
}

export function RuntimeHistoryChart({ items }: RuntimeHistoryChartProps) {
  const samples = useMemo(() => toTimedSamples(items), [items])
  const series: SeriesSpec[] = useMemo(() => {
    const kv = buildMetricSegments(samples, 'kvPercent')
    const running = buildMetricSegments(samples, 'running')
    const waiting = buildMetricSegments(samples, 'waiting')
    return [
      {
        id: 'kv',
        label: 'KV 캐시 사용률',
        unit: '%',
        colorVar: 'var(--accent)',
        segments: kv,
      },
      {
        id: 'running',
        label: '처리 중 요청',
        unit: '건',
        colorVar: 'var(--ok)',
        segments: running,
      },
      {
        id: 'waiting',
        label: '대기 중 요청',
        unit: '건',
        colorVar: 'var(--warn)',
        segments: waiting,
      },
    ]
  }, [samples])

  const drawable = series.filter((s) => canDrawTimeline(s.segments))
  const timedCount = samples.length
  const excludedNoTime = items.length - timedCount

  if (items.length === 0) {
    return null
  }

  return (
    <div
      className="runtime-history-chart"
      data-testid="runtime-history-chart"
    >
      <h3 className="runtime-history-chart__heading">
        실측 시간 추이 (persisted history)
      </h3>
      <p className="panel-hint">
        DB에 저장된 snapshot만 그립니다. null·미수집은 잇지 않으며, runtime
        instance 변경·장시간 공백 구간은 선이 끊깁니다. 값은 보간·예측하지
        않습니다. 상세 수치는 아래 표를 사용하세요.
      </p>
      {excludedNoTime > 0 ? (
        <p className="runtime-history-chart__status" role="status">
          샘플 시각이 없는 행 {excludedNoTime}개는 차트에서 제외했고 표에는
          유지합니다.
        </p>
      ) : null}
      {drawable.length === 0 ? (
        <p className="empty-state" role="status">
          {timedCount < 2
            ? '차트용 실측 점이 2개 미만입니다. 단일/결측 샘플은 표로만 확인하세요.'
            : '그릴 수 있는 연속 실측 구간이 없습니다. null·결측·인스턴스 변경으로 선이 모두 단절되었을 수 있습니다. 표는 유지됩니다.'}
        </p>
      ) : (
        <div className="runtime-history-chart__grid">
          {drawable.map((s) => (
            <Sparkline
              key={s.id}
              title={s.label}
              description={`${s.label} 실측 추이. 단위 ${s.unit}. 결측·인스턴스 변경·장시간 공백은 선이 끊깁니다.`}
              unit={s.unit}
              color={s.colorVar}
              segments={s.segments}
            />
          ))}
        </div>
      )}
    </div>
  )
}
