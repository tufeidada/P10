import { useState } from 'react';
import { useApi } from '../hooks/useApi';
import type { RegimeData, StockSummary, Signal, HealthData, CandidateListResponse, SignalListResponse, RegimeResponse } from '../types';

interface SidebarProps {
  selectedSymbol: string | null;
  onSelectSymbol: (symbol: string | null) => void;
}

const REGIME_LABELS: Record<string, string> = {
  offense: '进攻',
  cautious_offense: '谨慎进攻',
  defense: '防守',
  risk_off: '规避风险',
};

const REGIME_BORDER: Record<string, string> = {
  offense: 'border-l-teal',
  cautious_offense: 'border-l-amber',
  defense: 'border-l-orange-500',
  risk_off: 'border-l-red',
};

const REGIME_TEXT: Record<string, string> = {
  offense: 'text-teal',
  cautious_offense: 'text-amber',
  defense: 'text-orange-400',
  risk_off: 'text-red',
};

function ScoreBar({ label, value }: { label: string; value: number | null }) {
  const pct = value != null ? Math.min(100, Math.max(0, value)) : 0;
  const color = value == null ? '#21262d' : value >= 60 ? '#00d4aa' : value >= 40 ? '#e3b341' : '#f85149';

  return (
    <div className="flex items-center gap-2 py-0.5">
      <span className="text-text-muted font-mono" style={{ fontSize: '10px', minWidth: '28px' }}>{label}</span>
      <div className="relative" style={{ width: '40px', height: '3px', background: '#21262d', borderRadius: '2px' }}>
        <div
          style={{
            position: 'absolute', left: 0, top: 0, height: '100%',
            width: `${pct}%`, background: color, borderRadius: '2px',
            transition: 'width 0.4s ease',
          }}
        />
      </div>
      <span className="font-mono" style={{ fontSize: '10px', color, minWidth: '28px' }}>
        {value != null ? value.toFixed(1) : '--'}
      </span>
    </div>
  );
}

function RegimeCard({ label, data }: { label: string; data: RegimeData | null }) {
  if (!data) {
    return (
      <div className="flex-1 p-2 rounded border border-border bg-elevated flex flex-col gap-1"
        style={{ borderLeft: '3px solid #21262d' }}>
        <div className="flex items-center justify-between mb-1">
          <span className="text-text-muted font-mono" style={{ fontSize: '10px' }}>{label}</span>
          <span className="text-text-muted font-mono" style={{ fontSize: '10px' }}>--</span>
        </div>
        <div className="skeleton h-3 w-16 mb-1" />
        <div className="skeleton h-2 w-full" />
        <div className="skeleton h-2 w-full" />
        <div className="skeleton h-2 w-full" />
        <div className="skeleton h-2 w-full" />
      </div>
    );
  }

  void REGIME_BORDER[data.regime_mode]; // unused but kept for reference
  const textClass = REGIME_TEXT[data.regime_mode] || 'text-text-muted';
  const borderColor = {
    offense: '#00d4aa',
    cautious_offense: '#e3b341',
    defense: '#f97316',
    risk_off: '#f85149',
  }[data.regime_mode] || '#21262d';

  return (
    <div
      className="flex-1 p-2 rounded border border-border bg-elevated flex flex-col"
      style={{ borderLeft: `3px solid ${borderColor}` }}
    >
      <div className="flex items-center justify-between mb-1.5">
        <span className="text-text-muted font-mono" style={{ fontSize: '10px' }}>{label}</span>
        <span className={`font-mono font-semibold ${textClass}`} style={{ fontSize: '10px' }}>
          {REGIME_LABELS[data.regime_mode]}
        </span>
      </div>
      <ScoreBar label="趋势" value={data.trend_score} />
      <ScoreBar label="波动" value={data.volatility_score} />
      <ScoreBar label="宽度" value={data.breadth_score} />
      <ScoreBar label="流动" value={data.liquidity_score} />
    </div>
  );
}

function DirectionBadge({ direction }: { direction: 'bullish' | 'neutral' | 'bearish' | null }) {
  if (!direction) return <span className="text-text-muted text-xs">--</span>;
  const map = {
    bullish: { icon: '↑', color: 'text-teal' },
    neutral: { icon: '→', color: 'text-amber' },
    bearish: { icon: '↓', color: 'text-red' },
  };
  const { icon, color } = map[direction];
  return <span className={`font-mono font-bold text-sm ${color}`}>{icon}</span>;
}

function ScoreNum({ value }: { value: number | null }) {
  if (value == null) return <span className="text-text-muted font-mono text-xs">--</span>;
  const color = value >= 65 ? '#00d4aa' : value >= 40 ? '#e3b341' : '#f85149';
  return (
    <span className="font-mono text-xs font-semibold" style={{ color }}>
      {value.toFixed(1)}
    </span>
  );
}

function StockRow({ stock, isSelected, onClick }: {
  stock: StockSummary;
  isSelected: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`w-full flex items-center justify-between px-3 py-2 transition-all text-left card-hover ${
        isSelected ? 'bg-elevated' : 'hover:bg-elevated/60'
      }`}
      style={{
        borderLeft: isSelected ? '3px solid #00d4aa' : '3px solid transparent',
        boxShadow: isSelected ? 'inset 3px 0 8px rgba(0, 212, 170, 0.08)' : undefined,
      }}
    >
      <div className="flex flex-col min-w-0">
        <span className="font-mono font-semibold text-xs text-text-primary">{stock.symbol}</span>
        <span className="text-text-muted truncate" style={{ fontSize: '10px', maxWidth: '120px' }}>{stock.name}</span>
      </div>
      <div className="flex items-center gap-2 flex-shrink-0">
        <ScoreNum value={stock.composite_score} />
        <DirectionBadge direction={stock.direction} />
      </div>
    </button>
  );
}

function SignalRow({ signal }: { signal: Signal }) {
  const isBuy = signal.signal_type === 'buy';
  return (
    <div className="flex items-center justify-between px-3 py-1.5 border-b border-border/30">
      <div className="flex items-center gap-2">
        <span className={`font-mono text-xs font-bold ${isBuy ? 'text-teal' : 'text-red'}`}>
          {isBuy ? '买' : '卖'}
        </span>
        <span className="font-mono text-xs text-text-primary">{signal.symbol}</span>
        <span className="text-text-muted" style={{ fontSize: '10px' }}>{signal.strength}</span>
      </div>
      <span className="font-mono text-xs text-text-muted">
        {signal.price_at_signal.toFixed(2)}
      </span>
    </div>
  );
}

export default function Sidebar({ selectedSymbol, onSelectSymbol }: SidebarProps) {
  const [signalsExpanded, setSignalsExpanded] = useState(false);

  const { data: regimeData, loading: regimeLoading } = useApi<RegimeResponse>('/api/regime/latest', 60000);
  const { data: candidatesData, loading: candidatesLoading } = useApi<CandidateListResponse>('/api/candidates', 60000);
  const { data: signalsData } = useApi<SignalListResponse>('/api/signals/today', 60000);
  const { data: healthData } = useApi<HealthData>('/api/health', 60000);

  const stocks = candidatesData?.stocks ?? [];
  const signals = signalsData?.signals ?? [];
  const buySignals = signals.filter(s => s.signal_type === 'buy');
  const sellSignals = signals.filter(s => s.signal_type === 'sell');

  return (
    <aside
      className="flex flex-col bg-surface border-r border-border overflow-hidden flex-shrink-0"
      style={{ width: '280px' }}
    >
      {/* Regime section */}
      <div className="px-3 pt-3 pb-2 border-b border-border">
        <div className="flex items-center justify-between mb-2">
          <span className="text-text-muted font-mono font-semibold uppercase tracking-wider" style={{ fontSize: '10px' }}>
            市场状态
          </span>
          <span className="text-text-muted font-mono" style={{ fontSize: '10px' }}>
            {regimeData
              ? (() => {
                  const today = new Date().toISOString().slice(0, 10);
                  const cnDate = regimeData.cn?.trade_date ?? null;
                  const usDate = regimeData.us?.trade_date ?? null;
                  const latest = [cnDate, usDate].filter(Boolean).sort().pop() ?? null;
                  if (!latest) return '--';
                  return latest === today ? latest : `${latest} ⚠️`;
                })()
              : '...'}
          </span>
        </div>
        <div className="flex gap-2">
          {regimeLoading && !regimeData ? (
            <>
              <div className="flex-1 skeleton h-20 rounded" />
              <div className="flex-1 skeleton h-20 rounded" />
            </>
          ) : (
            <>
              <RegimeCard label="A股" data={regimeData?.cn ?? null} />
              <RegimeCard label="美股" data={regimeData?.us ?? null} />
            </>
          )}
        </div>
      </div>

      {/* Candidate pool header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-border flex-shrink-0">
        <span className="text-text-muted font-mono font-semibold uppercase tracking-wider" style={{ fontSize: '10px' }}>
          候选池
        </span>
        <span className="bg-elevated text-teal font-mono font-bold rounded px-1.5 py-0.5" style={{ fontSize: '10px' }}>
          {candidatesData?.total ?? stocks.length}
        </span>
      </div>

      {/* Stock list */}
      <div className="flex-1 overflow-y-auto">
        {candidatesLoading && stocks.length === 0 ? (
          <div className="p-3 space-y-1">
            {[...Array(8)].map((_, i) => (
              <div key={i} className="skeleton h-9 rounded" />
            ))}
          </div>
        ) : stocks.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-24 text-text-muted" style={{ fontSize: '11px' }}>
            <span>候选池为空</span>
            <span className="text-xs mt-1 opacity-60">请先运行数据分析流程</span>
          </div>
        ) : (
          <div className="py-1">
            {stocks.map(stock => (
              <StockRow
                key={stock.symbol}
                stock={stock}
                isSelected={selectedSymbol === stock.symbol}
                onClick={() => onSelectSymbol(stock.symbol)}
              />
            ))}
          </div>
        )}
      </div>

      {/* Today's signals */}
      <div className="border-t border-border flex-shrink-0">
        <button
          className="w-full flex items-center justify-between px-3 py-2 hover:bg-elevated/50 transition-colors"
          onClick={() => setSignalsExpanded(!signalsExpanded)}
        >
          <div className="flex items-center gap-2">
            <span className="text-amber" style={{ fontSize: '11px' }}>📡</span>
            <span className="text-text-muted font-mono font-semibold uppercase tracking-wider" style={{ fontSize: '10px' }}>
              今日信号
            </span>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-teal font-mono font-bold" style={{ fontSize: '10px' }}>
              {buySignals.length} 买
            </span>
            <span className="text-text-muted font-mono" style={{ fontSize: '10px' }}>·</span>
            <span className="text-red font-mono font-bold" style={{ fontSize: '10px' }}>
              {sellSignals.length} 卖
            </span>
            <span className={`text-text-muted transition-transform ${signalsExpanded ? 'rotate-180' : ''}`} style={{ fontSize: '10px' }}>
              ▼
            </span>
          </div>
        </button>
        {signalsExpanded && (
          <div className="max-h-36 overflow-y-auto bg-elevated/30">
            {signals.length === 0 ? (
              <div className="px-3 py-2 text-text-muted text-center" style={{ fontSize: '11px' }}>
                今日暂无信号
              </div>
            ) : (
              signals.map(s => <SignalRow key={s.id} signal={s} />)
            )}
          </div>
        )}
      </div>

      {/* Health status */}
      <div className="border-t border-border px-3 py-2 flex-shrink-0">
        <div className="flex items-center justify-between mb-1.5">
          <span className="text-text-muted font-mono font-semibold uppercase tracking-wider" style={{ fontSize: '10px' }}>
            数据状态
          </span>
          <span
            className={`font-mono font-bold ${
              healthData?.status === 'ok' ? 'text-teal' :
              healthData?.status === 'warning' ? 'text-amber' : 'text-red'
            }`}
            style={{ fontSize: '10px' }}
          >
            {healthData?.status === 'ok' ? '● 正常' :
             healthData?.status === 'warning' ? '● 告警' : '● 异常'}
          </span>
        </div>
        {healthData?.sources && (
          <div className="grid grid-cols-2 gap-x-2 gap-y-0.5">
            {Object.entries(healthData.sources).map(([key, val]) => (
              <div key={key} className="flex items-center gap-1">
                <div
                  className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${
                    val.status === 'ok' ? 'bg-teal' :
                    val.status === 'warning' ? 'bg-amber' : 'bg-red'
                  }`}
                />
                <span className="text-text-muted truncate font-mono" style={{ fontSize: '9px' }}>
                  {key}
                </span>
              </div>
            ))}
          </div>
        )}
        {!healthData && (
          <div className="grid grid-cols-2 gap-1">
            {[...Array(4)].map((_, i) => (
              <div key={i} className="skeleton h-3 rounded" />
            ))}
          </div>
        )}
      </div>
    </aside>
  );
}
