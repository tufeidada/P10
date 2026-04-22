import {
  Radar,
  RadarChart,
  PolarGrid,
  PolarAngleAxis,
  ResponsiveContainer,
  Tooltip,
} from 'recharts';
import type { JudgmentDetail } from '../types';

interface RadarChartProps {
  current: JudgmentDetail;
  previous?: JudgmentDetail | null;
}

const DIRECTION_COLOR = {
  bullish: '#00d4aa',
  neutral: '#e3b341',
  bearish: '#f85149',
};

const DIRECTION_LABEL = {
  bullish: '看多',
  neutral: '中性',
  bearish: '看空',
};

export default function RadarChartComponent({ current, previous }: RadarChartProps) {
  const fillColor = DIRECTION_COLOR[current.direction] ?? '#8b949e';
  const dirLabel = DIRECTION_LABEL[current.direction] ?? '--';

  const data = [
    {
      axis: '技术面',
      current: current.technical_score ?? 0,
      previous: previous?.technical_score ?? 0,
    },
    {
      axis: '基本面',
      current: current.fundamental_score ?? 0,
      previous: previous?.fundamental_score ?? 0,
    },
    {
      axis: '资金面',
      current: current.flow_score ?? 0,
      previous: previous?.flow_score ?? 0,
    },
    {
      axis: '情绪面',
      current: current.sentiment_score ?? 0,
      previous: previous?.sentiment_score ?? 0,
    },
  ];

  const confidence = current.confidence * 100;
  const confColor = confidence >= 70 ? '#00d4aa' : confidence >= 50 ? '#e3b341' : '#f85149';

  return (
    <div className="bg-surface border border-border rounded p-3">
      <div className="flex items-center justify-between mb-2">
        <span className="text-text-muted font-mono text-xs uppercase tracking-wider">多维评分</span>
        <span className="font-mono text-xs text-text-muted">
          {current.judgment_date
            ? new Date(current.judgment_date).toLocaleDateString('zh-CN')
            : '--'}
        </span>
      </div>

      {/* Radar */}
      <div style={{ height: '180px' }}>
        <ResponsiveContainer width="100%" height="100%">
          <RadarChart data={data} margin={{ top: 10, right: 20, bottom: 10, left: 20 }}>
            <PolarGrid stroke="#21262d" />
            <PolarAngleAxis
              dataKey="axis"
              tick={{ fill: '#8b949e', fontSize: 10, fontFamily: '"IBM Plex Sans"' }}
            />
            <Tooltip
              contentStyle={{
                background: '#161b22',
                border: '1px solid #21262d',
                borderRadius: '4px',
                fontSize: '11px',
                fontFamily: '"JetBrains Mono"',
              }}
              itemStyle={{ color: '#e6edf3' }}
              labelStyle={{ color: '#8b949e' }}
            />
            {/* Previous (dashed outline only) */}
            {previous && (
              <Radar
                name="上次"
                dataKey="previous"
                stroke={fillColor}
                fill="transparent"
                strokeWidth={1}
                strokeDasharray="4 2"
                dot={false}
              />
            )}
            {/* Current (filled) */}
            <Radar
              name="当前"
              dataKey="current"
              stroke={fillColor}
              fill={fillColor}
              fillOpacity={0.25}
              strokeWidth={2}
              dot={{ fill: fillColor, r: 3 }}
            />
          </RadarChart>
        </ResponsiveContainer>
      </div>

      {/* Score summary */}
      <div className="flex items-center gap-4 mt-2 pt-2 border-t border-border">
        <div className="flex flex-col">
          <span className="text-text-muted font-mono" style={{ fontSize: '10px' }}>综合评分</span>
          <span
            className="font-mono font-bold"
            style={{ fontSize: '24px', color: fillColor, lineHeight: 1.2 }}
          >
            {current.composite_score.toFixed(1)}
          </span>
        </div>
        <div className="flex flex-col">
          <span className="text-text-muted font-mono" style={{ fontSize: '10px' }}>方向</span>
          <span className="font-mono font-bold text-sm" style={{ color: fillColor }}>
            {dirLabel}
          </span>
        </div>
        <div className="flex-1 flex flex-col">
          <span className="text-text-muted font-mono" style={{ fontSize: '10px' }}>
            置信度 {confidence.toFixed(0)}%
          </span>
          <div className="mt-1" style={{ height: '4px', background: '#21262d', borderRadius: '2px' }}>
            <div
              style={{
                width: `${confidence}%`,
                height: '100%',
                background: confColor,
                borderRadius: '2px',
                transition: 'width 0.4s ease',
              }}
            />
          </div>
        </div>
      </div>
    </div>
  );
}
