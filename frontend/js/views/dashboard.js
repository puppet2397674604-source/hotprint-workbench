/**
 * Stage 03: Market Analytics Dashboard — Scheme A Executive Edition.
 * Dark Indigo Hero Pulse + 2x2 High-Contrast KPIs, Balanced Sales & Price Charts,
 * Open-Air Six-Category Panorama, Pre-Populated Interactive Scatter Plot,
 * Luminous Keyword Cloud, Gallery-Grade Top 8 Hot Samples, Real Brand Leaderboard
 * (filtering Generic), and Collapsible Sample Table with Active Filter Chips.
 */

import {
  bindChartInteractions,
  renderKeywordCloudSvg,
  renderPriceDonutHtml,
  renderPriceSalesScatterSvg,
  renderSalesSplineAreaSvg,
  startWordCloudPhysics,
} from "../charts.js";
import {
  escapeHtml,
  formatDecimal,
  formatInt,
  formatPrice,
  formatRatingHtml,
  renderAnomalyBadges,
  renderCategoryBadges,
  renderSafeImage,
} from "../format.js";

const ICON_ARROW_RIGHT = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14"/><path d="m12 5 7 7-7 7"/></svg>`;
const ICON_CHEVRON = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>`;

const KW_BAR_COLORS = {
  holiday: "#E11D48",
  motif: "#0F766E",
  style: "#2552D9",
  vibe: "#475569",
};

const GENERIC_BRAND_SET = new Set(["generic", "unknown", "unbranded", "n/a", "none", "未知", "无品牌", ""]);

const PRICE_BAND_LABELS = {
  under_15: "< $15",
  "15_20": "$15 – $19.99",
  "20_25": "$20 – $24.99",
  "25_35": "$25 – $34.99",
  "35_plus": "$35+",
  invalid: "仅异常价",
};

function isGenericBrand(name) {
  return GENERIC_BRAND_SET.has(String(name || "").trim().toLowerCase());
}

function renderBrandEmblem(b) {
  const bg = escapeHtml(b.brand_bg || "#EFF4FF");
  const initials = escapeHtml(b.brand_initials || "?");
  const fallbackJs = `this.parentElement.style.background='${bg}';this.outerHTML='<span>${initials}</span>'`;
  if (b.logo_url) {
    return `
      <span class="brand-emblem" style="background:#FFFFFF;color:var(--ink-primary);border:1px solid var(--border);" title="${escapeHtml(b.brand)} 品牌图标">
        <img src="${escapeHtml(b.logo_url)}" alt="${escapeHtml(b.brand)}" loading="lazy" referrerpolicy="no-referrer"
          onload="if(this.naturalWidth<=16){${fallbackJs}}"
          onerror="${fallbackJs}">
      </span>
    `;
  }
  return `
    <span class="brand-emblem" style="background:${bg};color:var(--ink-primary);border:1px solid var(--border);" title="${escapeHtml(b.brand)} 品牌徽标">
      <span>${initials}</span>
    </span>
  `;
}

export function renderDashboardView(
  container,
  {
    overview,
    productsPage,
    tableState,
    prepScope = "printable",
    scatterScaleMode = "focused",
    scatterSelectedCategories = [],
    tableExpanded = false,
    wordCloudPaused = false,
    handlers,
  }
) {
  if (!overview) {
    container.innerHTML = `<div class="loading-state"><div class="spinner"></div><p>正在加载市场数据…</p></div>`;
    return;
  }

  if (!overview.ready) {
    const st = overview.db_status || {};
    container.innerHTML = `
      <div class="empty-state">
        <span class="badge badge-warn">${escapeHtml(st.state || "unavailable")}</span>
        <h2 style="margin:6px 0 0;font-size:17px;color:var(--ink-primary);">暂无可用市场快照数据</h2>
        <p style="max-width:520px;margin:0;font-size:13px;">${escapeHtml(st.message || "未检测到有效的 SQLite 数据库内容。")}</p>
      </div>
    `;
    return;
  }

  const k = overview.kpis;
  const salesStats = overview.sales_analysis.stats;
  const priceStats = overview.price_analysis.stats;
  const bands = overview.price_analysis.bands || [];
  const peakBandCount = Math.max(0, ...bands.map((band) => band.count));
  const peakBands = bands.filter((band) => peakBandCount > 0 && band.count === peakBandCount);
  const peakBandText = peakBands.length ? peakBands.map((b) => b.label).join(" / ") : "—";
  const peakBandShare = peakBands.length ? `${formatDecimal(peakBands[0].share_pct, 1)}%` : "—";

  const cmp = overview.category_comparison || {};
  const overlap = cmp.overlap || {};
  const activeCats = (cmp.active_categories && cmp.active_categories.length)
    ? cmp.active_categories
    : (cmp.categories || []).filter((c) => c.has_data || c.observation_count > 0);
  const totalOverlapDenom = Math.max(1, overlap.total_unique || 1);
  const overlapSegments = (overlap.segments && overlap.segments.length)
    ? overlap.segments
    : activeCats.map((c) => ({
        key: c.key,
        label: `${c.short_name || c.name}独有`,
        full_label: `仅 ${c.name}`,
        count: c.exclusive_asin_count || 0,
        color: c.color,
      }));
  const maxActiveSalesMedian = Math.max(1, ...activeCats.map((c) => c.sales_median || 0));

  const cloud = overview.keyword_cloud || { keywords: [], top_keywords: [] };
  const topKeywords = cloud.top_keywords || [];
  const topThemeWords = topKeywords.slice(0, 3).map((kw) => kw.word);
  const maxKwCount = Math.max(1, ...topKeywords.map((item) => item.doc_count || 0));
  const activeKwLower = (tableState.q || "").trim().toLowerCase();

  // Interactive scatter category selector options
  const allScatterPoints = overview.price_analysis.scatter || [];
  const scatterOptions = activeCats.map((cat) => ({
    key: cat.key,
    label: cat.short_name || cat.name,
    fullName: cat.name,
    color: cat.color,
    softColor: cat.soft_color,
    count: allScatterPoints.filter((pt) => pt.category_group === cat.key).length,
  }));
  const crossScatterCount = allScatterPoints.filter((pt) => pt.category_group === "both").length;
  if (crossScatterCount > 0) {
    scatterOptions.push({
      key: "both",
      label: "跨类目重合",
      fullName: "跨类目重合样本",
      color: "#6D28D9",
      softColor: "#F5F3FF",
      count: crossScatterCount,
    });
  }
  const singleCategoryScoped = overview.category_filter !== "all";
  const selectedScatterSet = new Set(scatterSelectedCategories || []);
  const filteredScatterPoints = singleCategoryScoped
    ? allScatterPoints
    : allScatterPoints.filter((pt) => selectedScatterSet.has(pt.category_group));

  // Filter out Generic / Unknown / Unbranded placeholder brands (transparently noted)
  const rawBrands = overview.top_brands || [];
  const excludedGenericBrands = rawBrands.filter((b) => isGenericBrand(b.brand));
  const excludedGenericAsinCount = excludedGenericBrands.reduce((sum, b) => sum + (Number(b.asin_count) || 0), 0);
  const realBrands = rawBrands.filter((b) => !isGenericBrand(b.brand));
  const maxBrandCount = Math.max(1, ...realBrands.map((b) => b.asin_count || 0));

  // Build Active Filter Chips for Sample Table
  const activeChips = [];
  if (tableState.price_band && tableState.price_band !== "all") {
    activeChips.push({
      key: "price_band",
      label: `价格带：${PRICE_BAND_LABELS[tableState.price_band] || tableState.price_band}`,
    });
  }
  if (tableState.brand) {
    activeChips.push({ key: "brand", label: `品牌：${tableState.brand}` });
  }
  if (tableState.q) {
    activeChips.push({ key: "q", label: `关键词：${tableState.q}` });
  }
  if (tableState.anomaly && tableState.anomaly !== "all") {
    activeChips.push({ key: "anomaly", label: `质量状态：${tableState.anomaly}` });
  }
  const isTableOpen = Boolean(tableExpanded) || activeChips.length > 0;

  const scopeBadgeLabel = prepScope === "printable"
    ? "仅看印花池"
    : prepScope === "hide_excluded"
      ? "过滤已排除"
      : "全量原始";

  container.innerHTML = `
    <div class="dash-hm-root" data-hallmark-theme="sapphire-ledger">
    <!-- 1. Single-Row Unified Context Bar -->
    <div class="dash-top-strip">
      <div class="dash-top-left">
        <div class="prep-scope-bar" style="padding:0;border:0;box-shadow:none;background:transparent;" aria-label="预处理数据口径">
          <div class="segmented" role="group" aria-label="切换市场看板数据口径">
            <button type="button" class="seg-btn ${prepScope === "printable" ? "is-active" : ""}" aria-pressed="${prepScope === "printable"}" data-prep-scope="printable">仅看印花</button>
            <button type="button" class="seg-btn ${prepScope === "hide_excluded" ? "is-active" : ""}" aria-pressed="${prepScope === "hide_excluded"}" data-prep-scope="hide_excluded">过滤已排除</button>
            <button type="button" class="seg-btn ${prepScope === "all" ? "is-active" : ""}" aria-pressed="${prepScope === "all"}" data-prep-scope="all">全量原始</button>
          </div>
        </div>
      </div>

      <div class="dash-top-right">
        <span class="dash-meta-pill mono">快照 ${escapeHtml(k.latest_observed_date || "—")} · ${formatInt(k.category_count)}/${formatInt(k.total_target_categories || 6)} 类目</span>
        <button type="button" class="btn btn-ghost btn-sm" id="btn-goto-preprocessing">
          <span>02 预处理审核</span>
          ${ICON_ARROW_RIGHT}
        </button>
      </div>
    </div>

    <!-- 2. Hallmark Coral Charcoal Ink Board + Unified 2x2 Ruled Cotton Ledger -->
    <section class="dash-hero-grid" aria-label="大盘核心洞察与指标">
      <article class="dash-hero-pulse">
        <div class="dash-hero-top">
          <span class="dash-hero-tag">市场脉搏 · 核心结论</span>
          <span class="dash-hero-pill">${scopeBadgeLabel}</span>
        </div>

        <div class="dash-hero-main">
          <div class="dash-hero-number-row">
            <strong class="dash-hero-value">${formatInt(k.sales_median)}</strong>
            <span class="dash-hero-unit">近 30 天估算销量中位数（件 / Listing）</span>
          </div>
          <p class="dash-hero-insight">
            主力价格带集中在 <strong>${escapeHtml(peakBandText)}</strong>（占比 <strong>${peakBandShare}</strong>）${topThemeWords.length ? `，高频印花题材聚焦 <strong>${escapeHtml(topThemeWords.join("、"))}</strong>` : ""}；当前覆盖 <strong>${formatInt(k.category_count)}</strong> 个核心服饰类目、共 <strong>${formatInt(k.unique_asin_count)}</strong> 件去重商品。
          </p>
        </div>

        <div class="dash-hero-foot">
          <div class="dash-hero-stat">
            <span>核心区间 (P25–P75)</span>
            <strong>${formatInt(salesStats.p25)} – ${formatInt(salesStats.p75)}</strong>
          </div>
          <div class="dash-hero-stat">
            <span>爆款门槛 (P90)</span>
            <strong>${formatInt(salesStats.p90)} 件/月</strong>
          </div>
          <div class="dash-hero-stat">
            <span>跨类目重合去重</span>
            <strong>${formatInt(overlap.cross_category_asin_count ?? overlap.both ?? 0)} ASIN</strong>
          </div>
        </div>
      </article>

      <div class="dash-kpi-2x2 kpi-grid" aria-label="核心大盘 KPI">
        <article class="kpi-card">
          <div class="kpi-top"><span class="kpi-label">去重后样本数</span></div>
          <div class="kpi-value">${formatInt(k.unique_asin_count)}</div>
          <div class="kpi-sub">${formatInt(k.raw_asin_count)} ASIN · ${formatInt(k.observation_count)} 条观察</div>
        </article>

        <article class="kpi-card">
          <div class="kpi-top"><span class="kpi-label">有效售价中位数</span></div>
          <div class="kpi-value">${priceStats.median !== null && priceStats.median !== undefined ? `$${formatDecimal(priceStats.median, 2)}` : "—"}</div>
          <div class="kpi-sub">有效 ${formatInt(overview.price_analysis.valid_count)} 件 · 主力 ${escapeHtml(peakBandText)}</div>
        </article>

        <article class="kpi-card">
          <div class="kpi-top"><span class="kpi-label">有效评分中位数</span></div>
          <div class="kpi-value">${k.rating_median !== null && k.rating_median !== undefined ? `${formatDecimal(k.rating_median, 1)}★` : "—"}</div>
          <div class="kpi-sub">有效 ${formatInt(k.rating_valid_count)} 件 · 覆盖 ${formatDecimal(k.rating_valid_pct, 0)}%</div>
        </article>

        <article class="kpi-card">
          <div class="kpi-top"><span class="kpi-label">活跃品牌规模</span></div>
          <div class="kpi-value">${formatInt(k.distinct_brand_count)}</div>
          <div class="kpi-sub">覆盖 ${formatInt(k.category_count)} / ${formatInt(k.total_target_categories || 6)} 核心类目</div>
        </article>
      </div>
    </section>

    <!-- 3. Balanced 50/50 Visuals Row: Sales Spline + Price Band Donut -->
    <section class="grid-2" aria-label="销量分布与有效售价分布">
      <article class="panel panel-balanced">
        <div class="panel-header">
          <div>
            <h2 class="panel-title">近 30 天估算销量分布</h2>
            <p class="panel-subtitle">各销量区间商品分布 · 有效样本 ${formatInt(salesStats.count)} 件</p>
          </div>
        </div>
        <div class="chart-wrap" id="chart-sales-hist">
          ${renderSalesSplineAreaSvg(overview.sales_analysis.histogram)}
        </div>
        <div class="quantile-strip" aria-label="销量分位数">
          <div class="quantile-cell"><span class="quantile-label">最小</span><span class="quantile-val">${formatInt(salesStats.min)}</span></div>
          <div class="quantile-cell"><span class="quantile-label">P25</span><span class="quantile-val">${formatInt(salesStats.p25)}</span></div>
          <div class="quantile-cell is-median"><span class="quantile-label">中位数</span><span class="quantile-val">${formatInt(salesStats.median)}</span></div>
          <div class="quantile-cell"><span class="quantile-label">P75</span><span class="quantile-val">${formatInt(salesStats.p75)}</span></div>
          <div class="quantile-cell"><span class="quantile-label">P90</span><span class="quantile-val">${formatInt(salesStats.p90)}</span></div>
          <div class="quantile-cell"><span class="quantile-label">最大</span><span class="quantile-val">${formatInt(salesStats.max)}</span></div>
        </div>
      </article>

      <article class="panel panel-balanced">
        <div class="panel-header">
          <div>
            <h2 class="panel-title">有效售价分布</h2>
            <p class="panel-subtitle">点击任意价格带自动联动底部样本明细 · 有效 ${formatInt(overview.price_analysis.valid_count)} 件</p>
          </div>
          ${tableState.price_band !== "all" ? `<button type="button" class="btn btn-ghost btn-sm" id="btn-clear-price-band">清除价格筛选</button>` : ""}
        </div>
        <div id="chart-price-bands">
          ${renderPriceDonutHtml(overview.price_analysis.bands, priceStats, tableState.price_band)}
        </div>
      </article>
    </section>

    <!-- 4. Open-Air Six-Category Panorama Comparison (No Nested Card Double Border) -->
    <section class="dash-open-section panel-compare" aria-label="多类目全景对比">
      <div class="dash-section-head">
        <div>
          <h2>六类目全景对比（${activeCats.length} / ${overlap.total_target_categories || 6}）</h2>
          <p class="dash-section-sub">共 ${formatInt(overlap.total_observations || k.observation_count)} 条类目观察 → ${formatInt(overlap.total_unique || k.raw_asin_count)} 去重 ASIN（重合 ${formatInt(overlap.cross_category_asin_count ?? overlap.both ?? 0)} 件，未跨类目累加销量）</p>
        </div>
        ${overview.category_filter !== "all"
          ? `<button type="button" class="btn btn-secondary btn-sm" data-switch-cat="all">返回全部类目</button>`
          : ""
        }
      </div>

      <div class="dash-overlap-wrap">
        <div class="overlap-bar" role="img" aria-label="跨类目样本占比条">
          ${overlapSegments.map((seg) => {
            const pct = ((seg.count / totalOverlapDenom) * 100).toFixed(1);
            return `<div class="overlap-seg" style="width:${pct}%;background:${escapeHtml(seg.color)};" title="${escapeHtml(seg.full_label || seg.label)}：${seg.count} ASIN (${pct}%)"></div>`;
          }).join("")}
        </div>
        <div class="overlap-legend" aria-label="跨类目样本分布图例">
          ${overlapSegments.map((seg) => `
            <span class="overlap-legend-item">
              <span class="cat-dot" style="background:${escapeHtml(seg.color)}"></span>
              <span>${escapeHtml(seg.label)}</span>
              <strong>${formatInt(seg.count)}</strong>
            </span>
          `).join("")}
        </div>
      </div>

      <div class="cat-compare-grid ${activeCats.length >= 3 ? "is-multi" : ""}">
        ${activeCats.map((cat) => {
          const isSelected = overview.category_filter === cat.node_id;
          const sqrtPct = Math.max(
            8,
            Math.round(Math.sqrt(Math.max(0, cat.sales_median || 0) / Math.max(1, maxActiveSalesMedian)) * 100)
          );
          return `
            <div class="cat-compare-card ${isSelected ? "is-selected" : ""}" style="background:var(--bg-surface);border-color:${isSelected ? escapeHtml(cat.color) : "var(--border)"};" title="Node ID: ${escapeHtml(cat.node_id || "—")}">
              <div class="cat-compare-head">
                <div style="min-width:0;">
                  <div style="font-weight:700;font-size:14px;display:flex;align-items:center;gap:7px;flex-wrap:wrap;color:var(--ink-primary);">
                    <span class="cat-dot" style="background:${escapeHtml(cat.color)}"></span>
                    <span>${escapeHtml(cat.name)}</span>
                  </div>
                  <div style="font-size:12px;color:var(--ink-muted);margin-top:3px;">${escapeHtml(cat.garment_label || "")} · 样本 ${formatInt(cat.observation_count)}（独有 ${formatInt(cat.exclusive_asin_count)}）</div>
                </div>
                <button type="button" class="btn ${isSelected ? "btn-primary" : "btn-ghost"} btn-sm" data-switch-cat="${escapeHtml(isSelected ? "all" : cat.node_id)}">${isSelected ? "已选" : "筛选"}</button>
              </div>
              <div class="cat-metric-list">
                <div class="cat-metric-item"><span>销量中位</span><strong>${formatInt(cat.sales_median)}</strong></div>
                <div class="cat-metric-item"><span>P75 销量</span><strong>${formatInt(cat.sales_p75)}</strong></div>
                <div class="cat-metric-item"><span>有效售价</span><strong>${cat.price_median !== null ? `$${formatDecimal(cat.price_median, 2)}` : "—"}</strong></div>
                <div class="cat-metric-item"><span>评分中位</span><strong>${cat.rating_median !== null ? `${formatDecimal(cat.rating_median, 1)}★` : "—"}</strong></div>
              </div>
              <div class="cat-scale-bar" title="相对销量规模条（平方根尺度映射）">
                <div class="cat-scale-track">
                  <div class="cat-scale-fill" style="width:${sqrtPct}%;background:${escapeHtml(cat.color)};"></div>
                </div>
                <span class="mono" style="font-size:12px;color:var(--ink-muted);white-space:nowrap;">最高 ${formatInt(cat.sales_max)}</span>
              </div>
            </div>
          `;
        }).join("")}
      </div>
    </section>

    <!-- 5. Pre-Populated Interactive Price vs Sales Scatter Plot -->
    <section class="panel" aria-label="售价与销量散点分布">
      <div class="panel-header" style="margin-bottom:10px;">
        <div>
          <h2 class="panel-title">售价 × 近 30 天估算销量</h2>
          <p class="panel-subtitle">
            ${singleCategoryScoped
              ? `当前单类目共 ${formatInt(filteredScatterPoints.length)} 个有效点位`
              : `已展示 ${selectedScatterSet.size} 个类目 · ${formatInt(filteredScatterPoints.length)} 个点位（点击气泡查看商品详情）`
            }
          </p>
        </div>
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
          ${!singleCategoryScoped
            ? `<button type="button" class="btn btn-ghost btn-sm" id="btn-scatter-select-all">全选</button>
               <button type="button" class="btn btn-ghost btn-sm" id="btn-scatter-clear-all">清空</button>`
            : ""
          }
          <div class="segmented" role="group" aria-label="散点图坐标尺度切换">
            <button type="button" class="seg-btn ${scatterScaleMode !== "linear" ? "is-active" : ""}" data-scatter-scale="focused">对数聚焦</button>
            <button type="button" class="seg-btn ${scatterScaleMode === "linear" ? "is-active" : ""}" data-scatter-scale="linear">线性全量</button>
          </div>
        </div>
      </div>

      ${!singleCategoryScoped
        ? `<div class="scatter-cat-picker" id="scatter-cat-selector" role="group" aria-label="勾选要在散点图呈现的类目">
            ${scatterOptions.map((opt) => {
              const checked = selectedScatterSet.has(opt.key);
              return `
                <button type="button" class="scatter-cat-pill ${checked ? "is-checked" : ""}"
                  data-scatter-cat="${escapeHtml(opt.key)}" aria-pressed="${checked ? "true" : "false"}"
                  style="${checked ? `border-color:${escapeHtml(opt.color)};background:${escapeHtml(opt.softColor)};` : ""}">
                  <span class="cat-dot" style="background:${escapeHtml(opt.color)}"></span>
                  <span>${escapeHtml(opt.label)}</span>
                  <span class="mono" style="font-size:12px;color:var(--ink-muted);">(${opt.count})</span>
                </button>
              `;
            }).join("")}
          </div>`
        : ""
      }

      <div class="chart-wrap" id="chart-price-scatter">
        ${!singleCategoryScoped && selectedScatterSet.size === 0
          ? `<div style="padding:36px 20px;text-align:center;color:var(--ink-muted);font-size:13px;">
              已清空所有类目，点击上方任意类目胶囊或「全选」即可恢复散点分布。
            </div>`
          : renderPriceSalesScatterSvg(filteredScatterPoints, {
              scaleMode: scatterScaleMode,
              medianPrice: priceStats.median,
              medianSales: salesStats.median,
            })
        }
      </div>
    </section>

    <!-- 6. Luminous Keyword Cloud & Top 8 Ranking -->
    <section class="panel" aria-label="热门印花关键词词云">
      <div class="panel-header">
        <div>
          <h2 class="panel-title">热门印花关键词云</h2>
          <p class="panel-subtitle">已清洗版型/尺码/面料套话，点击任意词条直达匹配商品明细（Top ${cloud.keywords.length}）</p>
        </div>
        <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
          <div class="legend-row">
            <span class="legend-item"><span class="cat-dot" style="background:#E11D48"></span>节日/季节</span>
            <span class="legend-item"><span class="cat-dot" style="background:#0F766E"></span>图案元素</span>
            <span class="legend-item"><span class="cat-dot" style="background:#2552D9"></span>风格/工艺</span>
            <span class="legend-item"><span class="cat-dot" style="background:#475569"></span>情绪/场景</span>
          </div>
          ${tableState.q ? `<button type="button" class="btn btn-ghost btn-sm" id="btn-clear-keyword">清除词筛选：${escapeHtml(tableState.q)}</button>` : ""}
          <button type="button" class="btn btn-ghost btn-sm" id="btn-toggle-wc-motion">${wordCloudPaused ? "恢复浮动" : "暂停浮动"}</button>
        </div>
      </div>

      <div class="wordcloud-layout">
        <div class="wordcloud-stage" id="chart-keyword-cloud">
          ${renderKeywordCloudSvg(cloud.keywords, tableState.q)}
        </div>

        <div class="kw-rank-list" aria-label="高频印花主题词排行">
          ${topKeywords.slice(0, 8).map((kw, idx) => {
            const barColor = KW_BAR_COLORS[kw.theme] || "#254EDB";
            const pct = Math.max(10, Math.round((kw.doc_count / maxKwCount) * 100));
            const isActive = activeKwLower && activeKwLower === kw.query.toLowerCase();
            return `
              <button type="button" class="kw-rank-item ${isActive ? "is-active" : ""}" data-keyword="${escapeHtml(kw.query)}"
                title="点击筛选包含 ${escapeHtml(kw.word)} 的商品">
                <div class="kw-rank-top">
                  <span>
                    <span class="kw-rank-num">${String(idx + 1).padStart(2, "0")}</span>
                    <strong style="color:var(--ink-primary);">${escapeHtml(kw.word)}</strong>
                  </span>
                  <span class="num" style="font-size:12px;color:var(--ink-muted);">
                    <strong style="color:var(--ink-primary);">${kw.doc_count}</strong> 件 (${kw.share_pct}%) · 中位 ${formatInt(kw.median_sales_30d)}
                  </span>
                </div>
                <div class="donut-row-bar">
                  <div class="donut-row-fill" style="width:${pct}%;background:${barColor};"></div>
                </div>
              </button>
            `;
          }).join("")}
        </div>
      </div>
    </section>

    <!-- 7. Open-Air Gallery: Hot-Selling Sample Top 8 -->
    <section class="dash-open-section" aria-label="热销样本展示">
      <div class="dash-section-head">
        <div>
          <h2>热销样本 Top 8</h2>
          <p class="dash-section-sub">按当前去重口径近 30 天估算销量排序，点击任意卡片查看多图与父子同款证据</p>
        </div>
      </div>
      <div class="hot-grid">
        ${overview.hot_samples.map((p, idx) => `
          <article class="product-card" tabindex="0" role="button" data-open-asin="${escapeHtml(p.asin)}" aria-label="查看热销样本 ${escapeHtml(p.asin)}">
            <div class="product-card-media">
              ${renderSafeImage(p.main_image_url, p.title || p.asin, "", true)}
              <span class="rank-ribbon">#${idx + 1}</span>
            </div>
            <div class="product-card-body">
              <div class="tag-row">${renderCategoryBadges(p)}</div>
              <h3 class="product-card-title" title="${escapeHtml(p.title)}">${escapeHtml(p.title || "（无标题）")}</h3>
              <div style="font-size:12px;color:var(--ink-muted);display:flex;justify-content:space-between;gap:6px;">
                <span class="mono">${escapeHtml(p.asin)}</span>
                <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(p.brand || "未知")}</span>
              </div>
              <div class="dash-hot-metrics">
                <span class="dash-hot-sales" title="近 30 天估算销量">${formatInt(p.listing_sales_30d)}/月</span>
                <span class="dash-hot-price">${formatPrice(p.sales_price_usd, p.price_valid)}</span>
                <span class="dash-hot-rating">${formatRatingHtml(p.rating_stars, p.ratings_count, p.rating_status)}</span>
              </div>
              ${((p.duplicate_cluster_size || 1) > 1 || p.needs_parent_verification)
                ? `<div class="tag-row">
                    ${(p.duplicate_cluster_size || 1) > 1 ? `<span class="badge badge-info" title="${escapeHtml(p.duplicate_match_reason || "")}">合并 ${p.duplicate_cluster_size} 同款</span>` : ""}
                    ${p.needs_parent_verification ? `<span class="badge badge-warn">父子同号</span>` : ""}
                  </div>`
                : ""
              }
            </div>
          </article>
        `).join("")}
      </div>
    </section>

    <!-- 8. Open-Air Real Brand Leaderboard (Generic Filtered) -->
    <section class="dash-open-section" aria-label="头部品牌分布">
      <div class="dash-section-head">
        <div>
          <h2>头部真实品牌 Top ${realBrands.length}</h2>
          <p class="dash-section-sub">
            共 ${formatInt(k.distinct_brand_count)} 个品牌 · 点击品牌卡片直达对应样本明细${excludedGenericAsinCount > 0 ? `（已自动过滤 Generic 等无品牌占位 ${formatInt(excludedGenericAsinCount)} 件）` : ""}
          </p>
        </div>
        ${tableState.brand
          ? `<button type="button" class="btn btn-secondary btn-sm" id="btn-clear-brand">清除品牌筛选：${escapeHtml(tableState.brand)}</button>`
          : ""
        }
      </div>
      <div class="brand-showcase-grid">
        ${realBrands.map((b, idx) => {
          const pctWidth = Math.max(12, Math.round((b.asin_count / maxBrandCount) * 100));
          const isActive = tableState.brand && tableState.brand.toLowerCase() === b.brand.toLowerCase();
          return `
            <button type="button" class="brand-card ${isActive ? "is-active" : ""}" data-brand-filter="${escapeHtml(b.brand)}" title="点击筛选品牌 ${escapeHtml(b.brand)} 的商品">
              <div class="brand-card-top">
                ${renderBrandEmblem(b)}
                <div style="min-width:0;flex:1;">
                  <div style="display:flex;align-items:center;gap:6px;">
                    <span class="mono" style="font-size:12px;color:var(--ink-muted);">#${idx + 1}</span>
                    <div class="brand-card-name">${escapeHtml(b.brand)}</div>
                  </div>
                  <div style="display:flex;align-items:center;gap:6px;margin-top:2px;flex-wrap:wrap;">
                    <span style="font-size:12px;color:var(--ink-muted);">占比 ${b.share_pct}%</span>
                    ${b.has_brand_store ? `<span class="badge badge-info">旗舰店</span>` : ""}
                  </div>
                </div>
              </div>
              <div class="brand-card-stats">
                <div class="cat-metric-item">
                  <span>入选样本</span>
                  <strong>${b.asin_count} 件</strong>
                </div>
                <div class="cat-metric-item">
                  <span>中位销量</span>
                  <strong>${formatInt(b.median_sales_30d)}</strong>
                </div>
              </div>
              <div class="brand-bar-track">
                <div class="brand-bar-fill" style="width:${pctWidth}%;background:${escapeHtml(b.brand_color || "var(--accent)")};"></div>
              </div>
            </button>
          `;
        }).join("")}
      </div>
    </section>

    <!-- 9. Collapsible Full Product Sample Table (Collapsed by Default + Active Filter Chips) -->
    <section class="panel" id="product-table-section" aria-label="商品样本明细">
      <div class="panel-header" style="${isTableOpen ? "" : "margin-bottom:0;"}">
        <div>
          <h2 class="panel-title">商品样本明细（<span id="table-count-badge">${productsPage ? formatInt(productsPage.total) : "—"} 件</span>）</h2>
          <p class="panel-subtitle">${isTableOpen ? "支持按关键词、价格带、质量状态与字段组合检索；点击任意行打开商品详情抽屉。" : "默认收起以保持看板清爽；点击右侧展开，或点击上方价格带 / 词云 / 品牌自动定位。"}</p>
        </div>
        <button type="button" class="btn btn-secondary btn-sm" id="btn-toggle-product-table" aria-expanded="${isTableOpen ? "true" : "false"}" aria-controls="dash-table-collapse-body">
          <span>${isTableOpen ? "收起样本明细" : "展开样本明细"}</span>
          ${ICON_CHEVRON}
        </button>
      </div>

      ${activeChips.length > 0
        ? `<div class="dash-active-chips" style="margin-bottom:12px;" aria-label="当前生效的联动筛选">
            ${activeChips.map((chip) => `
              <button type="button" class="dash-filter-chip" data-remove-chip="${escapeHtml(chip.key)}">
                <span>${escapeHtml(chip.label)}</span>
                <span aria-hidden="true">×</span>
              </button>
            `).join("")}
            <button type="button" class="btn btn-ghost btn-sm" id="btn-clear-all-chips">全部清除</button>
          </div>`
        : ""
      }

      <div class="dash-table-collapse-body" id="dash-table-collapse-body" ${isTableOpen ? "" : "hidden"}>
        <div class="table-toolbar">
          <div class="table-filters">
            <input type="search" id="table-search" class="input-control search-input"
              placeholder="搜索 ASIN / 标题 / 关键词 / 品牌…" value="${escapeHtml(tableState.q)}" aria-label="搜索商品">

            <select id="table-anomaly-filter" class="select-control" aria-label="质量状态筛选">
              <option value="all" ${tableState.anomaly === "all" ? "selected" : ""}>全部状态</option>
              <option value="duplicate_listing_cluster" ${tableState.anomaly === "duplicate_listing_cluster" ? "selected" : ""}>同款重复簇</option>
              <option value="cross_category" ${tableState.anomaly === "cross_category" ? "selected" : ""}>跨类目重合</option>
              <option value="same_parent_asin" ${tableState.anomaly === "same_parent_asin" ? "selected" : ""}>父子同号</option>
              <option value="invalid_price" ${tableState.anomaly === "invalid_price" ? "selected" : ""}>$0 / 无效售价</option>
              <option value="missing_rating" ${tableState.anomaly === "missing_rating" ? "selected" : ""}>缺失评分</option>
              <option value="missing_source_bsr" ${tableState.anomaly === "missing_source_bsr" ? "selected" : ""}>缺失类目 BSR</option>
              <option value="clean" ${tableState.anomaly === "clean" ? "selected" : ""}>字段完备</option>
            </select>

            <select id="table-price-filter" class="select-control" aria-label="价格区间筛选">
              <option value="all" ${tableState.price_band === "all" ? "selected" : ""}>全部价格带</option>
              <option value="under_15" ${tableState.price_band === "under_15" ? "selected" : ""}>&lt; $15</option>
              <option value="15_20" ${tableState.price_band === "15_20" ? "selected" : ""}>$15 - $19.99</option>
              <option value="20_25" ${tableState.price_band === "20_25" ? "selected" : ""}>$20 - $24.99</option>
              <option value="25_35" ${tableState.price_band === "25_35" ? "selected" : ""}>$25 - $34.99</option>
              <option value="35_plus" ${tableState.price_band === "35_plus" ? "selected" : ""}>$35+</option>
              <option value="invalid" ${tableState.price_band === "invalid" ? "selected" : ""}>仅异常价</option>
            </select>

            <select id="table-sort-field" class="select-control" aria-label="排序字段">
              <option value="listing_sales_30d" ${tableState.sort === "listing_sales_30d" ? "selected" : ""}>30 天估算销量</option>
              <option value="sales_price_usd" ${tableState.sort === "sales_price_usd" ? "selected" : ""}>有效售价</option>
              <option value="rating_stars" ${tableState.sort === "rating_stars" ? "selected" : ""}>有效星级</option>
              <option value="ratings_count" ${tableState.sort === "ratings_count" ? "selected" : ""}>评分数</option>
              <option value="main_category_bsr" ${tableState.sort === "main_category_bsr" ? "selected" : ""}>大类 BSR</option>
              <option value="source_category_bsr" ${tableState.sort === "source_category_bsr" ? "selected" : ""} ${overview.can_sort_source_bsr ? "" : "disabled"}>源类目 BSR${overview.can_sort_source_bsr ? "" : "（选单类目可用）"}</option>
              <option value="online_date" ${tableState.sort === "online_date" ? "selected" : ""}>上架日期</option>
            </select>

            <button type="button" class="btn btn-secondary" id="table-sort-order" aria-label="切换升降序">
              ${tableState.order === "asc" ? "升序 ↑" : "降序 ↓"}
            </button>

            <button type="button" class="btn btn-ghost" id="table-reset-filters">重置</button>
          </div>
        </div>

        ${productsPage && productsPage.sort_warning ? `<div class="alert alert-warn" style="margin-bottom:10px;">${escapeHtml(productsPage.sort_warning)}</div>` : ""}

        <div id="product-table-body-wrap">
          ${renderProductTableHtml(productsPage)}
        </div>
      </div>
    </section>
    </div>
  `;

  bindChartInteractions(container, {
    onSelectPriceBand: (bandKey) => handlers.onTogglePriceBand(bandKey),
    onOpenAsin: (asin) => handlers.onOpenAsin(asin),
    onSelectKeyword: (kw) => handlers.onSelectKeyword(kw),
  });

  startWordCloudPhysics(container, { paused: wordCloudPaused });

  container.querySelectorAll("[data-prep-scope]").forEach((btn) => {
    btn.addEventListener("click", () => {
      handlers.onChangePrepScope?.(btn.getAttribute("data-prep-scope"));
    });
  });

  document.getElementById("btn-goto-preprocessing")?.addEventListener("click", () => {
    handlers.onJumpToPreprocessing?.();
  });

  document.getElementById("btn-toggle-product-table")?.addEventListener("click", () => {
    handlers.onToggleDashboardTable?.();
  });

  container.querySelectorAll("[data-remove-chip]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const key = btn.getAttribute("data-remove-chip");
      if (key === "price_band") handlers.onTogglePriceBand("all");
      else if (key === "brand") handlers.onSelectBrand("");
      else if (key === "q") handlers.onSelectKeyword("");
      else if (key === "anomaly") handlers.onSelectAnomaly("all");
    });
  });

  document.getElementById("btn-clear-all-chips")?.addEventListener("click", () => {
    handlers.onResetTableFilters();
  });

  container.querySelectorAll("[data-scatter-cat]").forEach((btn) => {
    btn.addEventListener("click", () => {
      handlers.onToggleScatterCategory?.(btn.getAttribute("data-scatter-cat"));
    });
  });

  document.getElementById("btn-scatter-select-all")?.addEventListener("click", () => {
    handlers.onSetScatterCategories?.(scatterOptions.map((o) => o.key));
  });

  document.getElementById("btn-scatter-clear-all")?.addEventListener("click", () => {
    handlers.onSetScatterCategories?.([]);
  });

  document.getElementById("btn-toggle-wc-motion")?.addEventListener("click", () => {
    handlers.onToggleWordCloudMotion?.();
  });

  container.querySelectorAll("[data-open-asin]").forEach((el) => {
    if (el.tagName.toLowerCase() === "circle") return;
    const trigger = () => handlers.onOpenAsin(el.getAttribute("data-open-asin"));
    el.addEventListener("click", trigger);
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        trigger();
      }
    });
  });

  container.querySelectorAll("[data-switch-cat]").forEach((btn) => {
    btn.addEventListener("click", () => handlers.onSelectCategory(btn.getAttribute("data-switch-cat")));
  });

  container.querySelectorAll("[data-scatter-scale]").forEach((btn) => {
    btn.addEventListener("click", () => {
      handlers.onToggleScatterScale?.(btn.getAttribute("data-scatter-scale"));
    });
  });

  container.querySelectorAll("[data-brand-filter]").forEach((btn) => {
    btn.addEventListener("click", () => {
      handlers.onSelectBrand(btn.getAttribute("data-brand-filter"));
    });
  });

  document.getElementById("btn-clear-price-band")?.addEventListener("click", () => handlers.onTogglePriceBand("all"));
  document.getElementById("btn-clear-brand")?.addEventListener("click", () => handlers.onSelectBrand(""));
  document.getElementById("btn-clear-keyword")?.addEventListener("click", () => handlers.onSelectKeyword(""));

  let searchTimer = null;
  document.getElementById("table-search")?.addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    const val = e.target.value;
    searchTimer = setTimeout(() => handlers.onTableFilterChange({ q: val, page: 1 }), 200);
  });
  document.getElementById("table-anomaly-filter")?.addEventListener("change", (e) => {
    handlers.onTableFilterChange({ anomaly: e.target.value, page: 1 });
  });
  document.getElementById("table-price-filter")?.addEventListener("change", (e) => {
    handlers.onTableFilterChange({ price_band: e.target.value, page: 1 });
  });
  document.getElementById("table-sort-field")?.addEventListener("change", (e) => {
    const field = e.target.value;
    const defaultOrder = ["main_category_bsr", "source_category_bsr", "result_position"].includes(field) ? "asc" : "desc";
    handlers.onTableFilterChange({ sort: field, order: defaultOrder, page: 1 });
  });
  document.getElementById("table-sort-order")?.addEventListener("click", () => {
    handlers.onTableFilterChange({ order: tableState.order === "desc" ? "asc" : "desc", page: 1 });
  });
  document.getElementById("table-reset-filters")?.addEventListener("click", () => {
    handlers.onResetTableFilters();
  });

  bindTableRowEvents(container, handlers);
}

export function renderProductTableHtml(productsPage) {
  if (!productsPage) {
    return `<div class="loading-state"><div class="spinner"></div><p>加载商品明细…</p></div>`;
  }
  if (!productsPage.items || productsPage.items.length === 0) {
    return `<div class="empty-state"><strong>无匹配商品</strong><span>请尝试调整或重置筛选条件。</span></div>`;
  }

  const rowsHtml = productsPage.items.map((p) => `
    <tr tabindex="0" data-row-asin="${escapeHtml(p.asin)}" aria-label="查看商品 ${escapeHtml(p.asin)} 详情">
      <td>
        <div class="cell-product">
          <div class="thumb-box">${renderSafeImage(p.main_image_url, p.asin)}</div>
          <div style="min-width:0;">
            <div class="cell-title" title="${escapeHtml(p.title)}">${escapeHtml(p.title || "（无标题）")}</div>
            <div class="cell-sub">
              <span class="mono">${escapeHtml(p.asin)}</span>
              <span>·</span>
              <span>${escapeHtml(p.brand || "未知")}</span>
            </div>
          </div>
        </div>
      </td>
      <td><div class="tag-row">${renderCategoryBadges(p)}</div></td>
      <td class="num-cell"><strong style="color:var(--accent-text)">${formatInt(p.listing_sales_30d)}</strong></td>
      <td class="num-cell">${formatPrice(p.sales_price_usd, p.price_valid)}</td>
      <td class="num-cell">${formatRatingHtml(p.rating_stars, p.ratings_count, p.rating_status)}</td>
      <td class="num-cell">${p.main_category_bsr !== null ? `#${formatInt(p.main_category_bsr)}` : "—"}</td>
      <td class="mono" style="font-size:12px;color:var(--ink-muted);white-space:nowrap;">${escapeHtml(p.online_date || "—")}</td>
      <td><div class="tag-row">${renderAnomalyBadges(p)}</div></td>
      <td><button type="button" class="btn btn-ghost btn-sm" data-row-btn="${escapeHtml(p.asin)}">详情</button></td>
    </tr>
  `).join("");

  return `
    <div class="table-scroll" style="border:none;border-radius:0;border-top:1px solid var(--border);">
      <table class="data-table">
        <thead>
          <tr>
            <th>商品 / ASIN</th>
            <th>来源类目与 BSR</th>
            <th class="num-cell">30天销量</th>
            <th class="num-cell">售价</th>
            <th class="num-cell">评分</th>
            <th class="num-cell">大类 BSR</th>
            <th>上架日期</th>
            <th>状态标记</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>${rowsHtml}</tbody>
      </table>
    </div>
    <div class="table-footer">
      <div>共 <strong>${formatInt(productsPage.total)}</strong> 件 · 第 <strong>${productsPage.page}</strong> / ${Math.max(1, productsPage.total_pages)} 页</div>
      <div class="pagination">
        <button type="button" class="btn btn-secondary btn-sm" id="btn-page-prev" ${productsPage.page <= 1 ? "disabled" : ""}>上一页</button>
        <button type="button" class="btn btn-secondary btn-sm" id="btn-page-next" ${productsPage.page >= productsPage.total_pages ? "disabled" : ""}>下一页</button>
      </div>
    </div>
  `;
}

export function bindTableRowEvents(rootEl, handlers) {
  rootEl.querySelectorAll("[data-row-asin]").forEach((tr) => {
    const asin = tr.getAttribute("data-row-asin");
    tr.addEventListener("click", (e) => {
      if (e.target.closest("a")) return;
      handlers.onOpenAsin(asin);
    });
    tr.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        handlers.onOpenAsin(asin);
      }
    });
  });

  document.getElementById("btn-page-prev")?.addEventListener("click", () => handlers.onChangePage(-1));
  document.getElementById("btn-page-next")?.addEventListener("click", () => handlers.onChangePage(1));
}
