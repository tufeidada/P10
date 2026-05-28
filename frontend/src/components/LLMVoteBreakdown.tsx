/**
 * Multi-LLM voting breakdown — displays each model's individual stance
 * plus the voted consensus. Designed for dev-mode max-info-density: shows
 * every model's direction/signal/reasoning/risks so the user can compare
 * model bias and spot dissenters.
 */

interface PerModelResult {
  model: string;
  ok: boolean;
  elapsed_ms?: number;
  direction?: string;
  signal_strength?: string;
  reasoning?: string | null;
  risks?: string | null;
  extra_advice?: string | null;
  narrative?: string | null;
  error?: string;
  raw_excerpt?: string;
}

interface Props {
  perModel: PerModelResult[] | null | undefined;
  voted: { direction: string; signal: string; consensus: number; total: number };
}

const DIR_LABEL: Record<string, { text: string; color: string }> = {
  bullish: { text: '看多', color: '#00d4aa' },
  neutral: { text: '中性', color: '#e3b341' },
  bearish: { text: '看空', color: '#f85149' },
  unknown: { text: '未知', color: '#6e7681' },
};

const SIG_LABEL: Record<string, { text: string; color: string }> = {
  strong_buy:  { text: '强买', color: '#00d4aa' },
  buy:         { text: '买入', color: '#00d4aa' },
  weak_buy:    { text: '弱买', color: '#7ce0c0' },
  hold:        { text: '持有', color: '#e3b341' },
  weak_sell:   { text: '弱卖', color: '#f0945a' },
  sell:        { text: '卖出', color: '#f85149' },
  strong_sell: { text: '强卖', color: '#f85149' },
  unknown:     { text: '未知', color: '#6e7681' },
};

function Pill({ map, value }: { map: Record<string, { text: string; color: string }>; value: string }) {
  const m = map[value] ?? { text: value, color: '#6e7681' };
  return (
    <span
      className="font-mono font-semibold inline-block px-1.5 py-0.5 rounded"
      style={{ fontSize: '10px', color: m.color, background: `${m.color}22`, border: `1px solid ${m.color}66` }}
    >
      {m.text}
    </span>
  );
}

export default function LLMVoteBreakdown({ perModel, voted }: Props) {
  if (!perModel || perModel.length === 0) {
    return (
      <div className="bg-surface border border-border rounded p-3 text-center text-text-muted text-xs py-4">
        暂无多模型投票数据（旧 judgment 未保存 per_model）
      </div>
    );
  }

  const dissenters = perModel.filter(
    (r) => r.ok && r.direction && r.direction !== voted.direction,
  );

  return (
    <div className="bg-surface border border-border rounded p-3">
      <div className="flex items-center justify-between mb-2">
        <div className="text-text-muted font-mono text-xs uppercase tracking-wider">
          多模型投票 <span className="ml-1 text-teal">({perModel.length})</span>
        </div>
        <div className="flex items-center gap-2 font-mono" style={{ fontSize: '10px' }}>
          <span className="text-text-muted">共识</span>
          <span
            className="font-bold"
            style={{
              color: voted.consensus >= 0.99 ? '#00d4aa' : voted.consensus >= 0.66 ? '#e3b341' : '#f85149',
            }}
          >
            {(voted.consensus * 100).toFixed(0)}%
          </span>
          {dissenters.length > 0 && (
            <span className="text-amber" title={`${dissenters.length} 个模型方向与众数不一致`}>
              ⚠ {dissenters.length} 分歧
            </span>
          )}
        </div>
      </div>

      <div className="space-y-2">
        {perModel.map((r) => {
          const isDissent = r.ok && r.direction && r.direction !== voted.direction;
          return (
            <div
              key={r.model}
              className="border border-border/50 rounded p-2"
              style={{
                background: isDissent ? 'rgba(227,179,65,0.05)' : 'transparent',
                borderLeft: isDissent ? '2px solid #e3b341' : undefined,
              }}
            >
              <div className="flex items-center justify-between mb-1">
                <div className="flex items-center gap-2">
                  <span
                    className="font-mono font-bold uppercase"
                    style={{ fontSize: '11px', color: r.ok ? '#79c0ff' : '#f85149' }}
                  >
                    {r.model}
                  </span>
                  {r.ok ? (
                    <>
                      <Pill map={DIR_LABEL} value={r.direction ?? 'unknown'} />
                      <Pill map={SIG_LABEL} value={r.signal_strength ?? 'unknown'} />
                      {isDissent && (
                        <span className="text-amber font-mono" style={{ fontSize: '10px' }}>
                          ✗ 与众数不同
                        </span>
                      )}
                    </>
                  ) : (
                    <span className="font-mono text-text-muted" style={{ fontSize: '10px' }}>
                      ❌ {r.error?.slice(0, 60) || 'failed'}
                    </span>
                  )}
                </div>
                {r.elapsed_ms != null && (
                  <span
                    className="font-mono text-text-muted"
                    style={{ fontSize: '10px' }}
                  >
                    {(r.elapsed_ms / 1000).toFixed(1)}s
                  </span>
                )}
              </div>
              {r.ok && r.reasoning && (
                <div
                  className="font-mono text-text"
                  style={{ fontSize: '11px', lineHeight: 1.5 }}
                >
                  <span className="text-text-muted">理由：</span>
                  {r.reasoning}
                </div>
              )}
              {r.ok && r.risks && (
                <div
                  className="font-mono mt-1"
                  style={{ fontSize: '11px', lineHeight: 1.5, color: '#f0945a' }}
                >
                  <span className="text-text-muted">风险：</span>
                  {r.risks}
                </div>
              )}
              {r.ok && r.extra_advice && (
                <div
                  className="font-mono text-text-muted mt-1"
                  style={{ fontSize: '11px', lineHeight: 1.5 }}
                >
                  <span>建议：</span>
                  {r.extra_advice}
                </div>
              )}
              {r.ok && r.narrative && (
                <details className="mt-1">
                  <summary
                    className="font-mono text-text-muted cursor-pointer"
                    style={{ fontSize: '10px' }}
                  >
                    展开完整叙事
                  </summary>
                  <div
                    className="font-mono text-text mt-1 pl-2 border-l border-border/50"
                    style={{ fontSize: '11px', lineHeight: 1.6 }}
                  >
                    {r.narrative}
                  </div>
                </details>
              )}
              {!r.ok && r.raw_excerpt && (
                <details className="mt-1">
                  <summary
                    className="font-mono text-text-muted cursor-pointer"
                    style={{ fontSize: '10px' }}
                  >
                    展开 raw response 片段
                  </summary>
                  <pre
                    className="font-mono text-text-muted mt-1 pl-2 border-l border-border/50 whitespace-pre-wrap"
                    style={{ fontSize: '10px', lineHeight: 1.4 }}
                  >
                    {r.raw_excerpt}
                  </pre>
                </details>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
