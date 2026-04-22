import { useApi } from '../hooks/useApi';
import type { QualityTrackingData, AccuracyRow } from '../types';
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  Legend,
} from 'recharts';

// ─── Helpers ──────────────────────────────────────────────────────────────────

const DIR_COLOR: Record<string, string> = {
  bullish: '#00d4aa',
  neutral: '#e3b341',
  bearish: '#f85149',
  unknown: '#8b949e',
};

const DIR_LABEL: Record<string, string> = {
  bullish: '看多',
  neutral: '中性',
  bearish: '看空',
};

function pct(v: number | null): string {
  if (v == null) return 'N/A';
  return `${(v * 100).toFixed(1)}%`;
}

function ret(v: number | null): string {
  if (v == null) return 'N/A';
  const sign = v > 0 ? '+' : '';
  return `${sign}${(v * 100).toFixed(2)}%`;
}

// ─── Section header ────────────────────────────────────────────────────────────

function SectionHeader({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <div className="mb-3">
      <div className="font-mono text-xs uppercase tracking-wider text-text-muted">{title}</div>
      {subtitle && <div className="font-mono text-xs text-text-muted opacity-60 mt-0.5">{subtitle}</div>}
    </div>
  );
}

// ─── Accuracy table ────────────────────────────────────────────────────────────

function AccuracyTable({ rows, title, showVote = false }: { rows: AccuracyRow[]; title: string; showVote?: boolean }) {
  return (
    <div className="bg-surface border border-border rounded p-3">
      <SectionHeader title={title} subtitle="accuracy = 10日后实际涨跌与方向一致" />
      {rows.length === 0 ? (
        <div className="text-text-muted text-xs italic py-2">暂无数据</div>
      ) : (
        <table className="data-table w-full">
          <thead>
            <tr>
              <th>方向</th>
              <th>总计</th>
              <th>已评估</th>
              <th>准确率</th>
              <th>平均10日收益</th>
              {showVote && <th>Vote一致性</th>}
            </tr>
          </thead>
          <tbody>
            {rows.map(r => (
              <tr key={r.direction}>
                <td>
                  <div className="flex items-center gap-1.5">
                    <div className="w-1.5 h-1.5 rounded-full" style={{ background: DIR_COLOR[r.direction] ?? '#8b949e' }} />
                    <span className="font-mono text-xs" style={{ color: DIR_COLOR[r.direction] ?? '#8b949e' }}>
                      {DIR_LABEL[r.direction] ?? r.direction}
                    </span>
                  </div>
                </td>
                <td><span className="font-mono text-xs">{r.total}</span></td>
                <td>
                  <span className="font-mono text-xs text-text-muted">
                    {r.evaluated} {r.evaluated < r.total ? `(${((r.evaluated / r.total) * 100).toFixed(0)}%)` : ''}
                  </span>
                </td>
                <td>
                  <span
                    className="font-mono text-xs font-semibold"
                    style={{
                      color: r.accuracy == null ? '#8b949e' : r.accuracy >= 0.6 ? '#00d4aa' : r.accuracy >= 0.4 ? '#e3b341' : '#f85149',
                    }}
                  >
                    {pct(r.accuracy)}
                  </span>
                </td>
                <td>
                  <span
                    className="font-mono text-xs"
                    style={{
                      color: r.avg_ret_10d == null ? '#8b949e' : r.avg_ret_10d > 0 ? '#f85149' : r.avg_ret_10d < 0 ? '#00d4aa' : '#8b949e',
                    }}
                  >
                    {ret(r.avg_ret_10d)}
                  </span>
                </td>
                {showVote && (
                  <td>
                    <span
                      className="font-mono text-xs"
                      style={{
                        color: r.avg_vote_consensus == null ? '#8b949e' : r.avg_vote_consensus >= 0.67 ? '#00d4aa' : '#e3b341',
                      }}
                    >
                      {r.avg_vote_consensus != null ? pct(r.avg_vote_consensus) : 'N/A'}
                    </span>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

// ─── Divergence block ─────────────────────────────────────────────────────────

function DivergenceBlock({ data }: { data: QualityTrackingData }) {
  const { divergence, divergence_trend } = data;
  const total = divergence.total;

  const barData = [
    {
      name: 'LLM激进',
      label: 'LLM看多/规则中性空',
      count: divergence.llm_more_aggressive,
      ratio: divergence.llm_aggressive_ratio,
      color: '#f97316',
    },
    {
      name: 'LLM保守',
      label: '规则看多/LLM中性空',
      count: divergence.llm_more_conservative,
      ratio: divergence.llm_conservative_ratio,
      color: '#a5b4fc',
    },
    {
      name: '完全一致',
      label: '规则与LLM方向相同',
      count: divergence.fully_aligned,
      ratio: divergence.aligned_ratio,
      color: '#00d4aa',
    },
  ];

  const trendData = [...divergence_trend].reverse().slice(-14).map(p => ({
    date: p.date.slice(5),
    激进: p.llm_aggressive,
    保守: p.llm_conservative,
    一致: p.aligned,
  }));

  return (
    <div className="bg-surface border border-border rounded p-3">
      <SectionHeader
        title="规则 vs LLM 分歧分析"
        subtitle={`共 ${total} 条有效判断（LLM非unknown）`}
      />
      {total === 0 ? (
        <div className="text-text-muted text-xs italic py-2">暂无数据 — 需要先完成分析并存储 llm_direction</div>
      ) : (
        <div className="space-y-3">
          {/* Summary cards */}
          <div className="grid grid-cols-3 gap-2">
            {barData.map(item => (
              <div key={item.name} className="bg-elevated rounded p-2.5">
                <div className="text-text-muted font-mono mb-1" style={{ fontSize: '10px' }}>{item.name}</div>
                <div className="font-mono text-lg font-bold" style={{ color: item.color }}>
                  {item.count}
                </div>
                <div className="font-mono text-xs text-text-muted">
                  {item.ratio != null ? `${(item.ratio * 100).toFixed(1)}%` : 'N/A'}
                </div>
                <div className="text-text-muted mt-1" style={{ fontSize: '9px', lineHeight: 1.4 }}>
                  {item.label}
                </div>
              </div>
            ))}
          </div>

          {/* Trend chart */}
          {trendData.length > 1 && (
            <div>
              <div className="text-text-muted font-mono mb-2" style={{ fontSize: '10px' }}>分歧趋势（近14天）</div>
              <ResponsiveContainer width="100%" height={120}>
                <BarChart data={trendData} margin={{ top: 0, right: 0, bottom: 0, left: -20 }}>
                  <XAxis
                    dataKey="date"
                    tick={{ fontSize: 9, fill: '#8b949e', fontFamily: 'monospace' }}
                    tickLine={false}
                    axisLine={{ stroke: '#21262d' }}
                  />
                  <YAxis
                    tick={{ fontSize: 9, fill: '#8b949e', fontFamily: 'monospace' }}
                    tickLine={false}
                    axisLine={false}
                    allowDecimals={false}
                  />
                  <Tooltip
                    contentStyle={{
                      background: '#161b22',
                      border: '1px solid #21262d',
                      borderRadius: '4px',
                      fontSize: '11px',
                      fontFamily: 'monospace',
                    }}
                  />
                  <Legend
                    formatter={(v: string) => (
                      <span style={{ fontSize: '10px', fontFamily: 'monospace', color: '#8b949e' }}>{v}</span>
                    )}
                  />
                  <Bar dataKey="激进" stackId="a" fill="#f97316" />
                  <Bar dataKey="保守" stackId="a" fill="#a5b4fc" />
                  <Bar dataKey="一致" stackId="a" fill="#00d4aa" />
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}

          {/* Interpretation note */}
          <div
            className="rounded p-2 font-mono"
            style={{ background: 'rgba(227,179,65,0.08)', border: '1px solid rgba(227,179,65,0.2)', fontSize: '10px', color: '#8b949e', lineHeight: 1.6 }}
          >
            解读：LLM激进比例高 → LLM系统性看多偏差；LLM保守比例高 → LLM系统性保守偏差；
            需结合准确率判断哪侧偏差"赚钱"
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Alpha block ──────────────────────────────────────────────────────────────

function AlphaBlock({ data }: { data: QualityTrackingData }) {
  const { alpha } = data;

  return (
    <div className="bg-surface border border-border rounded p-3">
      <SectionHeader title="累计 Alpha" subtitle="高置信度(综合≥65)组合 vs 全样本平均" />
      {alpha.evaluated === 0 ? (
        <div className="text-text-muted text-xs italic py-2">暂无已验证数据 — 判断需10日后才能验证</div>
      ) : (
        <div className="grid grid-cols-3 gap-2">
          <div className="bg-elevated rounded p-2.5">
            <div className="text-text-muted font-mono mb-1" style={{ fontSize: '10px' }}>高置信度样本</div>
            <div className="font-mono text-lg font-bold text-teal">{alpha.high_conviction_count}</div>
            <div className="text-text-muted font-mono" style={{ fontSize: '10px' }}>综合分 ≥ 65</div>
          </div>
          <div className="bg-elevated rounded p-2.5">
            <div className="text-text-muted font-mono mb-1" style={{ fontSize: '10px' }}>高置信度10日均收益</div>
            <div
              className="font-mono text-lg font-bold"
              style={{ color: alpha.high_conviction_avg_ret == null ? '#8b949e' : alpha.high_conviction_avg_ret > 0 ? '#f85149' : '#00d4aa' }}
            >
              {ret(alpha.high_conviction_avg_ret)}
            </div>
          </div>
          <div className="bg-elevated rounded p-2.5">
            <div className="text-text-muted font-mono mb-1" style={{ fontSize: '10px' }}>全样本10日均收益</div>
            <div
              className="font-mono text-lg font-bold"
              style={{ color: alpha.overall_avg_ret == null ? '#8b949e' : alpha.overall_avg_ret > 0 ? '#f85149' : '#00d4aa' }}
            >
              {ret(alpha.overall_avg_ret)}
            </div>
            <div className="text-text-muted font-mono" style={{ fontSize: '10px' }}>已验证: {alpha.evaluated}</div>
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Main component ───────────────────────────────────────────────────────────

export default function QualityPage() {
  const { data, loading } = useApi<QualityTrackingData>('/api/quality-tracking', 300000);

  if (loading && !data) {
    return (
      <div className="h-full flex flex-col overflow-hidden bg-bg p-4 space-y-3">
        {[...Array(4)].map((_, i) => (
          <div key={i} className="skeleton h-40 rounded" />
        ))}
      </div>
    );
  }

  if (!data) {
    return (
      <div className="h-full flex items-center justify-center text-text-muted">
        <div className="text-center">
          <div className="text-4xl mb-3 opacity-30">📈</div>
          <div className="text-sm">质量追踪数据加载失败</div>
        </div>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col overflow-hidden bg-bg">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-2 border-b border-border flex-shrink-0 bg-surface">
        <span className="font-mono font-semibold text-xs uppercase tracking-wider text-text-muted">
          质量追踪
        </span>
        <span className="font-mono text-xs text-text-muted">
          规则准确率 · LLM准确率 · 分歧统计 · Alpha
        </span>
      </div>

      <div className="flex-1 overflow-auto p-4">
        <div className="grid grid-cols-2 gap-4">
          {/* Block 1: Rule accuracy */}
          <AccuracyTable rows={data.rule_accuracy} title="规则信号胜率" />

          {/* Block 2: LLM accuracy */}
          <AccuracyTable rows={data.llm_accuracy} title="LLM 信号胜率" showVote={true} />

          {/* Block 3: Divergence */}
          <div className="col-span-2">
            <DivergenceBlock data={data} />
          </div>

          {/* Block 4: Alpha */}
          <div className="col-span-2">
            <AlphaBlock data={data} />
          </div>
        </div>
      </div>
    </div>
  );
}
