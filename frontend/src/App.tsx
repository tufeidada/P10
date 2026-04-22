import { useState, useCallback } from 'react';
import { BrowserRouter, Routes, Route, NavLink, useParams } from 'react-router-dom';
import Sidebar from './components/Sidebar';
import StockOverview from './components/StockOverview';
import StockDetail from './components/StockDetail';
import StatusBar from './components/StatusBar';
import ReviewPage from './components/ReviewPage';
import HealthPage from './components/HealthPage';
import QualityPage from './components/QualityPage';
import type { HealthData } from './types';
import { useApi } from './hooks/useApi';

// ─── nav link class helper ────────────────────────────────────────────────────

function navClass(isActive: boolean): string {
  return `text-xs px-3 py-1 rounded border transition-colors font-mono ${
    isActive
      ? 'border-teal text-teal bg-teal/10'
      : 'border-border text-text-muted hover:border-teal/50 hover:text-text-primary'
  }`;
}

// ─── stock detail page (route /stock/:symbol) ─────────────────────────────────

function StockDetailPage({ onSelectSymbol }: { onSelectSymbol: (s: string | null) => void }) {
  const { symbol } = useParams<{ symbol: string }>();
  if (!symbol) return null;
  return <StockDetail symbol={symbol} onBack={() => onSelectSymbol(null)} />;
}

// ─── overview page (route /) ──────────────────────────────────────────────────

function OverviewContent({
  selectedSymbol,
  onSelectSymbol,
}: {
  selectedSymbol: string | null;
  onSelectSymbol: (s: string | null) => void;
}) {
  return selectedSymbol ? (
    <StockDetail symbol={selectedSymbol} onBack={() => onSelectSymbol(null)} />
  ) : (
    <StockOverview onSelectSymbol={onSelectSymbol} />
  );
}

// ─── inner app (has access to router context) ─────────────────────────────────

function AppInner() {
  const [selectedSymbol, setSelectedSymbol] = useState<string | null>(null);
  const { data: healthData } = useApi<HealthData>('/api/health', 60000);

  const handleSelectSymbol = useCallback((symbol: string | null) => {
    setSelectedSymbol(symbol);
  }, []);

  return (
    <div className="flex flex-col h-screen bg-bg text-text-primary overflow-hidden">
      {/* ── Header ────────────────────────────────────────────────────────── */}
      <header
        className="flex items-center justify-between px-4 py-2 bg-surface border-b border-border flex-shrink-0 z-20"
        style={{ height: '44px' }}
      >
        {/* Left: logo + tagline */}
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2">
            <span className="text-teal font-mono font-bold text-lg tracking-wider">⬡</span>
            <span className="font-mono font-bold text-base tracking-widest text-text-primary">
              ALPHA<span className="text-teal">RADAR</span>
            </span>
          </div>
          <div className="h-4 w-px bg-border" />
          <span className="text-text-muted text-xs font-mono">A股+美股 投研分析系统</span>
        </div>

        {/* Right: nav + current symbol chip + clock */}
        <div className="flex items-center gap-4">
          {/* Navigation tabs */}
          <nav className="flex items-center gap-1.5">
            <NavLink to="/" end className={({ isActive }) => navClass(isActive)}>
              总览
            </NavLink>
            <NavLink to="/review" className={({ isActive }) => navClass(isActive)}>
              复盘
            </NavLink>
            <NavLink to="/health" className={({ isActive }) => navClass(isActive)}>
              健康
            </NavLink>
            <NavLink to="/quality" className={({ isActive }) => navClass(isActive)}>
              质量
            </NavLink>
          </nav>

          {/* Selected symbol chip (only on overview route) */}
          {selectedSymbol && (
            <div className="flex items-center gap-2">
              <div className="h-4 w-px bg-border" />
              <span className="text-xs font-mono text-teal">{selectedSymbol}</span>
              <button
                onClick={() => setSelectedSymbol(null)}
                className="text-text-muted hover:text-text-primary text-xs ml-1"
              >
                ✕
              </button>
            </div>
          )}

          <div className="h-4 w-px bg-border" />

          {/* Status dot + clock */}
          <div className="flex items-center gap-1.5">
            <div
              className={`w-1.5 h-1.5 rounded-full pulse-dot ${
                healthData?.status === 'ok'
                  ? 'bg-teal'
                  : healthData?.status === 'warning'
                  ? 'bg-amber'
                  : 'bg-red'
              }`}
            />
            <span className="text-text-muted text-xs font-mono">
              {new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}
            </span>
          </div>
        </div>
      </header>

      {/* ── Body ──────────────────────────────────────────────────────────── */}
      <div className="flex flex-1 overflow-hidden">
        {/* Sidebar always visible */}
        <Sidebar selectedSymbol={selectedSymbol} onSelectSymbol={handleSelectSymbol} />

        {/* Routed content area */}
        <main className="flex-1 overflow-hidden bg-bg">
          <Routes>
            <Route
              path="/"
              element={
                <OverviewContent
                  selectedSymbol={selectedSymbol}
                  onSelectSymbol={handleSelectSymbol}
                />
              }
            />
            <Route path="/review" element={<ReviewPage />} />
            <Route path="/health" element={<HealthPage />} />
            <Route path="/quality" element={<QualityPage />} />
            <Route
              path="/stock/:symbol"
              element={<StockDetailPage onSelectSymbol={handleSelectSymbol} />}
            />
          </Routes>
        </main>
      </div>

      {/* ── Status bar ────────────────────────────────────────────────────── */}
      <StatusBar healthData={healthData} />
    </div>
  );
}

// ─── root export (provides BrowserRouter context) ─────────────────────────────

export default function App() {
  return (
    <BrowserRouter>
      <AppInner />
    </BrowserRouter>
  );
}
