import { useEffect, useRef } from 'react';
import {
  createChart,
  ColorType,
  CrosshairMode,
  LineStyle,
  CandlestickSeries,
  LineSeries,
  HistogramSeries,
  createSeriesMarkers,
} from 'lightweight-charts';
import type {
  IChartApi,
  ISeriesApi,
  UTCTimestamp,
  SeriesMarker,
  Time,
} from 'lightweight-charts';
import type { Bar, Signal, JudgmentHistory } from '../types';

interface KLineChartProps {
  bars: Bar[];
  movingAverages?: {
    ma5: (number | null)[];
    ma20: (number | null)[];
    ma60: (number | null)[];
    ma150: (number | null)[];
  };
  signals?: Signal[];
  judgments?: JudgmentHistory[];
  keyLevels?: {
    support?: number[];
    resistance?: number[];
    entryLow?: number | null;
    entryHigh?: number | null;
    stopLoss?: number | null;
    target?: number | null;
  };
  height?: number;
}

function parseTime(t: string): UTCTimestamp {
  return Math.floor(new Date(t).getTime() / 1000) as UTCTimestamp;
}

export default function KLineChart({
  bars,
  movingAverages,
  signals = [],
  judgments = [],
  keyLevels,
  height = 420,
}: KLineChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<'Candlestick'> | null>(null);

  useEffect(() => {
    if (!containerRef.current || bars.length === 0) return;

    const container = containerRef.current;

    // Create chart
    const chart = createChart(container, {
      layout: {
        background: { type: ColorType.Solid, color: '#0d1117' },
        textColor: '#8b949e',
        fontFamily: '"JetBrains Mono", monospace',
        fontSize: 11,
      },
      grid: {
        vertLines: { color: '#161b22', style: LineStyle.Solid },
        horzLines: { color: '#161b22', style: LineStyle.Solid },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: {
          color: '#30363d',
          width: 1,
          style: LineStyle.Dashed,
          labelBackgroundColor: '#161b22',
        },
        horzLine: {
          color: '#30363d',
          width: 1,
          style: LineStyle.Dashed,
          labelBackgroundColor: '#161b22',
        },
      },
      rightPriceScale: {
        borderColor: '#21262d',
        textColor: '#8b949e',
        scaleMargins: { top: 0.1, bottom: 0.25 },
      },
      timeScale: {
        borderColor: '#21262d',
        timeVisible: true,
        secondsVisible: false,
        fixLeftEdge: true,
        fixRightEdge: false,
      },
      width: container.clientWidth,
      height: height,
    });

    chartRef.current = chart;

    // Candlestick series
    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: '#00d4aa',
      downColor: '#f85149',
      borderVisible: false,
      wickUpColor: '#00d4aa',
      wickDownColor: '#f85149',
    });
    candleRef.current = candleSeries;

    const candleData = bars.map(b => ({
      time: parseTime(b.time),
      open: b.open,
      high: b.high,
      low: b.low,
      close: b.close,
    }));
    candleSeries.setData(candleData);

    // Moving averages
    const maConfigs = [
      { key: 'ma5', color: '#3b82f6', lineWidth: 1 as const },
      { key: 'ma20', color: '#f97316', lineWidth: 1 as const },
      { key: 'ma60', color: '#a855f7', lineWidth: 2 as const },
      { key: 'ma150', color: '#22c55e', lineWidth: 2 as const },
    ];

    if (movingAverages) {
      maConfigs.forEach(({ key, color, lineWidth }) => {
        const maValues = movingAverages[key as keyof typeof movingAverages];
        if (!maValues) return;

        const maSeries = chart.addSeries(LineSeries, {
          color,
          lineWidth,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
        });

        const maData = bars
          .map((b, i) => ({ time: parseTime(b.time), value: maValues[i] }))
          .filter(d => d.value != null) as { time: UTCTimestamp; value: number }[];

        maSeries.setData(maData);
      });
    }

    // Volume histogram (secondary scale)
    const volumeSeries = chart.addSeries(HistogramSeries, {
      priceFormat: { type: 'volume' },
      priceScaleId: 'volume',
      color: '#21262d',
    });

    chart.priceScale('volume').applyOptions({
      scaleMargins: { top: 0.8, bottom: 0 },
    });

    const volumeData = bars.map(b => ({
      time: parseTime(b.time),
      value: b.volume,
      color: b.close >= b.open ? 'rgba(0, 212, 170, 0.3)' : 'rgba(248, 81, 73, 0.3)',
    }));
    volumeSeries.setData(volumeData);

    // Build markers using createSeriesMarkers (v5 API)
    const markers: SeriesMarker<Time>[] = [];

    signals.forEach(sig => {
      const isBuy = sig.signal_type === 'buy';
      markers.push({
        time: parseTime(sig.signal_time) as Time,
        position: isBuy ? 'belowBar' : 'aboveBar',
        color: isBuy ? '#00d4aa' : '#f85149',
        shape: isBuy ? 'arrowUp' : 'arrowDown',
        text: `${isBuy ? 'B' : 'S'} ${sig.strength}`,
        size: 1,
      });
    });

    // Judgment history markers
    judgments.slice(-10).forEach(j => {
      const color =
        j.direction === 'bullish'
          ? '#3b82f6'
          : j.direction === 'bearish'
          ? '#9b59b6'
          : '#8b949e';
      markers.push({
        time: parseTime(j.judgment_date) as Time,
        position: 'aboveBar' as const,
        color,
        shape: 'circle' as const,
        text: '',
        size: 0.5,
      });
    });

    // Sort markers by time (required)
    markers.sort((a, b) => Number(a.time) - Number(b.time));

    if (markers.length > 0) {
      createSeriesMarkers(candleSeries, markers);
    }

    // Key level price lines
    if (keyLevels) {
      const addPriceLine = (price: number, color: string, title: string) => {
        candleSeries.createPriceLine({
          price,
          color,
          lineWidth: 1,
          lineStyle: LineStyle.Dashed,
          axisLabelVisible: true,
          title,
        });
      };

      keyLevels.support?.forEach(p => addPriceLine(p, '#00d4aa', 'S'));
      keyLevels.resistance?.forEach(p => addPriceLine(p, '#f85149', 'R'));
      if (keyLevels.entryLow) addPriceLine(keyLevels.entryLow, '#e3b341', '买入低');
      if (keyLevels.entryHigh) addPriceLine(keyLevels.entryHigh, '#e3b341', '买入高');
      if (keyLevels.stopLoss) addPriceLine(keyLevels.stopLoss, '#f85149', '止损');
      if (keyLevels.target) addPriceLine(keyLevels.target, '#00d4aa', '目标');
    }

    chart.timeScale().fitContent();

    // Resize observer
    const resizeObserver = new ResizeObserver(entries => {
      for (const entry of entries) {
        const { width } = entry.contentRect;
        chart.applyOptions({ width });
      }
    });
    resizeObserver.observe(container);

    return () => {
      resizeObserver.disconnect();
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
    };
  }, [bars, movingAverages, signals, judgments, keyLevels, height]);

  if (bars.length === 0) {
    return (
      <div
        className="flex flex-col items-center justify-center bg-surface border border-border rounded"
        style={{ height: `${height}px` }}
      >
        <div className="text-text-muted text-center">
          <div className="text-3xl mb-2 opacity-20">📈</div>
          <div className="text-sm">暂无K线数据</div>
        </div>
      </div>
    );
  }

  return (
    <div className="relative bg-surface rounded border border-border overflow-hidden">
      {/* MA Legend */}
      <div
        className="absolute top-2 left-3 z-10 flex items-center gap-3"
        style={{ pointerEvents: 'none' }}
      >
        {[
          { label: 'MA5', color: '#3b82f6' },
          { label: 'MA20', color: '#f97316' },
          { label: 'MA60', color: '#a855f7' },
          { label: 'MA150', color: '#22c55e' },
        ].map(({ label, color }) => (
          <div key={label} className="flex items-center gap-1">
            <div style={{ width: '12px', height: '2px', background: color }} />
            <span className="font-mono" style={{ fontSize: '10px', color }}>
              {label}
            </span>
          </div>
        ))}
      </div>
      <div ref={containerRef} style={{ height: `${height}px` }} />
    </div>
  );
}
