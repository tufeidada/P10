import type { JudgmentHistory } from '../types';

interface JudgmentTimelineProps {
  judgments: JudgmentHistory[];
  loading?: boolean;
}

export default function JudgmentTimeline({ judgments, loading }: JudgmentTimelineProps) {
  if (loading) {
    return (
      <div className="bg-surface border border-border rounded p-3">
        <div className="text-text-muted font-mono text-xs uppercase tracking-wider mb-2">判断历史</div>
        <div className="space-y-1.5">
          {[...Array(5)].map((_, i) => (
            <div key={i} className="skeleton h-7 rounded" />
          ))}
        </div>
      </div>
    );
  }

  if (judgments.length === 0) {
    return (
      <div className="bg-surface border border-border rounded p-3">
        <div className="text-text-muted font-mono text-xs uppercase tracking-wider mb-3">判断历史</div>
        <div className="text-center text-text-muted py-4 text-xs">暂无历史判断记录</div>
      </div>
    );
  }

  function ResultIcon({ isCorrect }: { isCorrect: boolean | null }) {
    if (isCorrect === null) return <span className="text-amber text-xs">⏳</span>;
    return <span className="text-xs">{isCorrect ? '✅' : '❌'}</span>;
  }

  function DirectionBadge({ direction }: { direction: string }) {
    const map: Record<string, { text: string; color: string }> = {
      bullish: { text: '看多', color: '#00d4aa' },
      neutral: { text: '中性', color: '#e3b341' },
      bearish: { text: '看空', color: '#f85149' },
    };
    const { text, color } = map[direction] ?? { text: direction, color: '#8b949e' };
    return (
      <span className="font-mono text-xs font-semibold" style={{ color }}>
        {text}
      </span>
    );
  }

  return (
    <div className="bg-surface border border-border rounded p-3">
      <div className="text-text-muted font-mono text-xs uppercase tracking-wider mb-2">
        判断历史
        <span className="ml-2 text-teal">({judgments.length})</span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-border">
              {['日期', '方向', '综合', '置信度', 'T+5', 'T+10', '结果'].map(h => (
                <th
                  key={h}
                  className="text-text-muted font-mono text-left py-1.5 pr-3"
                  style={{ fontSize: '10px', fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.06em' }}
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {judgments.map(j => {
              const ret5d = j.actual_ret_5d ?? null;
              const ret10d = j.actual_ret_10d;
              const colorOf = (v: number | null) =>
                v == null ? '#8b949e' : v > 0 ? '#00d4aa' : '#f85149';
              const fmt = (v: number | null) =>
                v != null ? `${v >= 0 ? '+' : ''}${(v * 100).toFixed(2)}%` : '--';

              return (
                <tr key={j.id} className="border-b border-border/30 hover:bg-elevated/40 transition-colors">
                  <td className="font-mono text-text-muted py-1.5 pr-3" style={{ fontSize: '11px' }}>
                    {new Date(j.judgment_date).toLocaleDateString('zh-CN', {
                      month: '2-digit',
                      day: '2-digit',
                    })}
                  </td>
                  <td className="py-1.5 pr-3">
                    <DirectionBadge direction={j.direction} />
                  </td>
                  <td className="font-mono py-1.5 pr-3" style={{ fontSize: '11px' }}>
                    <span
                      style={{
                        color: j.composite_score >= 65 ? '#00d4aa' : j.composite_score >= 40 ? '#e3b341' : '#f85149',
                      }}
                    >
                      {j.composite_score.toFixed(1)}
                    </span>
                  </td>
                  <td className="font-mono py-1.5 pr-3" style={{ fontSize: '11px' }}>
                    <span style={{ color: j.confidence >= 0.7 ? '#00d4aa' : j.confidence >= 0.5 ? '#e3b341' : '#f85149' }}>
                      {(j.confidence * 100).toFixed(0)}%
                    </span>
                  </td>
                  <td className="font-mono py-1.5 pr-3" style={{ fontSize: '11px', color: colorOf(ret5d) }}>
                    {fmt(ret5d)}
                  </td>
                  <td className="font-mono py-1.5 pr-3" style={{ fontSize: '11px', color: colorOf(ret10d) }}>
                    {fmt(ret10d)}
                  </td>
                  <td
                    className="py-1.5"
                    title={j.error_category ?? undefined}
                  >
                    <ResultIcon isCorrect={j.is_correct} />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
