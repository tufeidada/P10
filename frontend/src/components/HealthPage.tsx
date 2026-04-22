import { useApi } from '../hooks/useApi';
import type { DataQualityData } from '../types';

interface SchedulerJob {
  job_name: string;
  total: number;
  success: number;
  failed: number;
  skipped: number;
  last_run: string | null;
}

interface SchedulerStatusData {
  heartbeat: {
    beat_time: string | null;
    lag_min: number | null;
    healthy: boolean;
    pid: number | null;
    jobs_count: number;
  };
  jobs: SchedulerJob[];
  last_composite: { job_name: string; status: string; trigger_time: string } | null;
  llm_cost: { today_cny: number; total_cny: number; daily_avg_7d_cny: number; monthly_est_cny: number; budget_cny: number };
  llm_quality: { today_total: number; unknown_count: number; unknown_ratio: number | null };
}

// ─── helpers ──────────────────────────────────────────────────────────────────

function daysSince(dateStr: string | null): number | null {
  if (!dateStr) return null;
  const d = new Date(dateStr);
  if (isNaN(d.getTime())) return null;
  return Math.floor((Date.now() - d.getTime()) / 86_400_000);
}

function statusColor(status: 'ok' | 'warning' | 'error'): string {
  return status === 'ok' ? '#00d4aa' : status === 'warning' ? '#e3b341' : '#f85149';
}

function overallBadge(status: string) {
  const map: Record<string, { label: string; color: string; bg: string }> = {
    ok: { label: 'OK', color: '#00d4aa', bg: 'rgba(0,212,170,0.12)' },
    warning: { label: 'DEGRADED', color: '#e3b341', bg: 'rgba(227,179,65,0.12)' },
    error: { label: 'ERROR', color: '#f85149', bg: 'rgba(248,81,73,0.12)' },
  };
  const s = map[status] ?? map['error'];
  return (
    <span
      className="font-mono text-xs font-bold px-2 py-0.5 rounded border"
      style={{ color: s.color, background: s.bg, borderColor: `${s.color}40` }}
    >
      {s.label}
    </span>
  );
}

// ─── sub-components ───────────────────────────────────────────────────────────

interface SourceCardProps {
  name: string;
  status: 'ok' | 'warning' | 'error';
  latestDate: string | null;
  rowCount?: number;
}

function SourceCard({ name, status, latestDate, rowCount }: SourceCardProps) {
  const days = daysSince(latestDate);
  const dotColor = statusColor(status);

  return (
    <div
      className="bg-elevated border rounded-lg p-4 flex flex-col gap-2"
      style={{ borderColor: status === 'ok' ? '#21262d' : `${dotColor}40` }}
    >
      {/* Header row */}
      <div className="flex items-center justify-between">
        <span className="font-mono font-bold text-sm text-text-primary truncate mr-2">{name}</span>
        <div className="flex items-center gap-1.5 flex-shrink-0">
          <div
            className="w-2 h-2 rounded-full"
            style={{ background: dotColor, boxShadow: status !== 'ok' ? `0 0 6px ${dotColor}` : undefined }}
          />
          <span className="font-mono text-xs" style={{ color: dotColor }}>
            {status.toUpperCase()}
          </span>
        </div>
      </div>

      {/* Stats */}
      <div className="flex items-center gap-4">
        <div className="flex flex-col">
          <span className="text-text-muted font-mono" style={{ fontSize: '10px' }}>最新日期</span>
          <span className="font-mono text-xs text-text-primary mt-0.5">
            {latestDate ?? '--'}
          </span>
        </div>
        {rowCount != null && (
          <>
            <div className="h-6 w-px bg-border" />
            <div className="flex flex-col">
              <span className="text-text-muted font-mono" style={{ fontSize: '10px' }}>记录数</span>
              <span className="font-mono text-xs text-text-primary mt-0.5">
                {rowCount.toLocaleString()}
              </span>
            </div>
          </>
        )}
        {days != null && (
          <>
            <div className="h-6 w-px bg-border" />
            <div className="flex flex-col">
              <span className="text-text-muted font-mono" style={{ fontSize: '10px' }}>距今天数</span>
              <span
                className="font-mono text-xs mt-0.5"
                style={{ color: days <= 1 ? '#00d4aa' : days <= 3 ? '#e3b341' : '#f85149' }}
              >
                {days === 0 ? '今天' : `${days} 天前`}
              </span>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ─── main component ───────────────────────────────────────────────────────────

export default function HealthPage() {
  const { data: health, loading, error } = useApi<DataQualityData>('/api/data-quality', 30000);
  const { data: scheduler } = useApi<SchedulerStatusData>('/api/scheduler/status', 30000);

  const sources = health?.sources ?? {};
  const sourceEntries = Object.entries(sources);

  return (
    <div className="h-full overflow-auto bg-bg">
      <div className="p-6 space-y-6 max-w-screen-2xl mx-auto">

        {/* ── Header ────────────────────────────────────────────────────────── */}
        <section>
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <h2 className="font-mono font-bold text-sm uppercase tracking-widest text-text-primary">
                系统数据健康
              </h2>
              {health && overallBadge(health.status)}
            </div>
            <div className="flex items-center gap-2">
              {health?.last_checked && (
                <span className="font-mono text-xs text-text-muted">
                  上次检查: {new Date(health.last_checked).toLocaleString('zh-CN', {
                    month: '2-digit', day: '2-digit',
                    hour: '2-digit', minute: '2-digit',
                  })}
                </span>
              )}
              {loading && (
                <div
                  className="w-3 h-3 rounded-full border-2 border-teal border-t-transparent animate-spin"
                />
              )}
            </div>
          </div>
        </section>

        {/* ── Error state ───────────────────────────────────────────────────── */}
        {error && !health && (
          <div className="bg-elevated border border-red/30 rounded-lg p-6 flex items-center gap-3">
            <div className="w-2 h-2 rounded-full bg-red flex-shrink-0" />
            <span className="font-mono text-sm text-red">无法获取健康数据: {error}</span>
          </div>
        )}

        {/* ── Data sources grid ─────────────────────────────────────────────── */}
        <section>
          <h3 className="font-mono text-xs uppercase tracking-wider text-text-muted mb-3">
            数据源状态
          </h3>
          {loading && sourceEntries.length === 0 ? (
            <div className="grid grid-cols-2 xl:grid-cols-3 gap-3">
              {[...Array(6)].map((_, i) => (
                <div key={i} className="skeleton h-24 rounded-lg" />
              ))}
            </div>
          ) : sourceEntries.length === 0 ? (
            <div className="bg-elevated border border-border rounded-lg p-8 flex flex-col items-center justify-center text-text-muted gap-2">
              <span className="text-3xl opacity-30">🔌</span>
              <span className="text-sm">暂无数据源信息</span>
            </div>
          ) : (
            <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-3">
              {sourceEntries.map(([name, src]) => (
                <SourceCard
                  key={name}
                  name={name}
                  status={src.status}
                  latestDate={src.latest_date}
                  rowCount={src.row_count}
                />
              ))}
            </div>
          )}
        </section>

        {/* ── Scheduler status ─────────────────────────────────────────────── */}
        <section>
          <div className="flex items-center justify-between mb-3">
            <h3 className="font-mono text-xs uppercase tracking-wider text-text-muted">
              Scheduler 状态
            </h3>
            {scheduler?.heartbeat && (
              <div className="flex items-center gap-2">
                <div
                  className="w-2 h-2 rounded-full"
                  style={{ background: scheduler.heartbeat.healthy ? '#00d4aa' : '#f85149' }}
                />
                <span className="font-mono text-xs text-text-muted">
                  心跳: {scheduler.heartbeat.lag_min != null ? `${scheduler.heartbeat.lag_min}分钟前` : '--'}
                  {scheduler.heartbeat.pid != null && ` · PID ${scheduler.heartbeat.pid}`}
                </span>
              </div>
            )}
          </div>

          {/* LLM cost */}
          {scheduler?.llm_cost && (
            <div className="mb-3 px-3 py-2 bg-elevated border border-border rounded-lg">
              <div className="flex items-center gap-4 flex-wrap">
                <span className="font-mono text-xs text-text-muted">LLM 今日:</span>
                <span className="font-mono text-xs font-bold" style={{ color: '#00d4aa' }}>
                  ¥{scheduler.llm_cost.today_cny.toFixed(4)}
                </span>
                <div className="h-3 w-px bg-border" />
                <span className="font-mono text-xs text-text-muted">近7天日均:</span>
                <span className="font-mono text-xs font-bold" style={{ color: '#e3b341' }}>
                  ¥{scheduler.llm_cost.daily_avg_7d_cny.toFixed(4)}
                </span>
                <span className="font-mono text-xs text-text-muted">| 月预估:</span>
                <span className="font-mono text-xs font-bold" style={{ color: '#a5b4fc' }}>
                  ¥{scheduler.llm_cost.monthly_est_cny.toFixed(2)}
                </span>
                <div className="h-3 w-px bg-border" />
                <span className="font-mono text-xs text-text-muted">预算上限:</span>
                <span className="font-mono text-xs text-text-muted">¥{scheduler.llm_cost.budget_cny.toFixed(0)}</span>
                <div className="h-3 w-px bg-border" />
                <span className="font-mono text-xs text-text-muted">累计:</span>
                <span className="font-mono text-xs text-text-primary">¥{scheduler.llm_cost.total_cny.toFixed(4)}</span>
              </div>
              {scheduler.llm_quality && (
                <div className="mt-1.5 flex items-center gap-3">
                  <span className="font-mono text-text-muted" style={{ fontSize: '10px' }}>今日LLM判断:</span>
                  <span className="font-mono text-xs">{scheduler.llm_quality.today_total} 条</span>
                  <span className="font-mono text-text-muted" style={{ fontSize: '10px' }}>未知比例:</span>
                  <span
                    className="font-mono text-xs"
                    style={{
                      color: scheduler.llm_quality.unknown_ratio == null ? '#8b949e'
                        : scheduler.llm_quality.unknown_ratio > 0.3 ? '#f85149'
                        : scheduler.llm_quality.unknown_ratio > 0.1 ? '#e3b341'
                        : '#00d4aa',
                    }}
                  >
                    {scheduler.llm_quality.unknown_ratio != null
                      ? `${(scheduler.llm_quality.unknown_ratio * 100).toFixed(1)}%`
                      : 'N/A'}
                  </span>
                </div>
              )}
            </div>
          )}

          {/* Job success rates */}
          {scheduler?.jobs && scheduler.jobs.length > 0 ? (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-border">
                    {['任务', '成功', '失败', '跳过', '成功率', '最后运行'].map(h => (
                      <th key={h} className="text-left text-text-muted font-mono py-1.5 pr-4" style={{ fontSize: '10px', fontWeight: 600, textTransform: 'uppercase' }}>
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {scheduler.jobs.map(job => {
                    const rate = job.total > 0 ? Math.round((job.success / job.total) * 100) : null;
                    const rateColor = rate == null ? '#8b949e' : rate >= 80 ? '#00d4aa' : rate >= 50 ? '#e3b341' : '#f85149';
                    return (
                      <tr key={job.job_name} className="border-b border-border/30 hover:bg-elevated/40">
                        <td className="font-mono py-1.5 pr-4 text-text-primary" style={{ fontSize: '11px' }}>
                          {job.job_name}
                        </td>
                        <td className="font-mono py-1.5 pr-4 text-teal" style={{ fontSize: '11px' }}>{job.success}</td>
                        <td className="font-mono py-1.5 pr-4" style={{ fontSize: '11px', color: job.failed > 0 ? '#f85149' : '#8b949e' }}>{job.failed}</td>
                        <td className="font-mono py-1.5 pr-4 text-text-muted" style={{ fontSize: '11px' }}>{job.skipped}</td>
                        <td className="font-mono py-1.5 pr-4" style={{ fontSize: '11px', color: rateColor }}>
                          {rate != null ? `${rate}%` : '--'}
                        </td>
                        <td className="font-mono py-1.5 pr-4 text-text-muted" style={{ fontSize: '11px' }}>
                          {job.last_run ? new Date(job.last_run).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '--'}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : !scheduler ? (
            <div className="grid grid-cols-2 gap-2">
              {[...Array(6)].map((_, i) => <div key={i} className="skeleton h-8 rounded" />)}
            </div>
          ) : (
            <div className="text-center text-text-muted text-xs py-4">过去24h暂无任务记录</div>
          )}
        </section>

        {/* ── Summary stats ─────────────────────────────────────────────────── */}
        {sourceEntries.length > 0 && (
          <section>
            <h3 className="font-mono text-xs uppercase tracking-wider text-text-muted mb-3">
              汇总
            </h3>
            <div className="grid grid-cols-3 gap-3">
              {(
                [
                  {
                    label: '正常数据源',
                    value: sourceEntries.filter(([, s]) => s.status === 'ok').length,
                    color: '#00d4aa',
                  },
                  {
                    label: '告警数据源',
                    value: sourceEntries.filter(([, s]) => s.status === 'warning').length,
                    color: '#e3b341',
                  },
                  {
                    label: '异常数据源',
                    value: sourceEntries.filter(([, s]) => s.status === 'error').length,
                    color: '#f85149',
                  },
                ] as const
              ).map(({ label, value, color }) => (
                <div
                  key={label}
                  className="bg-elevated border border-border rounded-lg px-4 py-3 flex flex-col gap-1"
                >
                  <span className="font-mono text-xs text-text-muted uppercase tracking-wider">
                    {label}
                  </span>
                  <span className="font-mono font-bold text-2xl" style={{ color }}>
                    {value}
                  </span>
                  <span className="font-mono text-xs text-text-muted">
                    共 {sourceEntries.length} 个
                  </span>
                </div>
              ))}
            </div>
          </section>
        )}

      </div>
    </div>
  );
}
