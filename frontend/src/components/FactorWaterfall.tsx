import type { FactorContributions } from '../types';

/**
 * Visualizes how composite_score decomposes into per-dimension contributions.
 *
 * composite ≈ baseline_50 + Σ (score - 50) × weight
 *
 * Each row shows: dim name | score | weight × | contribution bar (signed) | composite share.
 * Residual is surfaced separately when |residual| > 0.5 so users see when the
 * stored composite_score doesn't perfectly match the weight-based recomputation.
 */
interface Props {
  data: FactorContributions | null | undefined;
}

const DIM_LABELS: Record<string, string> = {
  technical: '技术',
  fundamental: '基本',
  flow: '资金',
  sentiment: '情绪',
};

const POS = '#00d4aa';
const NEG = '#f85149';
const NEU = '#6e7681';

function ContribBar({ value, maxAbs }: { value: number; maxAbs: number }) {
  // Center axis layout: positive bars grow right, negative bars grow left.
  const pct = maxAbs > 0 ? Math.min(50, (Math.abs(value) / maxAbs) * 50) : 0;
  const color = value > 0.05 ? POS : value < -0.05 ? NEG : NEU;
  return (
    <div className="relative h-3" style={{ background: 'transparent' }}>
      {/* center axis */}
      <div
        className="absolute top-0 bottom-0"
        style={{ left: '50%', width: '1px', background: '#30363d' }}
      />
      <div
        className="absolute top-0 bottom-0 rounded-sm"
        style={{
          [value >= 0 ? 'left' : 'right']: '50%',
          width: `${pct}%`,
          background: color,
          opacity: 0.85,
        }}
      />
    </div>
  );
}

export default function FactorWaterfall({ data }: Props) {
  if (!data) {
    return (
      <div className="bg-surface border border-border rounded p-3 text-center text-text-muted text-xs py-4">
        暂无因子分解数据
      </div>
    );
  }

  const rows = (['flow', 'fundamental', 'sentiment', 'technical'] as const)
    .map((dim) => ({ dim, ...data.factors[dim] }))
    .sort((a, b) => b.contribution - a.contribution); // strongest contributor first

  const maxAbs = Math.max(...rows.map((r) => Math.abs(r.contribution)), 0.1);
  const sumContrib = rows.reduce((s, r) => s + r.contribution, 0);
  const showResidual = Math.abs(data.residual) > 0.5;

  return (
    <div className="bg-surface border border-border rounded p-3">
      <div className="flex items-center justify-between mb-2">
        <div className="text-text-muted font-mono text-xs uppercase tracking-wider">
          因子贡献分解
        </div>
        <div
          className="font-mono text-text-muted"
          style={{ fontSize: '10px' }}
          title={`weights from: ${data.weights_source}`}
        >
          {data.weights_source === 'effective_weights' && (
            <span className="text-accent-green">权重已重分配</span>
          )}
          {data.weights_source === 'signal_sources.weights' && (
            <span>基础权重</span>
          )}
          {data.weights_source === 'regime_at_time' && (
            <span className="text-text-muted">legacy weights</span>
          )}
          {data.weights_source === 'fallback_default' && (
            <span style={{ color: '#e3b341' }}>⚠️ 默认权重</span>
          )}
        </div>
      </div>

      {/* Header row */}
      <div
        className="grid items-center text-text-muted font-mono mb-1 pb-1 border-b border-border/50"
        style={{ gridTemplateColumns: '40px 40px 36px 1fr 50px', gap: '8px', fontSize: '10px' }}
      >
        <div>维度</div>
        <div className="text-right">分数</div>
        <div className="text-right">权重</div>
        <div className="text-center">贡献</div>
        <div className="text-right">composite +</div>
      </div>

      {/* Baseline */}
      <div
        className="grid items-center py-1 text-text-muted font-mono"
        style={{ gridTemplateColumns: '40px 40px 36px 1fr 50px', gap: '8px', fontSize: '11px' }}
      >
        <div>基线</div>
        <div className="text-right">50.0</div>
        <div className="text-right">—</div>
        <div className="text-center" style={{ color: NEU }}>—</div>
        <div className="text-right font-bold" style={{ color: NEU }}>
          {data.baseline.toFixed(1)}
        </div>
      </div>

      {/* Factor rows */}
      {rows.map((r) => {
        const color = r.contribution > 0.05 ? POS : r.contribution < -0.05 ? NEG : NEU;
        return (
          <div
            key={r.dim}
            className="grid items-center py-1 font-mono border-b border-border/30 last:border-0"
            style={{ gridTemplateColumns: '40px 40px 36px 1fr 50px', gap: '8px', fontSize: '11px' }}
          >
            <div className="text-text">
              {DIM_LABELS[r.dim] || r.dim}
              {r.score_missing && (
                <span className="text-text-muted ml-1" title="原始分数为 NULL，按 50 代入">*</span>
              )}
            </div>
            <div className="text-right text-text">{r.score.toFixed(1)}</div>
            <div className="text-right text-text-muted">{(r.weight * 100).toFixed(0)}%</div>
            <div>
              <ContribBar value={r.contribution} maxAbs={maxAbs} />
              <div
                className="text-center mt-0.5"
                style={{ fontSize: '10px', color }}
              >
                {r.contribution > 0 ? '+' : ''}
                {r.contribution.toFixed(2)}
              </div>
            </div>
            <div className="text-right font-bold" style={{ color }}>
              {r.contribution > 0 ? '+' : ''}
              {r.contribution.toFixed(2)}
            </div>
          </div>
        );
      })}

      {/* Summary line */}
      <div
        className="grid items-center pt-2 mt-1 border-t border-border/50 font-mono font-bold"
        style={{ gridTemplateColumns: '40px 40px 36px 1fr 50px', gap: '8px', fontSize: '11px' }}
      >
        <div className="text-text">综合</div>
        <div></div>
        <div></div>
        <div className="text-center text-text-muted" style={{ fontSize: '10px' }}>
          50 + ({sumContrib >= 0 ? '+' : ''}
          {sumContrib.toFixed(2)})
          {showResidual && (
            <span style={{ color: '#e3b341' }}> + 残差 {data.residual.toFixed(2)}</span>
          )}
        </div>
        <div className="text-right" style={{ color: '#00d4aa' }}>
          {data.composite_stored.toFixed(1)}
        </div>
      </div>

      {showResidual && (
        <div
          className="mt-2 px-2 py-1 rounded text-text-muted font-mono"
          style={{ fontSize: '10px', background: '#21262d', lineHeight: 1.5 }}
        >
          ⚠️ 重算值 {data.composite_recomputed.toFixed(2)} 与存储值
          {data.composite_stored.toFixed(2)} 偏差 {data.residual.toFixed(2)} 分。
          可能原因：has_social=False 重分配未在历史 judgment 上记录，或某维度分数为 NULL 被 50 替代。
        </div>
      )}
    </div>
  );
}
