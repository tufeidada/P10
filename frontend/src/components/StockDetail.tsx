import { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  Legend,
} from 'recharts';
import { useApi } from '../hooks/useApi';
import type { JudgmentDetail, JudgmentHistory } from '../types';
import RadarChartComponent from './RadarChartComponent';
import JudgmentTimeline from './JudgmentTimeline';
import FactorWaterfall from './FactorWaterfall';

interface StockDetailProps {
  symbol: string;
  onBack: () => void;
}

interface JudgmentListResponse {
  judgments: JudgmentHistory[];
}

// ─── Score row with evidence ──────────────────────────────────────────────────

function ScoreRow({
  label,
  value,
  evidence,
}: {
  label: string;
  value: number | null;
  evidence?: string | null;
}) {
  const score = value ?? 0;
  const color = score >= 65 ? '#00d4aa' : score >= 40 ? '#e3b341' : '#f85149';
  const pct = Math.min(100, Math.max(0, score));

  return (
    <div className="py-1.5 border-b border-border/50">
      <div className="flex items-center justify-between mb-0.5">
        <span className="text-text-muted text-xs font-mono">{label}</span>
        <span className="font-mono text-sm font-bold" style={{ color }}>
          {value != null ? value.toFixed(1) : '--'}
        </span>
      </div>
      <div className="h-1 rounded-full mb-0.5" style={{ background: '#21262d' }}>
        <div className="h-full rounded-full transition-all" style={{ width: `${pct}%`, background: color }} />
      </div>
      {evidence && (
        <div className="text-text-muted font-mono" style={{ fontSize: '10px', lineHeight: '1.4' }}>
          {evidence}
        </div>
      )}
    </div>
  );
}

// ─── Extract evidence text from signal_sources ────────────────────────────────

function extractEvidence(sources: Record<string, unknown>, dim: string): string | null {
  const d = sources[dim];
  if (!d || typeof d !== 'object') return null;
  const data = d as Record<string, unknown>;
  const parts: string[] = [];

  if (dim === 'technical') {
    if (data.trend != null) parts.push(`趋势: ${String(data.trend)}`);
    if (data.stage != null) parts.push(`阶段: ${String(data.stage)}`);
    if (data.rs != null) parts.push(`RS: ${Number(data.rs).toFixed(2)}`);
    if (data.rsi != null) parts.push(`RSI: ${Number(data.rsi).toFixed(1)}`);
    if (data.vol_ratio != null) parts.push(`量比: ${Number(data.vol_ratio).toFixed(2)}`);
    if (data.ma_alignment != null) parts.push(`MA排列: ${String(data.ma_alignment)}`);
  } else if (dim === 'fundamental') {
    if (data.roe != null) parts.push(`ROE: ${Number(data.roe).toFixed(1)}%`);
    if (data.rev_yoy != null) parts.push(`营收增速: ${Number(data.rev_yoy).toFixed(1)}%`);
    if (data.np_yoy != null) parts.push(`净利增速: ${Number(data.np_yoy).toFixed(1)}%`);
    if (data.gross_margin != null) parts.push(`毛利率: ${Number(data.gross_margin).toFixed(1)}%`);
    if (data.net_margin != null) parts.push(`净利率: ${Number(data.net_margin).toFixed(1)}%`);
    if (data.debt_ratio != null) parts.push(`资产负债率: ${Number(data.debt_ratio).toFixed(1)}%`);
  } else if (dim === 'flow') {
    const nb = data.northbound as Record<string, unknown> | null | undefined;
    if (nb?.net_5d != null) parts.push(`北向5日净: ${(Number(nb.net_5d) / 1e8).toFixed(2)}亿`);
    const mf = data.main_force as Record<string, unknown> | null | undefined;
    if (mf?.net_lg_5d != null) parts.push(`主力5日净: ${(Number(mf.net_lg_5d) / 10000).toFixed(0)}万`);
    const mg = data.margin as Record<string, unknown> | null | undefined;
    if (mg?.change_5d_pct != null) parts.push(`融资5日变化: ${Number(mg.change_5d_pct).toFixed(1)}%`);
  } else if (dim === 'sentiment') {
    if (data.social_score != null) parts.push(`社交情绪: ${Number(data.social_score).toFixed(1)}`);
    if (data.news_tone != null) parts.push(`新闻基调: ${String(data.news_tone)}`);
    if (data.analyst_consensus != null) parts.push(`分析师共识: ${String(data.analyst_consensus)}`);
  }

  return parts.length > 0 ? parts.join('  ·  ') : null;
}

// ─── LLM narrative ────────────────────────────────────────────────────────────

function LLMNarrative({ text }: { text: string | null }) {
  const [expanded, setExpanded] = useState(false);
  if (!text) {
    return (
      <div className="bg-surface border border-border rounded p-3">
        <div className="text-text-muted font-mono text-xs uppercase tracking-wider mb-2">分析叙述</div>
        <div className="text-text-muted text-xs italic">暂无 LLM 叙述</div>
      </div>
    );
  }
  const PREVIEW_LEN = 500;
  const showToggle = text.length > PREVIEW_LEN;
  const displayText = expanded || !showToggle ? text : text.slice(0, PREVIEW_LEN) + '\n\n...';
  return (
    <div className="bg-surface border border-border rounded p-3">
      <div className="text-text-muted font-mono text-xs uppercase tracking-wider mb-2">分析叙述</div>
      <div
        className="markdown-content"
        style={{ maxHeight: expanded ? 'none' : '160px', overflow: 'hidden' }}
      >
        <ReactMarkdown>{displayText}</ReactMarkdown>
      </div>
      {showToggle && (
        <button onClick={() => setExpanded(e => !e)} className="mt-2 text-teal font-mono text-xs hover:underline">
          {expanded ? '收起' : '展开全文 ▼'}
        </button>
      )}
    </div>
  );
}

// ─── Signal color helpers ─────────────────────────────────────────────────────

const DIR_COLOR: Record<string, string> = {
  bullish: '#00d4aa',
  neutral: '#e3b341',
  bearish: '#f85149',
};
const DIR_LABEL: Record<string, string> = {
  bullish: '看多',
  neutral: '中性',
  bearish: '看空',
  unknown: '?',
};
const SIG_COLOR: Record<string, string> = {
  strong_buy: '#00d4aa',
  buy: '#26a86d',
  hold: '#e3b341',
  sell: '#f97316',
  strong_sell: '#f85149',
  unknown: '#8b949e',
};
const SIG_LABEL: Record<string, string> = {
  strong_buy: '强买',
  buy: '买入',
  hold: '观望',
  sell: '卖出',
  strong_sell: '强卖',
  unknown: '未知',
};

function isDivergent(ruleDir: string | null, llmDir: string | null): boolean {
  if (!ruleDir || !llmDir || llmDir === 'unknown') return false;
  return ruleDir !== llmDir;
}

// ─── Dual signal card ─────────────────────────────────────────────────────────

function DualSignalCard({ judgment }: { judgment: JudgmentDetail }) {
  const ruleDir = judgment.direction;
  const ruleSig = judgment.rule_signal_strength ?? 'unknown';
  const llmDir = judgment.llm_direction ?? 'unknown';
  const llmSig = judgment.llm_signal_strength ?? 'unknown';
  const divergent = isDivergent(ruleDir, llmDir);
  const voteConsensus = judgment.llm_vote_consensus;
  const voteTotalCalls = judgment.llm_vote_total_calls;

  const SigChip = ({ label, dir, sig }: { label: string; dir: string; sig: string }) => (
    <div className="bg-elevated rounded p-2.5 flex-1">
      <div className="text-text-muted font-mono mb-1.5" style={{ fontSize: '10px' }}>{label}</div>
      <div className="flex items-center gap-1.5 mb-1">
        <div className="w-1.5 h-1.5 rounded-full" style={{ background: DIR_COLOR[dir] ?? '#8b949e' }} />
        <span className="font-mono text-xs font-semibold" style={{ color: DIR_COLOR[dir] ?? '#8b949e' }}>
          {DIR_LABEL[dir] ?? dir}
        </span>
      </div>
      <span
        className="font-mono text-xs font-bold px-1.5 py-0.5 rounded"
        style={{
          background: `${SIG_COLOR[sig] ?? '#8b949e'}22`,
          color: SIG_COLOR[sig] ?? '#8b949e',
        }}
      >
        {SIG_LABEL[sig] ?? sig}
      </span>
    </div>
  );

  return (
    <div className="bg-surface border border-border rounded p-3">
      <div className="flex items-center gap-2 mb-2">
        <div className="text-text-muted font-mono text-xs uppercase tracking-wider">双信号</div>
        {divergent && (
          <span
            className="font-mono text-xs px-1.5 py-0.5 rounded"
            style={{ background: 'rgba(227,179,65,0.15)', color: '#e3b341' }}
          >
            ⚠️ 规则/LLM 分歧
          </span>
        )}
      </div>
      <div className="flex gap-2">
        <SigChip label="规则信号" dir={ruleDir} sig={ruleSig} />
        <SigChip label="LLM 信号" dir={llmDir} sig={llmSig} />
      </div>

      {/* Vote consensus badge */}
      {voteConsensus != null && voteTotalCalls != null && (
        <div className="mt-1.5 flex items-center gap-1.5">
          <span className="text-text-muted font-mono" style={{ fontSize: '10px' }}>vote 一致性:</span>
          <span
            className="font-mono text-xs font-semibold"
            style={{ color: voteConsensus >= 0.67 ? '#00d4aa' : '#e3b341' }}
          >
            {Math.round(voteConsensus * voteTotalCalls)}/{voteTotalCalls}
          </span>
          {voteConsensus < 0.67 && (
            <span className="font-mono" style={{ fontSize: '10px', color: '#e3b341' }}>⚠ 低一致性</span>
          )}
        </div>
      )}

      {/* LLM reasoning / risks / extra advice */}
      {(judgment.llm_reasoning || judgment.llm_risks || judgment.llm_extra_advice) && (
        <div className="mt-2.5 space-y-1.5">
          {judgment.llm_reasoning && (
            <div className="flex gap-1.5">
              <span className="text-teal font-mono flex-shrink-0" style={{ fontSize: '10px' }}>核心理由</span>
              <span className="text-text-primary" style={{ fontSize: '11px', lineHeight: '1.5' }}>
                {judgment.llm_reasoning}
              </span>
            </div>
          )}
          {judgment.llm_risks && (
            <div className="flex gap-1.5">
              <span className="font-mono flex-shrink-0" style={{ fontSize: '10px', color: '#f85149' }}>主要风险</span>
              <span className="text-text-muted" style={{ fontSize: '11px', lineHeight: '1.5' }}>
                {judgment.llm_risks}
              </span>
            </div>
          )}
          {judgment.llm_extra_advice && (
            <div className="flex gap-1.5">
              <span className="font-mono flex-shrink-0" style={{ fontSize: '10px', color: '#e3b341' }}>额外建议</span>
              <span className="text-text-muted" style={{ fontSize: '11px', lineHeight: '1.5' }}>
                {judgment.llm_extra_advice}
              </span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ─── 20-day dimension trend chart ─────────────────────────────────────────────

interface TrendPoint {
  date: string;
  technical: number | null;
  fundamental: number | null;
  flow: number | null;
  sentiment: number | null;
}

const TREND_COLORS = {
  technical: '#00d4aa',
  fundamental: '#e3b341',
  flow: '#a5b4fc',
  sentiment: '#f97316',
};

const TREND_LABELS: Record<string, string> = {
  technical: '技术',
  fundamental: '基本面',
  flow: '资金',
  sentiment: '情绪',
};

function DimensionTrendChart({ history }: { history: JudgmentHistory[] }) {
  if (history.length === 0) return null;

  const points: TrendPoint[] = [...history]
    .reverse()
    .slice(-20)
    .map(j => ({
      date: new Date(j.judgment_date).toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' }),
      technical: j.technical_score ?? null,
      fundamental: j.fundamental_score ?? null,
      flow: j.flow_score ?? null,
      sentiment: j.sentiment_score ?? null,
    }));

  const hasDimData = points.some(p => p.technical != null || p.fundamental != null);
  if (!hasDimData) return null;

  return (
    <div className="bg-surface border border-border rounded p-3">
      <div className="text-text-muted font-mono text-xs uppercase tracking-wider mb-3">
        维度趋势 <span className="text-teal ml-1">近{points.length}日</span>
      </div>
      <ResponsiveContainer width="100%" height={160}>
        <LineChart data={points} margin={{ top: 4, right: 8, bottom: 0, left: -20 }}>
          <XAxis
            dataKey="date"
            tick={{ fontSize: 9, fill: '#8b949e', fontFamily: 'monospace' }}
            tickLine={false}
            axisLine={{ stroke: '#21262d' }}
            interval="preserveStartEnd"
          />
          <YAxis
            domain={[0, 100]}
            tick={{ fontSize: 9, fill: '#8b949e', fontFamily: 'monospace' }}
            tickLine={false}
            axisLine={false}
            ticks={[0, 50, 100]}
          />
          <Tooltip
            contentStyle={{
              background: '#161b22',
              border: '1px solid #21262d',
              borderRadius: '4px',
              fontSize: '11px',
              fontFamily: 'monospace',
            }}
            labelStyle={{ color: '#8b949e', marginBottom: '4px' }}
            formatter={(value: unknown, name: unknown) => [
              value != null ? (value as number).toFixed(1) : '--',
              TREND_LABELS[name as string] ?? String(name),
            ]}
          />
          <Legend
            formatter={(value: string) => (
              <span style={{ fontSize: '10px', fontFamily: 'monospace', color: '#8b949e' }}>
                {TREND_LABELS[value] ?? value}
              </span>
            )}
          />
          {(Object.keys(TREND_COLORS) as (keyof typeof TREND_COLORS)[]).map(dim => (
            <Line
              key={dim}
              type="monotone"
              dataKey={dim}
              stroke={TREND_COLORS[dim]}
              strokeWidth={1.5}
              dot={false}
              connectNulls
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

// ─── Main component ───────────────────────────────────────────────────────────

export default function StockDetail({ symbol, onBack }: StockDetailProps) {
  const { data: judgment, loading: jLoading } = useApi<JudgmentDetail>(
    `/api/analysis/${symbol}`,
    60000
  );
  const { data: historyData, loading: histLoading } = useApi<JudgmentListResponse>(
    `/api/analysis/${symbol}/history`,
    120000
  );

  const judgmentHistory = historyData?.judgments ?? [];
  const prevJudgment = judgmentHistory[1] ?? null;
  const prevJudgmentFull = prevJudgment
    ? ({
        ...judgment,
        technical_score: prevJudgment.technical_score ?? judgment?.technical_score ?? null,
        fundamental_score: prevJudgment.fundamental_score ?? judgment?.fundamental_score ?? null,
        flow_score: prevJudgment.flow_score ?? judgment?.flow_score ?? null,
        sentiment_score: prevJudgment.sentiment_score ?? judgment?.sentiment_score ?? null,
        composite_score: prevJudgment.composite_score,
        confidence: prevJudgment.confidence,
        direction: prevJudgment.direction as 'bullish' | 'neutral' | 'bearish',
      } as JudgmentDetail)
    : null;

  return (
    <div className="h-full flex flex-col overflow-hidden bg-bg">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-2 border-b border-border bg-surface flex-shrink-0">
        <div className="flex items-center gap-3">
          <button onClick={onBack} className="text-text-muted hover:text-text-primary text-xs font-mono">
            ← 返回
          </button>
          <div className="h-3 w-px bg-border" />
          <span className="font-mono font-bold text-teal text-base">{symbol}</span>
          {judgment && (
            <>
              <div className="h-3 w-px bg-border" />
              <span
                className="font-mono text-sm font-bold"
                style={{
                  color: judgment.direction === 'bullish' ? '#00d4aa' : judgment.direction === 'bearish' ? '#f85149' : '#e3b341',
                }}
              >
                {judgment.direction === 'bullish' ? '看多' : judgment.direction === 'bearish' ? '看空' : '中性'}
              </span>
              <span className="text-text-muted font-mono text-xs">
                置信 {(judgment.confidence * 100).toFixed(0)}%
              </span>
            </>
          )}
        </div>
        <div className="flex items-center gap-2">
          {judgment && (
            <>
              <span className="text-text-muted font-mono text-xs">综合</span>
              <span
                className="font-mono font-bold text-sm"
                style={{ color: judgment.composite_score >= 65 ? '#00d4aa' : judgment.composite_score >= 40 ? '#e3b341' : '#f85149' }}
              >
                {judgment.composite_score.toFixed(1)}
              </span>
              <div className="h-3 w-px bg-border" />
              <span className="text-text-muted font-mono text-xs">
                {new Date(judgment.judgment_date).toLocaleDateString('zh-CN')}
              </span>
            </>
          )}
        </div>
      </div>

      {/* Body: left 60% | right 40% */}
      <div className="flex flex-1 overflow-hidden">
        {/* Left: scores + narrative + trade */}
        <div className="flex flex-col overflow-y-auto p-3 space-y-3" style={{ width: '60%' }}>
          {jLoading && !judgment ? (
            <div className="bg-surface border border-border rounded p-3 space-y-2">
              {[...Array(4)].map((_, i) => <div key={i} className="skeleton h-10 rounded" />)}
            </div>
          ) : judgment ? (
            <div className="bg-surface border border-border rounded p-3">
              <div className="text-text-muted font-mono text-xs uppercase tracking-wider mb-2">
                维度分析
              </div>
              <ScoreRow
                label="技术面"
                value={judgment.technical_score}
                evidence={extractEvidence(judgment.signal_sources, 'technical')}
              />
              <ScoreRow
                label="基本面"
                value={judgment.fundamental_score}
                evidence={extractEvidence(judgment.signal_sources, 'fundamental')}
              />
              <ScoreRow
                label="资金面"
                value={judgment.flow_score}
                evidence={extractEvidence(judgment.signal_sources, 'flow')}
              />
              <ScoreRow
                label="情绪面"
                value={judgment.sentiment_score}
                evidence={extractEvidence(judgment.signal_sources, 'sentiment')}
              />
            </div>
          ) : (
            <div className="bg-surface border border-border rounded p-3 text-center text-text-muted text-xs py-4">
              暂无分析数据
            </div>
          )}

          {/* Factor contribution waterfall (A1) */}
          {judgment && <FactorWaterfall data={judgment.factor_contributions} />}

          {/* Dual signal */}
          {judgment && <DualSignalCard judgment={judgment} />}

          {/* LLM narrative */}
          {jLoading && !judgment ? (
            <div className="space-y-1.5">
              {[...Array(5)].map((_, i) => <div key={i} className="skeleton h-4 rounded" />)}
            </div>
          ) : (
            <LLMNarrative text={judgment?.logic_text ?? null} />
          )}
        </div>

        {/* Right: radar + trend chart + history */}
        <div
          className="flex flex-col overflow-y-auto border-l border-border bg-surface/50 p-3 space-y-3"
          style={{ width: '40%' }}
        >
          {jLoading && !judgment ? (
            <div className="skeleton rounded" style={{ height: '240px' }} />
          ) : judgment ? (
            <RadarChartComponent current={judgment} previous={prevJudgmentFull} />
          ) : (
            <div className="bg-surface border border-border rounded p-3 text-center text-text-muted text-xs py-8">
              暂无判断数据
            </div>
          )}

          {/* 20-day dimension trend */}
          <DimensionTrendChart history={judgmentHistory} />

          {/* Judgment history table */}
          <JudgmentTimeline
            judgments={judgmentHistory.slice(0, 10)}
            loading={histLoading && judgmentHistory.length === 0}
          />
        </div>
      </div>
    </div>
  );
}
