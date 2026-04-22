import { useState } from 'react';
import { useApi } from '../hooks/useApi';
import type { ReviewReport, QualityData, ExperienceData } from '../types';
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from 'recharts';

// ─── tiny sub-components ─────────────────────────────────────────────────────

function MetricCard({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="bg-elevated border border-border rounded-lg px-4 py-3 flex flex-col gap-1">
      <span className="text-text-muted font-mono text-xs uppercase tracking-wider">{label}</span>
      <span className="font-mono font-bold text-xl text-text-primary">{value}</span>
      {sub && <span className="font-mono text-xs text-text-muted">{sub}</span>}
    </div>
  );
}

function AccuracyBadge({ value }: { value: number | null }) {
  if (value == null) return <span className="text-text-muted font-mono text-xs">--</span>;
  const pct = value * 100;
  const color = pct >= 70 ? '#00d4aa' : pct >= 50 ? '#e3b341' : '#f85149';
  const bg = pct >= 70 ? 'rgba(0,212,170,0.12)' : pct >= 50 ? 'rgba(227,179,65,0.12)' : 'rgba(248,81,73,0.12)';
  return (
    <span
      className="font-mono text-xs font-semibold px-1.5 py-0.5 rounded"
      style={{ color, background: bg }}
    >
      {pct.toFixed(1)}%
    </span>
  );
}

function StatusDot({ status }: { status: string }) {
  const color =
    status === 'active' ? '#00d4aa' :
    status === 'deprecated' ? '#f85149' :
    '#e3b341';
  return <div className="w-1.5 h-1.5 rounded-full flex-shrink-0" style={{ background: color }} />;
}

const EXPERIENCE_STATUS_LABELS: Record<string, string> = {
  active: '生效中',
  under_review: '审核中',
  deprecated: '已废弃',
};

const CATEGORY_COLORS: Record<string, string> = {
  technical: '#00d4aa',
  fundamental: '#9b59b6',
  flow: '#e3b341',
  sentiment: '#f85149',
  regime: '#8b949e',
};

// ─── main component ───────────────────────────────────────────────────────────

export default function ReviewPage() {
  const [expFilter, setExpFilter] = useState<string>('all');

  const { data: weekly, loading: weeklyLoading } = useApi<ReviewReport>('/api/review/weekly/latest', 60000);
  const { data: monthly } = useApi<ReviewReport>('/api/review/monthly/latest', 60000);
  const { data: quality } = useApi<QualityData>('/api/quality?limit=20', 60000);
  const { data: experiences, loading: expLoading } = useApi<ExperienceData>('/api/experience', 60000);
  const { data: historyRaw } = useApi<{ history: { week: string; accuracy: number }[] }>(
    '/api/review/weekly/history',
    120000,
  );

  const rules = quality?.rules ?? [];
  const sortedRules = [...rules].sort((a, b) => (b.accuracy ?? 0) - (a.accuracy ?? 0));

  const allExperiences = experiences?.experiences ?? [];
  const filteredExp =
    expFilter === 'all'
      ? allExperiences
      : allExperiences.filter(e => e.status === expFilter);

  const historyData = historyRaw?.history ?? [];

  // ─── loading skeleton ────────────────────────────────────────────────────
  if (weeklyLoading && !weekly) {
    return (
      <div className="h-full overflow-auto p-6 space-y-4">
        {[...Array(6)].map((_, i) => (
          <div key={i} className="skeleton h-12 rounded" />
        ))}
      </div>
    );
  }

  return (
    <div className="h-full overflow-auto bg-bg">
      <div className="p-6 space-y-6 max-w-screen-2xl mx-auto">

        {/* ── Top: latest weekly report ──────────────────────────────────── */}
        <section>
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-3">
              <h2 className="font-mono font-bold text-sm uppercase tracking-widest text-text-primary">
                本周复盘报告
              </h2>
              {weekly && (
                <span className="font-mono text-xs text-text-muted bg-elevated px-2 py-0.5 rounded border border-border">
                  {weekly.report_date}
                </span>
              )}
            </div>
            {weekly && (
              <span className="font-mono text-xs text-text-muted">
                {weekly.market === 'all' ? 'A股 + 美股' : weekly.market.toUpperCase()}
              </span>
            )}
          </div>

          {weekly ? (
            <>
              {/* Metric cards */}
              <div className="grid grid-cols-3 gap-3 mb-4">
                <MetricCard
                  label="判断准确率 (短期)"
                  value={weekly.accuracy_short != null ? `${(weekly.accuracy_short * 100).toFixed(1)}%` : '--'}
                />
                <MetricCard
                  label="超额收益 Alpha"
                  value={
                    weekly.alpha_vs_benchmark != null
                      ? `${weekly.alpha_vs_benchmark >= 0 ? '+' : ''}${(weekly.alpha_vs_benchmark * 100).toFixed(2)}%`
                      : '--'
                  }
                  sub="相对基准"
                />
                <MetricCard
                  label="总判断次数"
                  value={weekly.total_judgments != null ? String(weekly.total_judgments) : '--'}
                />
              </div>

              {/* Full report markdown or summary */}
              <div className="bg-elevated border border-border rounded-lg p-4">
                {weekly.full_report_md ? (
                  <pre
                    className="font-mono text-xs text-text-primary whitespace-pre-wrap leading-relaxed"
                    style={{ wordBreak: 'break-word' }}
                  >
                    {weekly.full_report_md}
                  </pre>
                ) : weekly.summary_text ? (
                  <p className="text-sm text-text-primary leading-relaxed">{weekly.summary_text}</p>
                ) : (
                  <span className="text-text-muted text-sm">暂无报告内容</span>
                )}
              </div>
            </>
          ) : (
            <div className="bg-elevated border border-border rounded-lg p-8 flex flex-col items-center justify-center text-text-muted gap-2">
              <span className="text-3xl opacity-30">📋</span>
              <span className="text-sm">本周复盘报告尚未生成</span>
            </div>
          )}
        </section>

        {/* ── Middle: signal quality table + monthly report ─────────────── */}
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">

          {/* Signal quality table */}
          <section>
            <h2 className="font-mono font-bold text-sm uppercase tracking-widest text-text-primary mb-3">
              信号规则质量排名
            </h2>
            <div className="bg-elevated border border-border rounded-lg overflow-hidden">
              {sortedRules.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-12 text-text-muted gap-2">
                  <span className="text-2xl opacity-30">📈</span>
                  <span className="text-sm">暂无信号质量数据</span>
                </div>
              ) : (
                <table className="w-full">
                  <thead>
                    <tr className="border-b border-border">
                      <th className="text-left px-3 py-2 font-mono text-xs text-text-muted uppercase tracking-wider">规则名</th>
                      <th className="text-right px-3 py-2 font-mono text-xs text-text-muted uppercase tracking-wider">触发次数</th>
                      <th className="text-right px-3 py-2 font-mono text-xs text-text-muted uppercase tracking-wider">准确率</th>
                      <th className="text-right px-3 py-2 font-mono text-xs text-text-muted uppercase tracking-wider">平均收益</th>
                      <th className="text-right px-3 py-2 font-mono text-xs text-text-muted uppercase tracking-wider">IR值</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sortedRules.map((rule, i) => (
                      <tr
                        key={`${rule.rule_name}-${i}`}
                        className="border-b border-border/50 hover:bg-surface/60 transition-colors"
                      >
                        <td className="px-3 py-2">
                          <div className="flex items-center gap-2">
                            <span className="font-mono text-xs text-text-primary">{rule.rule_name}</span>
                            {rule.market && (
                              <span className="font-mono text-xs text-text-muted bg-surface px-1 rounded border border-border">
                                {rule.market.toUpperCase()}
                              </span>
                            )}
                          </div>
                        </td>
                        <td className="px-3 py-2 text-right">
                          <span className="font-mono text-xs text-text-muted">{rule.total_signals}</span>
                        </td>
                        <td className="px-3 py-2 text-right">
                          <AccuracyBadge value={rule.accuracy} />
                        </td>
                        <td className="px-3 py-2 text-right">
                          {rule.avg_return != null ? (
                            <span
                              className="font-mono text-xs"
                              style={{ color: rule.avg_return >= 0 ? '#00d4aa' : '#f85149' }}
                            >
                              {rule.avg_return >= 0 ? '+' : ''}{(rule.avg_return * 100).toFixed(2)}%
                            </span>
                          ) : (
                            <span className="font-mono text-xs text-text-muted">--</span>
                          )}
                        </td>
                        <td className="px-3 py-2 text-right">
                          <span className="font-mono text-xs text-text-muted">
                            {rule.ir_value != null ? rule.ir_value.toFixed(2) : '--'}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </section>

          {/* Monthly report summary */}
          <section>
            <div className="flex items-center justify-between mb-3">
              <h2 className="font-mono font-bold text-sm uppercase tracking-widest text-text-primary">
                月度报告摘要
              </h2>
              {monthly && (
                <span className="font-mono text-xs text-text-muted">{monthly.report_date}</span>
              )}
            </div>
            {monthly ? (
              <div className="space-y-3">
                <div className="grid grid-cols-2 gap-3">
                  <MetricCard
                    label="月度准确率 (中期)"
                    value={monthly.accuracy_mid != null ? `${(monthly.accuracy_mid * 100).toFixed(1)}%` : '--'}
                  />
                  <MetricCard
                    label="月度 Alpha"
                    value={
                      monthly.alpha_vs_benchmark != null
                        ? `${monthly.alpha_vs_benchmark >= 0 ? '+' : ''}${(monthly.alpha_vs_benchmark * 100).toFixed(2)}%`
                        : '--'
                    }
                  />
                </div>
                <div className="bg-elevated border border-border rounded-lg p-4">
                  {monthly.full_report_md ? (
                    <pre
                      className="font-mono text-xs text-text-primary whitespace-pre-wrap leading-relaxed"
                      style={{ wordBreak: 'break-word', maxHeight: '240px', overflow: 'auto' }}
                    >
                      {monthly.full_report_md}
                    </pre>
                  ) : monthly.summary_text ? (
                    <p className="text-sm text-text-primary leading-relaxed">{monthly.summary_text}</p>
                  ) : (
                    <span className="text-text-muted text-sm">暂无月度报告内容</span>
                  )}
                </div>
              </div>
            ) : (
              <div className="bg-elevated border border-border rounded-lg p-8 flex flex-col items-center justify-center text-text-muted gap-2">
                <span className="text-3xl opacity-30">📅</span>
                <span className="text-sm">月度报告尚未生成</span>
              </div>
            )}
          </section>
        </div>

        {/* ── Bottom: accuracy trend + experience library ─────────────────── */}
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">

          {/* Accuracy trend chart */}
          <section>
            <h2 className="font-mono font-bold text-sm uppercase tracking-widest text-text-primary mb-3">
              周度准确率趋势
            </h2>
            <div className="bg-elevated border border-border rounded-lg p-4" style={{ height: '220px' }}>
              {historyData.length > 0 ? (
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={historyData} margin={{ top: 4, right: 8, left: -24, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
                    <XAxis
                      dataKey="week"
                      tick={{ fill: '#8b949e', fontSize: 10, fontFamily: 'JetBrains Mono, monospace' }}
                      tickLine={false}
                      axisLine={{ stroke: '#21262d' }}
                    />
                    <YAxis
                      domain={[0, 1]}
                      tickFormatter={(v: number) => `${(v * 100).toFixed(0)}%`}
                      tick={{ fill: '#8b949e', fontSize: 10, fontFamily: 'JetBrains Mono, monospace' }}
                      tickLine={false}
                      axisLine={{ stroke: '#21262d' }}
                    />
                    <Tooltip
                      contentStyle={{ background: '#161b22', border: '1px solid #21262d', borderRadius: '6px' }}
                      labelStyle={{ color: '#8b949e', fontSize: '11px' }}
                      itemStyle={{ color: '#00d4aa', fontSize: '11px', fontFamily: 'JetBrains Mono' }}
                      formatter={(v) => [`${(Number(v) * 100).toFixed(1)}%`, '准确率']}
                    />
                    <Line
                      type="monotone"
                      dataKey="accuracy"
                      stroke="#00d4aa"
                      strokeWidth={2}
                      dot={{ fill: '#00d4aa', r: 3 }}
                      activeDot={{ r: 5, fill: '#00d4aa' }}
                    />
                  </LineChart>
                </ResponsiveContainer>
              ) : (
                <div className="flex flex-col items-center justify-center h-full text-text-muted gap-2">
                  <span className="text-2xl opacity-30">📊</span>
                  <span className="text-sm">暂无历史趋势数据</span>
                </div>
              )}
            </div>
          </section>

          {/* Experience library */}
          <section>
            <div className="flex items-center justify-between mb-3">
              <h2 className="font-mono font-bold text-sm uppercase tracking-widest text-text-primary">
                经验库
              </h2>
              <div className="flex items-center gap-1.5">
                {(['all', 'active', 'under_review', 'deprecated'] as const).map(s => (
                  <button
                    key={s}
                    onClick={() => setExpFilter(s)}
                    className={`text-xs px-2 py-0.5 rounded border transition-colors font-mono ${
                      expFilter === s
                        ? 'border-teal text-teal bg-teal/10'
                        : 'border-border text-text-muted hover:border-border/60'
                    }`}
                  >
                    {s === 'all' ? '全部' : EXPERIENCE_STATUS_LABELS[s] ?? s}
                  </button>
                ))}
              </div>
            </div>

            <div className="space-y-2 overflow-auto" style={{ maxHeight: '220px' }}>
              {expLoading && allExperiences.length === 0 ? (
                [...Array(4)].map((_, i) => (
                  <div key={i} className="skeleton h-14 rounded" />
                ))
              ) : filteredExp.length === 0 ? (
                <div className="bg-elevated border border-border rounded-lg p-6 flex flex-col items-center justify-center text-text-muted gap-2">
                  <span className="text-2xl opacity-30">💡</span>
                  <span className="text-sm">暂无经验记录</span>
                </div>
              ) : (
                filteredExp.map(exp => (
                  <div
                    key={exp.id}
                    className="bg-elevated border border-border rounded-lg px-3 py-2.5 flex items-start gap-3"
                  >
                    <StatusDot status={exp.status} />
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 mb-1">
                        <span
                          className="font-mono text-xs font-semibold px-1.5 py-0.5 rounded"
                          style={{
                            color: CATEGORY_COLORS[exp.category] ?? '#8b949e',
                            background: `${CATEGORY_COLORS[exp.category] ?? '#8b949e'}18`,
                          }}
                        >
                          {exp.category}
                        </span>
                        <span className="font-mono text-xs text-text-muted">
                          {exp.market.toUpperCase()}
                        </span>
                        <span className="font-mono text-xs text-text-muted ml-auto">
                          {EXPERIENCE_STATUS_LABELS[exp.status] ?? exp.status}
                        </span>
                      </div>
                      <p className="text-xs text-text-primary leading-relaxed line-clamp-2">
                        {exp.content_text}
                      </p>
                      <div className="flex items-center gap-3 mt-1">
                        <span className="font-mono text-xs text-text-muted">
                          发现: {exp.discovery_date}
                        </span>
                        {exp.last_validated && (
                          <span className="font-mono text-xs text-text-muted">
                            验证: {exp.last_validated}
                          </span>
                        )}
                        <span className="font-mono text-xs text-text-muted">
                          应用 {exp.applied_count} 次
                        </span>
                      </div>
                    </div>
                  </div>
                ))
              )}
            </div>
          </section>
        </div>

      </div>
    </div>
  );
}
