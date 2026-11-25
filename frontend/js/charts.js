/**
 * Interactive pure SVG data visualizations — Modern Tech SaaS Suite.
 *
 * 1. Sales Distribution: Glowing Monotone Cubic Spline Area Chart
 * 2. Price Distribution: Interactive Donut Ring + 5-Band Breakdown
 * 3. Price vs Sales: Category-coded Interactive Scatter Plot
 * 4. Hot Print Keywords: Archimedean Spiral Collision-Free SVG Word Cloud
 */

import { escapeHtml, formatInt, formatDecimal } from "./format.js";

const tooltipEl = () => document.getElementById("chart-tooltip");

export function showTooltip(event, html) {
  const el = tooltipEl();
  if (!el) return;
  el.innerHTML = html;
  el.classList.add("is-visible");
  el.setAttribute("aria-hidden", "false");
  positionTooltip(event);
}

export function positionTooltip(event) {
  const el = tooltipEl();
  if (!el || !el.classList.contains("is-visible")) return;
  const pad = 14;
  const rect = el.getBoundingClientRect();
  let left = (event.clientX || 0) + pad;
  let top = (event.clientY || 0) + pad;
  if (left + rect.width > window.innerWidth - 12) {
    left = Math.max(12, (event.clientX || 0) - rect.width - pad);
  }
  if (top + rect.height > window.innerHeight - 12) {
    top = Math.max(12, (event.clientY || 0) - rect.height - pad);
  }
  el.style.left = `${left}px`;
  el.style.top = `${top}px`;
}

export function hideTooltip() {
  const el = tooltipEl();
  if (!el) return;
  el.classList.remove("is-visible");
  el.setAttribute("aria-hidden", "true");
}

/**
 * Compute Fritsch-Carlson Monotone Cubic Hermite Spline SVG path
 * so the smooth curve never overshoots below zero or creates loops.
 */
function buildMonotoneSplinePath(pts) {
  if (!pts.length) return "";
  if (pts.length === 1) return `M ${pts[0].x.toFixed(1)} ${pts[0].y.toFixed(1)}`;
  const n = pts.length;
  const dx = [];
  const dy = [];
  const slopes = [];
  for (let i = 0; i < n - 1; i++) {
    dx.push(pts[i + 1].x - pts[i].x);
    dy.push(pts[i + 1].y - pts[i].y);
    slopes.push(dy[i] / (dx[i] || 1));
  }
  const tangents = new Array(n).fill(0);
  tangents[0] = slopes[0];
  tangents[n - 1] = slopes[n - 2];
  for (let i = 1; i < n - 1; i++) {
    if (slopes[i - 1] * slopes[i] <= 0) {
      tangents[i] = 0;
    } else {
      tangents[i] = (slopes[i - 1] + slopes[i]) / 2;
    }
  }
  // Enforce monotonicity constraint
  for (let i = 0; i < n - 1; i++) {
    if (Math.abs(slopes[i]) < 1e-6) {
      tangents[i] = 0;
      tangents[i + 1] = 0;
    } else {
      const a = tangents[i] / slopes[i];
      const b = tangents[i + 1] / slopes[i];
      const h = Math.hypot(a, b);
      if (h > 3) {
        const t = 3 / h;
        tangents[i] = t * a * slopes[i];
        tangents[i + 1] = t * b * slopes[i];
      }
    }
  }

  let d = `M ${pts[0].x.toFixed(1)} ${pts[0].y.toFixed(1)}`;
  for (let i = 0; i < n - 1; i++) {
    const step = dx[i] / 3;
    const cp1x = pts[i].x + step;
    const cp1y = pts[i].y + tangents[i] * step;
    const cp2x = pts[i + 1].x - step;
    const cp2y = pts[i + 1].y - tangents[i + 1] * step;
    d += ` C ${cp1x.toFixed(1)} ${cp1y.toFixed(1)}, ${cp2x.toFixed(1)} ${cp2y.toFixed(1)}, ${pts[i + 1].x.toFixed(1)} ${pts[i + 1].y.toFixed(1)}`;
  }
  return d;
}

function niceAxisMax(rawMax, steps = 4) {
  const safeMax = Math.max(1, Number(rawMax) || 1);
  const roughStep = safeMax / Math.max(1, steps);
  const mag = Math.pow(10, Math.floor(Math.log10(roughStep)));
  const norm = roughStep / mag;
  let niceStep;
  if (norm <= 1) niceStep = 1 * mag;
  else if (norm <= 2) niceStep = 2 * mag;
  else if (norm <= 2.5) niceStep = 2.5 * mag;
  else if (norm <= 5) niceStep = 5 * mag;
  else niceStep = 10 * mag;
  return Math.ceil(safeMax / niceStep) * niceStep;
}

function formatCompactTick(val) {
  if (val >= 1000000) return `${(val / 1000000).toFixed(val % 1000000 === 0 ? 0 : 1)}M`;
  if (val >= 1000) return `${(val / 1000).toFixed(val % 1000 === 0 ? 0 : 1)}k`;
  return String(Math.round(val));
}

function percentileVal(sorted, p) {
  if (!sorted.length) return 0;
  const idx = Math.min(sorted.length - 1, Math.max(0, (sorted.length - 1) * p));
  const lo = Math.floor(idx);
  const hi = Math.ceil(idx);
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (idx - lo);
}

/**
 * 1. Sales 30d Estimated Distribution — Glowing Tech Smooth Spline Area Chart
 */
export function renderSalesSplineAreaSvg(histogram) {
  if (!histogram || !histogram.length || histogram.every((b) => !b.count)) {
    return `<div class="empty-state">该类目暂无已采快照数据</div>`;
  }
  const W = 600;
  const H = 196;
  const m = { top: 28, right: 26, bottom: 38, left: 42 };
  const plotW = W - m.left - m.right;
  const plotH = H - m.top - m.bottom;
  const maxVal = Math.max(1, ...histogram.map((b) => b.count));
  const yMax = niceAxisMax(maxVal * 1.18, 4);
  const baseY = m.top + plotH;

  const yTicks = [0, 0.25, 0.5, 0.75, 1].map((t) => Math.round(yMax * t));
  const gridLines = yTicks
    .map((val) => {
      const y = baseY - (val / yMax) * plotH;
      return `
        <line x1="${m.left}" y1="${y.toFixed(1)}" x2="${W - m.right}" y2="${y.toFixed(1)}" stroke="#E8EDF5" stroke-width="1" />
        <text x="${m.left - 10}" y="${(y + 4).toFixed(1)}" text-anchor="end" fill="#64748B" font-size="11" style="font-variant-numeric:tabular-nums;">${val}</text>
      `;
    })
    .join("");

  const innerPad = 18;
  const pts = histogram.map((bucket, idx) => {
    const x = histogram.length === 1
      ? m.left + plotW / 2
      : m.left + innerPad + (idx / (histogram.length - 1)) * (plotW - innerPad * 2);
    const y = baseY - (bucket.count / yMax) * plotH;
    return { x, y, ...bucket };
  });

  const peakCount = Math.max(...pts.map((p) => p.count));
  const linePath = buildMonotoneSplinePath(pts);
  const areaPath = `${linePath} L ${pts[pts.length - 1].x.toFixed(1)} ${baseY.toFixed(1)} L ${pts[0].x.toFixed(1)} ${baseY.toFixed(1)} Z`;

  const nodesHtml = pts
    .map((p) => {
      const isPeak = p.count === peakCount && p.count > 0;
      const countText = String(p.count);
      const pillW = Math.max(30, countText.length * 8 + 14);
      const tip = escapeHtml(
        `<strong>${p.label} 件 / 30天</strong><br>样本数：${p.count} 件（占 ${p.share_pct}%）<br><span style="color:#94A3B8">第三方 Listing 估算销量</span>`
      );
      return `
        <g tabindex="0" role="button" aria-label="${escapeHtml(p.label)} 区间 ${p.count} 件" data-tip="${tip}">
          <line x1="${p.x.toFixed(1)}" y1="${p.y.toFixed(1)}" x2="${p.x.toFixed(1)}" y2="${baseY.toFixed(1)}"
            stroke="${isPeak ? "rgba(37,82,217,0.32)" : "rgba(100,116,139,0.18)"}" stroke-width="1" />
          ${isPeak ? `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="9" fill="rgba(37,82,217,0.12)" />` : ""}
          <circle class="spline-node" cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${isPeak ? 5 : 4}"
            fill="${isPeak ? "#2552D9" : "#FFFFFF"}" stroke="${isPeak ? "#FFFFFF" : "#2552D9"}" stroke-width="2.2" />
          <rect x="${(p.x - pillW / 2).toFixed(1)}" y="${(p.y - 25).toFixed(1)}" width="${pillW}" height="18" rx="4"
            fill="${isPeak ? "#2552D9" : "#FFFFFF"}" stroke="${isPeak ? "#2552D9" : "#CBD5E1"}" stroke-width="1" />
          <text x="${p.x.toFixed(1)}" y="${(p.y - 12.5).toFixed(1)}" text-anchor="middle"
            fill="${isPeak ? "#FFFFFF" : "#0F172A"}" font-size="11" font-weight="700" style="font-variant-numeric:tabular-nums;">${countText}</text>
          <text x="${p.x.toFixed(1)}" y="${H - 19}" text-anchor="middle" fill="#334155" font-size="11" font-weight="600">${escapeHtml(p.label)}</text>
          <text x="${p.x.toFixed(1)}" y="${H - 5}" text-anchor="middle" fill="#64748B" font-size="10.5" style="font-variant-numeric:tabular-nums;">${p.share_pct}%</text>
        </g>
      `;
    })
    .join("");

  return `
    <svg class="chart-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="近30天估算销量平滑分布曲线">
      <defs>
        <linearGradient id="splineAreaGrad" x1="0%" y1="0%" x2="0%" y2="100%">
          <stop offset="0%" stop-color="#2552D9" stop-opacity="0.16" />
          <stop offset="70%" stop-color="#2552D9" stop-opacity="0.04" />
          <stop offset="100%" stop-color="#2552D9" stop-opacity="0.0" />
        </linearGradient>
      </defs>
      ${gridLines}
      <path d="${areaPath}" fill="url(#splineAreaGrad)" />
      <path d="${linePath}" fill="none" stroke="#2552D9" stroke-width="2.4" stroke-linecap="round" />
      ${nodesHtml}
    </svg>
  `;
}

/**
 * 2. Price Bands Distribution — Disciplined Sapphire-to-Slate Donut Ring + Breakdown List
 */
const DONUT_COLORS = ["#2552D9", "#0F172A", "#475569", "#94A3B8", "#CBD5E1"];

export function renderPriceDonutHtml(bands, priceStats, activeBand = "all") {
  if (!bands || !bands.length || bands.every((b) => !b.count)) {
    return `<div class="empty-state">该类目暂无有效售价样本</div>`;
  }
  const total = bands.reduce((acc, b) => acc + (b.count || 0), 0) || 1;
  const cx = 90;
  const cy = 90;
  const r = 64;
  const gapAngle = 0.065; // crisp spacing between arcs (radians)

  const polar = (angle) => ({
    x: cx + r * Math.cos(angle),
    y: cy + r * Math.sin(angle),
  });

  let cursor = -Math.PI / 2;
  const arcs = bands.map((band, idx) => {
    const color = DONUT_COLORS[idx % DONUT_COLORS.length];
    const sweep = (band.count / total) * Math.PI * 2;
    const startA = cursor + gapAngle / 2;
    const endA = cursor + Math.max(gapAngle + 0.02, sweep) - gapAngle / 2;
    cursor += sweep;

    if (band.count <= 0) return "";
    const p1 = polar(startA);
    const p2 = polar(endA);
    const largeArc = endA - startA > Math.PI ? 1 : 0;
    const d = `M ${p1.x.toFixed(2)} ${p1.y.toFixed(2)} A ${r} ${r} 0 ${largeArc} 1 ${p2.x.toFixed(2)} ${p2.y.toFixed(2)}`;

    const isActive = activeBand === band.key;
    const dim = activeBand !== "all" && !isActive;
    const tip = escapeHtml(
      `<strong>${band.label}</strong><br>有效样本：${band.count} 件（${band.share_pct}%）<br>该价位带中位销量：${band.median_sales_30d !== null ? formatInt(band.median_sales_30d) : "—"}<br><span style="color:#93C5FD">点击筛选下方商品表</span>`
    );

    return `
      <path class="donut-arc" d="${d}" fill="none"
        stroke="${color}" stroke-width="${isActive ? 25 : 21}"
        stroke-linecap="round" opacity="${dim ? 0.28 : 1}"
        tabindex="0" role="button" data-price-band="${escapeHtml(band.key)}" data-tip="${tip}"
        aria-label="价格带 ${escapeHtml(band.label)}，${band.count} 件（${band.share_pct}%）" />
    `;
  }).join("");

  const medianText = priceStats && priceStats.median !== null ? `$${formatDecimal(priceStats.median, 2)}` : "—";

  const listHtml = bands.map((band, idx) => {
    const color = DONUT_COLORS[idx % DONUT_COLORS.length];
    const isActive = activeBand === band.key;
    const medStr = band.median_sales_30d !== null ? `中位 ${formatInt(band.median_sales_30d)}` : "—";
    return `
      <button type="button" class="donut-row ${isActive ? "is-active" : ""}" data-price-band="${escapeHtml(band.key)}"
        title="点击筛选 ${escapeHtml(band.label)} 价格带商品">
        <div class="donut-row-top">
          <span style="display:inline-flex;align-items:center;gap:7px;font-weight:600;color:var(--ink-primary);">
            <span class="cat-dot" style="background:${color};width:8px;height:8px;"></span>
            ${escapeHtml(band.label)}
          </span>
          <span class="num" style="color:var(--ink-muted);">
            <strong style="color:var(--ink-primary);">${band.count}</strong> 件 (${band.share_pct}%) · ${medStr}
          </span>
        </div>
        <div class="donut-row-bar">
          <div class="donut-row-fill" style="width:${Math.max(4, band.share_pct)}%;background:${color};"></div>
        </div>
      </button>
    `;
  }).join("");

  return `
    <div class="donut-layout">
      <div class="donut-svg-wrap">
        <svg class="chart-svg" viewBox="0 0 180 180" role="img" aria-label="有效售价区间环形占比图">
          <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="#EEF2F6" stroke-width="18" />
          ${arcs}
          <text x="${cx}" y="${cy - 4}" text-anchor="middle" fill="#0F172A" font-size="20" font-weight="700" style="font-variant-numeric:tabular-nums;">${medianText}</text>
          <text x="${cx}" y="${cy + 14}" text-anchor="middle" fill="#64748B" font-size="11">中位有效售价</text>
          <text x="${cx}" y="${cy + 28}" text-anchor="middle" fill="#94A3B8" font-size="10" style="font-variant-numeric:tabular-nums;">有效 ${total} 件</text>
        </svg>
      </div>
      <div class="donut-list">${listHtml}</div>
    </div>
  `;
}

/**
 * 3. Price vs Listing 30d Sales Interactive Scatter Plot
 * Supports "focused" mode (Log10 Y-axis + P98-focused X-axis with dedicated outlier lane
 * so 400–169,000 sales and $4–$206 prices never crush points) and "linear" raw full-range mode.
 */
export function renderPriceSalesScatterSvg(
  points,
  { scaleMode = "focused", medianPrice = null, medianSales = null } = {}
) {
  if (!points || !points.length) {
    return `<div class="empty-state">暂无可绘制样本</div>`;
  }

  const W = 1020;
  const H = 252;
  const m = { top: 18, right: 24, bottom: 36, left: 58 };
  const totalPlotW = W - m.left - m.right;
  const plotH = H - m.top - m.bottom;

  const pricesSorted = points.map((p) => p.price_usd).sort((a, b) => a - b);
  const salesSorted = points.map((p) => Math.max(1, p.sales_30d)).sort((a, b) => a - b);
  const rawMaxPrice = pricesSorted[pricesSorted.length - 1];
  const rawMinSales = salesSorted[0];
  const rawMaxSales = salesSorted[salesSorted.length - 1];

  const isFocused = scaleMode !== "linear";
  const p98Price = percentileVal(pricesSorted, 0.98);
  const focusCapPrice = Math.min(60, Math.max(40, Math.ceil(p98Price / 10) * 10));
  const outlierPrices = isFocused ? points.filter((p) => p.price_usd > focusCapPrice) : [];
  const hasOutlierLane = isFocused && outlierPrices.length > 0;

  const mainPlotW = hasOutlierLane ? Math.round(totalPlotW * 0.875) : totalPlotW;
  const laneXStart = m.left + mainPlotW + 14;
  const laneW = Math.max(44, totalPlotW - mainPlotW - 14);
  const xMaxMain = isFocused ? (hasOutlierLane ? focusCapPrice : niceAxisMax(Math.max(40, rawMaxPrice), 5)) : niceAxisMax(Math.max(40, rawMaxPrice), 5);

  const xScale = (price) => {
    if (hasOutlierLane && price > xMaxMain) {
      const span = Math.max(1, rawMaxPrice - xMaxMain);
      const t = Math.log10(1 + (price - xMaxMain)) / Math.log10(1 + span);
      return laneXStart + 10 + t * Math.max(12, laneW - 20);
    }
    return m.left + (Math.min(price, xMaxMain) / xMaxMain) * mainPlotW;
  };

  // Y-axis: Log10 in focused mode (prevents 169k outliers from flattening 800-3,000 items), Linear in linear mode
  let yScale;
  let yTicks;
  if (isFocused) {
    const logMin = Math.floor(Math.log10(Math.max(100, rawMinSales * 0.75)) * 2) / 2;
    const logMax = Math.ceil(Math.log10(Math.max(2000, rawMaxSales * 1.25)) * 2) / 2;
    const safeSpan = Math.max(0.5, logMax - logMin);
    yScale = (val) => {
      const lv = Math.log10(Math.max(10, val));
      const clamped = Math.min(logMax, Math.max(logMin, lv));
      return m.top + plotH - ((clamped - logMin) / safeSpan) * plotH;
    };
    const candidates = [100, 200, 300, 500, 1000, 2000, 3000, 5000, 10000, 20000, 30000, 50000, 100000, 200000, 500000];
    const inRange = candidates.filter((v) => Math.log10(v) >= logMin - 0.01 && Math.log10(v) <= logMax + 0.01);
    // Keep at most 6 well-spaced ticks so labels never collide vertically
    yTicks = [];
    let lastY = Infinity;
    for (const v of inRange) {
      const y = yScale(v);
      if (Math.abs(lastY - y) >= 34) {
        yTicks.push(v);
        lastY = y;
      }
    }
    if (!yTicks.length) yTicks = [Math.round(rawMinSales), Math.round(rawMaxSales)];
  } else {
    const yMaxLinear = niceAxisMax(Math.max(1000, rawMaxSales * 1.08), 4);
    yScale = (val) => m.top + plotH - (Math.max(0, val) / yMaxLinear) * plotH;
    yTicks = [0, 0.25, 0.5, 0.75, 1].map((t) => Math.round(yMaxLinear * t));
  }

  const xStep = xMaxMain <= 50 ? 10 : niceAxisMax(xMaxMain / 5, 1);
  const xTicks = [];
  for (let v = 0; v <= xMaxMain + 0.01; v += xStep) {
    xTicks.push(Math.round(v));
  }

  const outlierLaneSvg = hasOutlierLane
    ? `
      <rect x="${laneXStart}" y="${m.top}" width="${laneW}" height="${plotH}" rx="8" fill="#F8FAFC" stroke="#E2E8F0" stroke-dasharray="4 3" />
      <text x="${laneXStart + laneW / 2}" y="${m.top + 14}" text-anchor="middle" fill="#64748B" font-size="10" font-weight="600">极值区 (&gt;$${xMaxMain})</text>
      <text x="${laneXStart + laneW / 2}" y="${H - 12}" text-anchor="middle" fill="#94A3B8" font-size="10.5">$${Math.round(rawMaxPrice)}</text>
    `
    : "";

  const grid = [
    ...yTicks.map((v) => {
      const y = yScale(v);
      return `
        <line x1="${m.left}" y1="${y.toFixed(1)}" x2="${m.left + mainPlotW}" y2="${y.toFixed(1)}" stroke="#EEF2F7" />
        <text x="${m.left - 10}" y="${(y + 4).toFixed(1)}" text-anchor="end" fill="#94A3B8" font-size="11">${formatCompactTick(v)}</text>
      `;
    }),
    ...xTicks.map((v) => {
      const x = xScale(v);
      return `
        <line x1="${x.toFixed(1)}" y1="${m.top}" x2="${x.toFixed(1)}" y2="${m.top + plotH}" stroke="#F8FAFC" />
        <text x="${x.toFixed(1)}" y="${H - 12}" text-anchor="middle" fill="#94A3B8" font-size="11">$${v}</text>
      `;
    }),
    `<text x="${m.left + mainPlotW / 2}" y="${H - 1}" text-anchor="middle" fill="#94A3B8" font-size="10">有效售价 (USD)</text>`,
    `<text x="14" y="${m.top + plotH / 2}" text-anchor="middle" fill="#94A3B8" font-size="10" transform="rotate(-90 14 ${m.top + plotH / 2})">${isFocused ? "30天销量 (对数)" : "30天销量 (线性)"}</text>`,
  ].join("");

  // Subtle median reference crosshairs + sweet-spot quadrant shading (high sales & above-median price)
  let medianSvg = "";
  const hasMx = medianPrice !== null && medianPrice > 0 && medianPrice <= xMaxMain;
  const mx = hasMx ? xScale(medianPrice) : null;
  const hasMy = medianSales !== null && medianSales > 0;
  const my = hasMy ? yScale(medianSales) : null;
  if (hasMx && hasMy && my >= m.top + 12 && my <= m.top + plotH - 12 && (m.left + mainPlotW - mx) > 80) {
    medianSvg += `
      <rect x="${mx.toFixed(1)}" y="${m.top}" width="${(m.left + mainPlotW - mx).toFixed(1)}" height="${(my - m.top).toFixed(1)}" fill="rgba(37,82,217,0.045)" />
      <text x="${(m.left + mainPlotW - 8).toFixed(1)}" y="${m.top + 14}" text-anchor="end" fill="#2552D9" font-size="10.5" font-weight="600" opacity="0.82">高销高价甜点区</text>
    `;
  }
  if (hasMx) {
    medianSvg += `
      <line x1="${mx.toFixed(1)}" y1="${m.top}" x2="${mx.toFixed(1)}" y2="${m.top + plotH}" stroke="#94A3B8" stroke-dasharray="4 4" stroke-width="1" opacity="0.7" />
      <rect x="${(mx - 36).toFixed(1)}" y="${m.top + 2}" width="72" height="16" rx="4" fill="#F1F5F9" />
      <text x="${mx.toFixed(1)}" y="${m.top + 13}" text-anchor="middle" fill="#475569" font-size="10" font-weight="600" style="font-variant-numeric:tabular-nums;">中位 $${formatDecimal(medianPrice, 2)}</text>
    `;
  }
  if (hasMy && my >= m.top + 12 && my <= m.top + plotH - 12) {
    medianSvg += `
      <line x1="${m.left}" y1="${my.toFixed(1)}" x2="${m.left + mainPlotW}" y2="${my.toFixed(1)}" stroke="#94A3B8" stroke-dasharray="4 4" stroke-width="1" opacity="0.7" />
      <rect x="${(m.left + mainPlotW - 86).toFixed(1)}" y="${(my - 17).toFixed(1)}" width="84" height="15" rx="4" fill="#F1F5F9" />
      <text x="${(m.left + mainPlotW - 44).toFixed(1)}" y="${(my - 6.5).toFixed(1)}" text-anchor="middle" fill="#475569" font-size="10" font-weight="600" style="font-variant-numeric:tabular-nums;">中位 ${formatInt(medianSales)}</text>
    `;
  }

  const colorMap = {
    womens_novelty_sweatshirts: "#D14D72",
    mens_novelty_sweatshirts: "#0D7C8C",
    womens_tshirts: "#E11D48",
    mens_tshirts: "#0284C7",
    womens_novelty_tshirts: "#EC4899",
    mens_novelty_tshirts: "#2563EB",
    women: "#E14D7A",
    men: "#0D9488",
    both: "#7C3AED",
  };

  const dense = points.length > 220;
  const dots = points
    .map((pt) => {
      const cx = xScale(pt.price_usd).toFixed(1);
      const cy = yScale(pt.sales_30d).toFixed(1);
      const fill = pt.category_color || colorMap[pt.category_group] || "#3B6BF6";
      const isOutlier = hasOutlierLane && pt.price_usd > xMaxMain;
      const r = isOutlier ? 5.6 : dense ? (pt.sales_30d >= 20000 ? 5.2 : 4.0) : (pt.sales_30d >= 5000 ? 6.0 : 4.8);
      const stroke = pt.needs_parent_verification ? "#D97706" : "#FFFFFF";
      const strokeW = pt.needs_parent_verification ? 1.8 : 1.1;
      const tip = escapeHtml(
        `<strong>${pt.asin}</strong> · ${pt.category_label}${isOutlier ? "（高价极值）" : ""}<br>${(pt.title || "").slice(0, 56)}${pt.title && pt.title.length > 56 ? "…" : ""}<br>售价：$${formatDecimal(pt.price_usd, 2)} · 30天销量：${formatInt(pt.sales_30d)}<br>评分：${pt.rating_stars !== null ? `${pt.rating_stars}★ (${formatInt(pt.ratings_count)})` : "暂无评分"}<br><span style="color:#93C5FD">点击查看商品详情</span>`
      );
      return `
        <circle class="scatter-dot" cx="${cx}" cy="${cy}" r="${r}" fill="${fill}" fill-opacity="${dense ? "0.72" : "0.8"}"
          stroke="${stroke}" stroke-width="${strokeW}" tabindex="0" role="button"
          data-open-asin="${escapeHtml(pt.asin)}" data-tip="${tip}"
          aria-label="ASIN ${escapeHtml(pt.asin)} 售价 $${pt.price_usd} 销量 ${pt.sales_30d}" />
      `;
    })
    .join("");

  return `
    <svg class="chart-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="售价与近30天估算销量散点图">
      ${outlierLaneSvg}
      ${grid}
      ${medianSvg}
      ${dots}
    </svg>
  `;
}

/**
 * 4. Hot Print Keywords — Archimedean Spiral Collision-Free SVG Word Cloud
 * Inspired by d3-cloud layout algorithm, deterministic and zero-overlap.
 */
const THEME_PALETTE = {
  holiday: ["#E11D48", "#BE123C", "#F43F5E"],
  motif: ["#0F766E", "#0D9488", "#115E59"],
  style: ["#2552D9", "#1D4ED8", "#3B6BF6"],
  vibe: ["#475569", "#334155", "#64748B"],
};

const THEME_LABELS = {
  holiday: "节日/季节",
  motif: "图案元素",
  style: "风格/工艺",
  vibe: "情绪/场景",
};

export function renderKeywordCloudSvg(keywords, activeQuery = "") {
  if (!keywords || !keywords.length) {
    return `<div class="empty-state">暂无足够文本提取关键词</div>`;
  }

  const W = 700;
  const H = 330;
  const cx = W / 2;
  const cy = H / 2;
  const placed = [];
  const activeLower = (activeQuery || "").trim().toLowerCase();

  const overlaps = (box) => {
    if (box.x1 < 10 || box.x2 > W - 10 || box.y1 < 10 || box.y2 > H - 10) {
      return true;
    }
    for (let i = 0; i < placed.length; i++) {
      const p = placed[i];
      if (!(box.x2 < p.x1 || box.x1 > p.x2 || box.y2 < p.y1 || box.y1 > p.y2)) {
        return true;
      }
    }
    return false;
  };

  const renderedWords = [];
  keywords.slice(0, 42).forEach((kw, idx) => {
    const wNorm = typeof kw.weight === "number" ? kw.weight : 0.5;
    // Scale font size cleanly from 12px to 32px, with safe width cap for long 2-gram / 3-gram phrases
    const rawFontSize = Math.round(12.5 + Math.pow(wNorm, 1.18) * 19.5);
    const charLen = Math.max(3, (kw.word || "").length);
    const maxFontForLen = charLen > 13 ? Math.max(12, Math.floor(236 / (charLen * 0.58))) : 32;
    const fontSize = Math.min(rawFontSize, maxFontForLen);
    const fontWeight = fontSize >= 22 ? 800 : fontSize >= 16 ? 700 : 600;
    const textW = charLen * fontSize * 0.58 + 8;
    const textH = fontSize * 1.08 + 3;
    const padX = 5;
    const padY = 3;

    // Archimedean elliptical spiral sweeping the full 700x330 canvas
    let found = null;
    const angleStart = (idx * 2.39996) % (Math.PI * 2);
    for (let step = 0; step < 960; step++) {
      const t = step * 0.11;
      const rx = 3.2 * t;
      const ry = 1.55 * t;
      const x = cx + rx * Math.cos(angleStart + t);
      const y = cy + ry * Math.sin(angleStart + t);
      const box = {
        x1: x - textW / 2 - padX,
        x2: x + textW / 2 + padX,
        y1: y - textH / 2 - padY,
        y2: y + textH / 2 + padY,
      };
      if (!overlaps(box)) {
        found = { x, y, box };
        break;
      }
    }

    if (!found) return;
    placed.push(found.box);

    const palette = THEME_PALETTE[kw.theme] || THEME_PALETTE.vibe;
    const color = palette[idx % palette.length];
    const isActive = activeLower && activeLower === kw.query.toLowerCase();
    const dim = activeLower && !isActive;
    const themeLabel = THEME_LABELS[kw.theme] || "主题词";
    const tip = escapeHtml(
      `<strong>${kw.word}</strong> · ${themeLabel}<br>覆盖商品：${kw.doc_count} 件（${kw.share_pct}%）<br>标题提及：${kw.title_count} 次 · 中位销量：${kw.median_sales_30d !== null ? formatInt(kw.median_sales_30d) : "—"}<br><span style="color:#93C5FD">点击筛选含该关键词的商品</span>`
    );

    renderedWords.push(`
      <text class="wc-word ${isActive ? "is-active" : ""}"
        x="0" y="${(fontSize * 0.34).toFixed(1)}"
        transform="translate(${found.x.toFixed(1)}, ${found.y.toFixed(1)})"
        data-x="${found.x.toFixed(2)}" data-y="${found.y.toFixed(2)}"
        data-hw="${(textW / 2 + padX).toFixed(2)}" data-hh="${(textH / 2 + padY).toFixed(2)}"
        data-size="${fontSize}"
        text-anchor="middle" font-size="${fontSize}" font-weight="${fontWeight}"
        fill="${color}" opacity="${dim ? 0.28 : 0.96}"
        tabindex="0" role="button" data-keyword="${escapeHtml(kw.query)}" data-tip="${tip}"
        aria-label="关键词 ${escapeHtml(kw.word)}，覆盖 ${kw.doc_count} 件商品">${escapeHtml(kw.word)}</text>
    `);
  });

  return `
    <svg class="chart-svg wc-physics-svg" viewBox="0 0 ${W} ${H}" data-wc-width="${W}" data-wc-height="${H}" role="img" aria-label="热门印花关键词动态词云图">
      <circle cx="${cx}" cy="${cy}" r="92" fill="none" stroke="rgba(37,82,217,0.06)" stroke-dasharray="4 6" />
      <circle cx="${cx}" cy="${cy}" r="158" fill="none" stroke="rgba(15,23,42,0.05)" stroke-dasharray="4 6" />
      ${renderedWords.join("")}
    </svg>
  `;
}

let wordCloudRafId = null;

export function stopWordCloudPhysics() {
  if (wordCloudRafId !== null) {
    cancelAnimationFrame(wordCloudRafId);
    wordCloudRafId = null;
  }
}

/**
 * Gentle 60fps floating drift + soft elastic collision bounce between SVG word cloud tokens.
 */
export function startWordCloudPhysics(container, { paused = false } = {}) {
  stopWordCloudPhysics();
  if (!container || paused) return;
  const svg = container.querySelector(".wc-physics-svg");
  if (!svg) return;
  if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    return;
  }

  const W = Number(svg.getAttribute("data-wc-width") || 700);
  const H = Number(svg.getAttribute("data-wc-height") || 330);
  const pad = 12;

  const nodes = Array.from(svg.querySelectorAll(".wc-word")).map((el, idx) => {
    const x = Number(el.getAttribute("data-x") || W / 2);
    const y = Number(el.getAttribute("data-y") || H / 2);
    const hw = Number(el.getAttribute("data-hw") || 28);
    const hh = Number(el.getAttribute("data-hh") || 12);
    const size = Number(el.getAttribute("data-size") || 16);
    const angle = ((idx * 137.508) % 360) * (Math.PI / 180);
    const speed = 0.16 + (idx % 5) * 0.04;
    const node = {
      el,
      x,
      y,
      hw,
      hh,
      mass: Math.max(10, size),
      vx: Math.cos(angle) * speed,
      vy: Math.sin(angle) * speed,
      phase: idx * 0.85,
      hovered: false,
    };
    el.addEventListener("mouseenter", () => {
      node.hovered = true;
    });
    el.addEventListener("mouseleave", () => {
      node.hovered = false;
    });
    return node;
  });

  if (!nodes.length) return;

  const maxSpeed = 0.52;
  const minSpeed = 0.11;
  let lastTs = performance.now();

  const step = (ts) => {
    if (!document.body.contains(svg)) {
      stopWordCloudPhysics();
      return;
    }
    if (document.hidden) {
      lastTs = ts;
      wordCloudRafId = requestAnimationFrame(step);
      return;
    }

    const dt = Math.min(2.0, Math.max(0.4, (ts - lastTs) / 16.67));
    lastTs = ts;

    // 1. Gentle ambient drift & boundary soft bounce
    for (let i = 0; i < nodes.length; i++) {
      const n = nodes[i];
      if (n.hovered) continue;

      n.vx += Math.sin(ts * 0.00045 + n.phase) * 0.0028 * dt;
      n.vy += Math.cos(ts * 0.00038 + n.phase * 1.3) * 0.0024 * dt;

      const sp = Math.hypot(n.vx, n.vy) || 0.001;
      if (sp > maxSpeed) {
        n.vx = (n.vx / sp) * maxSpeed;
        n.vy = (n.vy / sp) * maxSpeed;
      } else if (sp < minSpeed) {
        n.vx = (n.vx / sp) * minSpeed;
        n.vy = (n.vy / sp) * minSpeed;
      }

      n.x += n.vx * dt;
      n.y += n.vy * dt;

      const minX = pad + n.hw;
      const maxX = W - pad - n.hw;
      const minY = pad + n.hh;
      const maxY = H - pad - n.hh;

      if (n.x < minX) {
        n.x = minX;
        n.vx = Math.abs(n.vx) * 0.92;
      } else if (n.x > maxX) {
        n.x = maxX;
        n.vx = -Math.abs(n.vx) * 0.92;
      }
      if (n.y < minY) {
        n.y = minY;
        n.vy = Math.abs(n.vy) * 0.92;
      } else if (n.y > maxY) {
        n.y = maxY;
        n.vy = -Math.abs(n.vy) * 0.92;
      }
    }

    // 2. Soft pairwise elastic collision separation (AABB + gentle rebound)
    const gapX = 5;
    const gapY = 4;
    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j++) {
        const b = nodes[j];
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const overlapX = a.hw + b.hw + gapX - Math.abs(dx);
        const overlapY = a.hh + b.hh + gapY - Math.abs(dy);

        if (overlapX > 0 && overlapY > 0) {
          const invA = a.hovered ? 0 : 1 / a.mass;
          const invB = b.hovered ? 0 : 1 / b.mass;
          const invSum = invA + invB;
          if (invSum <= 0) continue;

          if (overlapX < overlapY) {
            const nx = dx >= 0 ? 1 : -1;
            const push = overlapX * 0.55;
            a.x -= nx * push * (invA / invSum);
            b.x += nx * push * (invB / invSum);

            const rvx = b.vx - a.vx;
            if (rvx * nx < 0) {
              const impulse = (-(1 + 0.86) * (rvx * nx)) / invSum;
              a.vx -= nx * impulse * invA;
              b.vx += nx * impulse * invB;
            } else {
              a.vx -= nx * 0.08 * (invA / invSum);
              b.vx += nx * 0.08 * (invB / invSum);
            }
          } else {
            const ny = dy >= 0 ? 1 : -1;
            const push = overlapY * 0.55;
            a.y -= ny * push * (invA / invSum);
            b.y += ny * push * (invB / invSum);

            const rvy = b.vy - a.vy;
            if (rvy * ny < 0) {
              const impulse = (-(1 + 0.86) * (rvy * ny)) / invSum;
              a.vy -= ny * impulse * invA;
              b.vy += ny * impulse * invB;
            } else {
              a.vy -= ny * 0.08 * (invA / invSum);
              b.vy += ny * 0.08 * (invB / invSum);
            }
          }
        }
      }
    }

    // 3. Apply transforms
    for (let i = 0; i < nodes.length; i++) {
      const n = nodes[i];
      const scale = n.hovered ? " scale(1.08)" : "";
      n.el.setAttribute("transform", `translate(${n.x.toFixed(1)}, ${n.y.toFixed(1)})${scale}`);
    }

    wordCloudRafId = requestAnimationFrame(step);
  };

  wordCloudRafId = requestAnimationFrame(step);
}

/**
 * Bind hover/focus tooltip & click events inside any container with SVG charts.
 */
export function bindChartInteractions(container, { onSelectPriceBand, onOpenAsin, onSelectKeyword } = {}) {
  if (!container) return;
  container.querySelectorAll("[data-tip]").forEach((node) => {
    const html = node.getAttribute("data-tip");
    node.addEventListener("mouseenter", (e) => showTooltip(e, html));
    node.addEventListener("mousemove", (e) => positionTooltip(e));
    node.addEventListener("mouseleave", hideTooltip);
    node.addEventListener("focus", () => {
      const rect = node.getBoundingClientRect();
      showTooltip({ clientX: rect.left + rect.width / 2, clientY: rect.top }, html);
    });
    node.addEventListener("blur", hideTooltip);
  });

  if (onSelectPriceBand) {
    container.querySelectorAll("[data-price-band]").forEach((node) => {
      const trigger = () => onSelectPriceBand(node.getAttribute("data-price-band"));
      node.addEventListener("click", trigger);
      node.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          trigger();
        }
      });
    });
  }

  if (onOpenAsin) {
    container.querySelectorAll("[data-open-asin]").forEach((node) => {
      const trigger = () => onOpenAsin(node.getAttribute("data-open-asin"));
      node.addEventListener("click", trigger);
      node.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          trigger();
        }
      });
    });
  }

  if (onSelectKeyword) {
    container.querySelectorAll("[data-keyword]").forEach((node) => {
      const trigger = () => onSelectKeyword(node.getAttribute("data-keyword"));
      node.addEventListener("click", trigger);
      node.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          trigger();
        }
      });
    });
  }
}
