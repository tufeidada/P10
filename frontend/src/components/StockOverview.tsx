import { useState, useMemo } from 'react';
import { useApi } from '../hooks/useApi';
import type { StockSummary, CandidateListResponse } from '../types';

interface StockOverviewProps {
  onSelectSymbol: (symbol: string) => void;
}

type SortField = 'composite_score' | 'technical_score' | 'fundamental_score' | 'flow_score' | 'confidence' | 'symbol';

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
  unknown: '--',
};
const DIR_LABEL: Record<string, string> = {
  bullish: '多',
  neutral: '中',
  bearish: '空',
  unknown: '?',
};
const DIR_COLOR: Record<string, string> = {
  bullish: '#00d4aa',
  neutral: '#e3b341',
  bearish: '#f85149',
  unknown: '#8b949e',
};

function SignalCell({ dir, sig, isDivergent }: { dir: string | null; sig: string | null; isDivergent?: boolean }) {
  if (!sig || !dir) return <span className="text-text-muted text-xs">--</span>;
  const color = SIG_COLOR[sig] ?? '#8b949e';
  const dirColor = DIR_COLOR[dir] ?? '#8b949e';
  return (
    <div className="flex items-center gap-1">
      <span className="font-mono text-xs font-semibold" style={{ color: dirColor }}>
        {DIR_LABEL[dir] ?? dir}
      </span>
      <span
        className="font-mono text-xs px-1 py-0.5 rounded"
        style={{ background: `${color}22`, color }}
      >
        {SIG_LABEL[sig] ?? sig}
      </span>
      {isDivergent && <span style={{ fontSize: '11px' }}>⚠️</span>}
    </div>
  );
}
type SortDir = 'asc' | 'desc';

function ScorePill({ value }: { value: number | null }) {
  if (value == null) return <span className="text-text-muted font-mono text-xs">--</span>;

  let bg: string;
  let color: string;
  if (value >= 70) { bg = 'rgba(0, 212, 170, 0.22)'; color = '#00d4aa'; }
  else if (value >= 50) { bg = 'rgba(227, 179, 65, 0.18)'; color = '#e3b341'; }
  else { bg = 'rgba(248, 81, 73, 0.18)'; color = '#f85149'; }

  return (
    <span
      className="font-mono text-xs font-semibold px-1.5 py-0.5 rounded"
      style={{ background: bg, color }}
    >
      {value.toFixed(1)}
    </span>
  );
}

function PriceCell({ close, pctChg, barDate }: { close: number | null; pctChg: number | null; barDate: string | null }) {
  if (close == null) return <span className="text-text-muted text-xs">--</span>;
  const today = new Date().toISOString().slice(0, 10);
  const daysAgo = barDate && barDate < today
    ? Math.round((Date.now() - new Date(barDate).getTime()) / 86_400_000)
    : 0;
  // A股 convention: up=red, down=green
  const pctColor = pctChg == null ? '#8b949e' : pctChg > 0 ? '#f85149' : pctChg < 0 ? '#00d4aa' : '#8b949e';
  const pctSign = pctChg != null && pctChg > 0 ? '+' : '';
  return (
    <div className="flex flex-col">
      <span className="font-mono text-xs text-text-primary">{close.toFixed(2)}</span>
      <div className="flex items-center gap-1">
        {pctChg != null && (
          <span className="font-mono" style={{ fontSize: '10px', color: pctColor }}>
            {pctSign}{pctChg.toFixed(2)}%
          </span>
        )}
        {daysAgo > 0 && (
          <span className="text-text-muted" style={{ fontSize: '9px' }}>({daysAgo}天前)</span>
        )}
      </div>
    </div>
  );
}

function DirectionCell({ direction }: { direction: 'bullish' | 'neutral' | 'bearish' | null }) {
  if (!direction) return <span className="text-text-muted text-xs">--</span>;
  const map = {
    bullish: { text: '看多', dot: '#00d4aa' },
    neutral: { text: '中性', dot: '#e3b341' },
    bearish: { text: '看空', dot: '#f85149' },
  };
  const { text, dot } = map[direction];
  return (
    <div className="flex items-center gap-1.5">
      <div className="w-1.5 h-1.5 rounded-full flex-shrink-0" style={{ background: dot }} />
      <span className="font-mono text-xs" style={{ color: dot }}>{text}</span>
    </div>
  );
}

function ConfidenceBar({ value }: { value: number | null }) {
  if (value == null) return <span className="text-text-muted font-mono text-xs">--</span>;
  const pct = Math.min(100, Math.max(0, value * 100));
  const color = pct >= 70 ? '#00d4aa' : pct >= 50 ? '#e3b341' : '#f85149';
  return (
    <div className="flex items-center gap-2">
      <div style={{ width: '48px', height: '3px', background: '#21262d', borderRadius: '2px' }}>
        <div style={{ width: `${pct}%`, height: '100%', background: color, borderRadius: '2px', transition: 'width 0.3s' }} />
      </div>
      <span className="font-mono text-xs" style={{ color }}>
        {(value * 100).toFixed(0)}%
      </span>
    </div>
  );
}


export default function StockOverview({ onSelectSymbol }: StockOverviewProps) {
  const [sortField, setSortField] = useState<SortField>('composite_score');
  const [sortDir, setSortDir] = useState<SortDir>('desc');
  const [filterDir, setFilterDir] = useState<string>('all');
  const [search, setSearch] = useState('');

  const { data, loading } = useApi<CandidateListResponse>('/api/candidates?limit=100', 60000);

  const stocks = data?.stocks ?? [];

  const handleSort = (field: SortField) => {
    if (field === sortField) {
      setSortDir(d => d === 'asc' ? 'desc' : 'asc');
    } else {
      setSortField(field);
      setSortDir('desc');
    }
  };

  const sortedStocks = useMemo(() => {
    let filtered = [...stocks];
    if (filterDir !== 'all') {
      filtered = filtered.filter(s => s.direction === filterDir);
    }
    if (search.trim()) {
      const q = search.trim().toLowerCase();
      filtered = filtered.filter(
        s => s.symbol.toLowerCase().includes(q) || (s.name ?? '').toLowerCase().includes(q)
      );
    }
    return filtered.sort((a, b) => {
      const aVal = a[sortField as keyof StockSummary] as number | string | null;
      const bVal = b[sortField as keyof StockSummary] as number | string | null;
      if (aVal == null && bVal == null) return 0;
      if (aVal == null) return 1;
      if (bVal == null) return -1;
      if (typeof aVal === 'string') {
        return sortDir === 'asc' ? aVal.localeCompare(bVal as string) : (bVal as string).localeCompare(aVal);
      }
      return sortDir === 'asc' ? (aVal as number) - (bVal as number) : (bVal as number) - (aVal as number);
    });
  }, [stocks, sortField, sortDir, filterDir]);

  function SortHeader({ field, label }: { field: SortField; label: string }) {
    const isActive = sortField === field;
    return (
      <th
        onClick={() => handleSort(field)}
        className={`cursor-pointer select-none ${isActive ? 'text-teal' : ''}`}
      >
        <div className="flex items-center gap-1">
          {label}
          {isActive && (
            <span className="text-teal" style={{ fontSize: '9px' }}>
              {sortDir === 'asc' ? '▲' : '▼'}
            </span>
          )}
        </div>
      </th>
    );
  }

  return (
    <div className="h-full flex flex-col bg-bg overflow-hidden">
      {/* Toolbar */}
      <div className="flex items-center justify-between px-4 py-2 border-b border-border flex-shrink-0 bg-surface">
        <div className="flex items-center gap-3">
          <span className="font-mono font-semibold text-xs uppercase tracking-wider text-text-muted">
            候选股票池
          </span>
          <span className="bg-elevated text-teal font-mono font-bold rounded px-1.5 py-0.5 text-xs">
            {sortedStocks.length}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <input
            type="text"
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="搜索代码/名称..."
            className="bg-elevated border border-border rounded px-2 py-0.5 text-xs font-mono text-text-primary placeholder-text-muted focus:outline-none focus:border-teal/50"
            style={{ width: '140px' }}
          />
          <div className="h-3 w-px bg-border" />
          <span className="text-text-muted text-xs">筛选:</span>
          {(['all', 'bullish', 'neutral', 'bearish'] as const).map(dir => (
            <button
              key={dir}
              onClick={() => setFilterDir(dir)}
              className={`text-xs px-2 py-0.5 rounded border transition-colors font-mono ${
                filterDir === dir
                  ? 'border-teal text-teal bg-teal/10'
                  : 'border-border text-text-muted hover:border-border/60'
              }`}
            >
              {dir === 'all' ? '全部' : dir === 'bullish' ? '看多' : dir === 'neutral' ? '中性' : '看空'}
            </button>
          ))}
        </div>
      </div>

      {/* Table */}
      <div className="flex-1 overflow-auto">
        {loading && stocks.length === 0 ? (
          <div className="p-4 space-y-2">
            {[...Array(12)].map((_, i) => (
              <div key={i} className="skeleton h-9 rounded" />
            ))}
          </div>
        ) : stocks.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-64 text-text-muted">
            <span className="text-4xl mb-3 opacity-30">📊</span>
            <span className="text-sm">候选池暂无数据</span>
            <span className="text-xs mt-1 opacity-60">请先运行数据采集和分析流程</span>
          </div>
        ) : (
          <table className="data-table w-full">
            <thead className="sticky top-0 bg-surface z-10">
              <tr>
                <SortHeader field="symbol" label="代码" />
                <th>名称</th>
                <th>最新价</th>
                <th className="hidden xl:table-cell" style={{ maxWidth: '80px' }}>行业</th>
                <SortHeader field="composite_score" label="综合" />
                <SortHeader field="technical_score" label="技术" />
                <SortHeader field="fundamental_score" label="基本面" />
                <SortHeader field="flow_score" label="资金" />
                <th>方向</th>
                <SortHeader field="confidence" label="置信度" />
                <th>规则信号</th>
                <th>LLM信号</th>
                <th style={{ maxWidth: '60px' }}>更新</th>
              </tr>
            </thead>
            <tbody>
              {sortedStocks.map(stock => (
                <tr
                  key={stock.symbol}
                  onClick={() => onSelectSymbol(stock.symbol)}
                  className="cursor-pointer"
                  style={{ transition: 'background-color 0.1s' }}
                >
                  <td>
                    <span className="font-mono font-bold text-teal text-xs">{stock.symbol}</span>
                  </td>
                  <td>
                    <span className="text-text-primary text-xs">{stock.name}</span>
                  </td>
                  <td>
                    <PriceCell
                      close={stock.latest_close ?? null}
                      pctChg={stock.latest_pct_chg ?? null}
                      barDate={stock.latest_bar_date ?? null}
                    />
                  </td>
                  <td className="hidden xl:table-cell" style={{ maxWidth: '80px' }}>
                    <span className="text-text-muted text-xs truncate block" style={{ maxWidth: '80px' }}>{stock.industry ?? '--'}</span>
                  </td>
                  <td><ScorePill value={stock.composite_score} /></td>
                  <td><ScorePill value={stock.technical_score} /></td>
                  <td><ScorePill value={stock.fundamental_score} /></td>
                  <td><ScorePill value={stock.flow_score} /></td>
                  <td><DirectionCell direction={stock.direction} /></td>
                  <td><ConfidenceBar value={stock.confidence} /></td>
                  <td>
                    <SignalCell
                      dir={stock.direction}
                      sig={stock.rule_signal_strength}
                    />
                  </td>
                  <td>
                    <SignalCell
                      dir={stock.llm_direction}
                      sig={stock.llm_signal_strength}
                      isDivergent={
                        !!(stock.llm_direction &&
                          stock.llm_direction !== 'unknown' &&
                          stock.direction &&
                          stock.llm_direction !== stock.direction)
                      }
                    />
                  </td>
                  <td style={{ maxWidth: '60px' }}>
                    <span className="font-mono text-text-muted" style={{ fontSize: '10px' }}>
                      {stock.judgment_date
                        ? new Date(stock.judgment_date).toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })
                        : '--'}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
