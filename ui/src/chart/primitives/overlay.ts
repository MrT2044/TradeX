/**
 * Zeichen-Primitive fuer die Analyse-Overlays.
 *
 * lightweight-charts kann von Haus aus nur Serien zeichnen. Alles, was TradeX
 * zusaetzlich braucht - FVG-Rechtecke, Liquiditaetslinien - kommt ueber ein
 * Series-Primitive: ein Objekt, das Zugriff auf Zeit- und Preisachse bekommt
 * und selbst auf die Canvas malt.
 *
 * Bewusst EIN Primitive fuer beides, mit zwei Pane-Views:
 *   - Rechtecke im z-Order "bottom": sie liegen hinter den Kerzen
 *   - Linien im z-Order "normal":   sie liegen darueber und bleiben lesbar
 */

import type {
  IChartApi,
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  ISeriesApi,
  ISeriesPrimitive,
  PrimitivePaneViewZOrder,
  SeriesAttachedParameter,
  SeriesType,
  Time,
} from 'lightweight-charts';

export interface OverlayBox {
  /** Startzeit der Zone (Sekunden seit Epoch, wie bei lightweight-charts ueblich). */
  from: number;
  /** Endzeit; `null` zeichnet bis zum rechten Chartrand (Zone noch offen). */
  to: number | null;
  top: number;
  bottom: number;
  fill: string;
  border: string;
  /** Gestrichelter Rahmen fuer erledigte Zonen. */
  dashed?: boolean;
  label?: string;
}

export interface OverlayLine {
  from: number;
  to: number | null;
  price: number;
  color: string;
  width?: number;
  dashed?: boolean;
  label?: string;
}

interface Scope {
  context: CanvasRenderingContext2D;
  horizontalPixelRatio: number;
  verticalPixelRatio: number;
  bitmapSize: { width: number; height: number };
}

class OverlayRenderer implements IPrimitivePaneRenderer {
  constructor(private readonly primitive: OverlayPrimitive, private readonly kind: 'boxes' | 'lines') {}

  draw(target: {
    useBitmapCoordinateSpace: (fn: (scope: Scope) => void) => void;
  }): void {
    const chart = this.primitive.chart;
    const series = this.primitive.series;
    if (!chart || !series) return;

    const timeScale = chart.timeScale();
    // Zeit -> X. Liegt der Zeitpunkt links ausserhalb des sichtbaren Bereichs,
    // liefert die Bibliothek null; dann wird am linken Rand angesetzt, damit
    // eine angeschnittene Zone nicht komplett verschwindet.
    const toX = (time: number | null, fallback: number): number => {
      if (time === null) return fallback;
      const coordinate = timeScale.timeToCoordinate(time as Time);
      return coordinate === null ? fallback : coordinate;
    };

    target.useBitmapCoordinateSpace((scope) => {
      const ctx = scope.context;
      const hr = scope.horizontalPixelRatio;
      const vr = scope.verticalPixelRatio;
      const rightEdge = scope.bitmapSize.width / hr;

      ctx.save();
      if (this.kind === 'boxes') {
        for (const box of this.primitive.boxes) {
          const yTop = series.priceToCoordinate(box.top);
          const yBottom = series.priceToCoordinate(box.bottom);
          if (yTop === null || yBottom === null) continue;

          const x1 = toX(box.from, 0);
          const x2 = toX(box.to, rightEdge);
          if (x2 < 0 || x1 > rightEdge) continue;

          const left = Math.max(Math.min(x1, x2), 0) * hr;
          const right = Math.min(Math.max(x1, x2), rightEdge) * hr;
          const top = Math.min(yTop, yBottom) * vr;
          const height = Math.max(Math.abs(yBottom - yTop) * vr, 1);

          ctx.fillStyle = box.fill;
          ctx.fillRect(left, top, Math.max(right - left, 1), height);

          ctx.strokeStyle = box.border;
          ctx.lineWidth = Math.max(hr, 1);
          ctx.setLineDash(box.dashed ? [4 * hr, 3 * hr] : []);
          ctx.strokeRect(left, top, Math.max(right - left, 1), height);
        }
      } else {
        ctx.setLineDash([]);
        for (const line of this.primitive.lines) {
          const y = series.priceToCoordinate(line.price);
          if (y === null) continue;

          const x1 = toX(line.from, 0);
          const x2 = toX(line.to, rightEdge);
          if (x2 < 0 || x1 > rightEdge) continue;

          const left = Math.max(Math.min(x1, x2), 0) * hr;
          const right = Math.min(Math.max(x1, x2), rightEdge) * hr;

          ctx.strokeStyle = line.color;
          ctx.lineWidth = Math.max((line.width ?? 1) * hr, 1);
          ctx.setLineDash(line.dashed ? [5 * hr, 4 * hr] : []);
          ctx.beginPath();
          ctx.moveTo(left, y * vr);
          ctx.lineTo(right, y * vr);
          ctx.stroke();

          if (line.label) {
            ctx.setLineDash([]);
            ctx.font = `${Math.round(10 * vr)}px ui-monospace, monospace`;
            ctx.fillStyle = line.color;
            ctx.textBaseline = 'bottom';
            ctx.fillText(line.label, left + 4 * hr, y * vr - 2 * vr);
          }
        }
      }
      ctx.restore();
    });
  }
}

class OverlayPaneView implements IPrimitivePaneView {
  private readonly rendererInstance: OverlayRenderer;

  constructor(
    primitive: OverlayPrimitive,
    private readonly kind: 'boxes' | 'lines',
  ) {
    this.rendererInstance = new OverlayRenderer(primitive, kind);
  }

  zOrder(): PrimitivePaneViewZOrder {
    return this.kind === 'boxes' ? 'bottom' : 'normal';
  }

  renderer(): IPrimitivePaneRenderer {
    return this.rendererInstance;
  }
}

export class OverlayPrimitive implements ISeriesPrimitive<Time> {
  chart: IChartApi | null = null;
  series: ISeriesApi<SeriesType, Time> | null = null;
  boxes: OverlayBox[] = [];
  lines: OverlayLine[] = [];

  private readonly views: IPrimitivePaneView[] = [
    new OverlayPaneView(this, 'boxes'),
    new OverlayPaneView(this, 'lines'),
  ];
  private requestUpdate?: () => void;

  attached(param: SeriesAttachedParameter<Time>): void {
    this.chart = param.chart;
    this.series = param.series as ISeriesApi<SeriesType, Time>;
    this.requestUpdate = param.requestUpdate;
  }

  detached(): void {
    this.chart = null;
    this.series = null;
    this.requestUpdate = undefined;
  }

  setData(boxes: OverlayBox[], lines: OverlayLine[]): void {
    this.boxes = boxes;
    this.lines = lines;
    this.requestUpdate?.();
  }

  updateAllViews(): void {
    /* Die Renderer lesen die Daten direkt vom Primitive - nichts zu tun. */
  }

  paneViews(): readonly IPrimitivePaneView[] {
    return this.views;
  }
}
