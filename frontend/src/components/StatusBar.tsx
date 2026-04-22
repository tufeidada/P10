import { useApi } from '../hooks/useApi';
import type { HealthData } from '../types';

interface StatusBarProps {
  healthData: HealthData | null;
}

interface StatsData {
  alpha_pct: number | null;
  accuracy_pct: number | null;
  total_judgments: number | null;
}

export default function StatusBar({ healthData }: StatusBarProps) {
  const { data: stats } = useApi<StatsData>('/api/stats/summary', 120000);
  const now = new Date();
  const timeStr = now.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });

  const statusOk = healthData?.status === 'ok';
  const statusWarn = healthData?.status === 'warning';

  return (
    <div
      className="flex items-center justify-between px-4 border-t border-border flex-shrink-0"
      style={{
        height: '40px',
        background: '#0a0d16',
        borderTop: '1px solid #21262d',
      }}
    >
      {/* Left: stats */}
      <div className="flex items-center gap-4">
        <div className="flex items-center gap-1.5">
          <span className="text-text-muted font-mono" style={{ fontSize: '10px' }}>Alpha:</span>
          <span
            className="font-mono font-semibold"
            style={{
              fontSize: '11px',
              color: stats?.alpha_pct != null
                ? (stats.alpha_pct >= 0 ? '#00d4aa' : '#f85149')
                : '#8b949e',
            }}
          >
            {stats?.alpha_pct != null
              ? `${stats.alpha_pct >= 0 ? '+' : ''}${stats.alpha_pct.toFixed(1)}%`
              : '--'}
          </span>
        </div>
        <div className="h-3 w-px bg-border" />
        <div className="flex items-center gap-1.5">
          <span className="text-text-muted font-mono" style={{ fontSize: '10px' }}>信号准确率:</span>
          <span
            className="font-mono font-semibold"
            style={{ fontSize: '11px', color: '#e6edf3' }}
          >
            {stats?.accuracy_pct != null
              ? `${stats.accuracy_pct.toFixed(0)}%`
              : '--'}
          </span>
        </div>
        <div className="h-3 w-px bg-border" />
        <div className="flex items-center gap-1.5">
          <span className="text-text-muted font-mono" style={{ fontSize: '10px' }}>总判断:</span>
          <span className="font-mono font-semibold text-text-primary" style={{ fontSize: '11px' }}>
            {stats?.total_judgments ?? '--'}
          </span>
        </div>
      </div>

      {/* Right: time and status */}
      <div className="flex items-center gap-3">
        <span className="text-text-muted font-mono" style={{ fontSize: '10px' }}>
          最后更新: {timeStr}
        </span>
        <div className="h-3 w-px bg-border" />
        <div className="flex items-center gap-1.5">
          <div
            className="w-1.5 h-1.5 rounded-full pulse-dot"
            style={{
              background: statusOk ? '#00d4aa' : statusWarn ? '#e3b341' : '#f85149',
            }}
          />
          <span
            className="font-mono"
            style={{
              fontSize: '10px',
              color: statusOk ? '#00d4aa' : statusWarn ? '#e3b341' : '#f85149',
            }}
          >
            {healthData == null
              ? '连接中...'
              : statusOk
              ? '数据正常'
              : statusWarn
              ? '数据告警'
              : '数据异常'}
          </span>
        </div>
      </div>
    </div>
  );
}
