/**
 * Stage 04: Candidate Product Scoring View (V2) — Streamlined Strategy Deck & Paginated Leaderboard.
 */

import { bindChartInteractions } from "../charts.js";
import {
  escapeHtml,
  formatDecimal,
  formatInt,
  formatPrice,
  formatRatingHtml,
  renderCredibilityAnomalyBadges,
  renderSafeImage,
  renderSourceCategoryBadge,
} from "../format.js";

const DIM_ORDER = ["sales", "opportunity", "rating", "freshness", "price"];

const ICON_SLIDERS = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/></svg>`;
const ICON_ARROW_RIGHT = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14"/><path d="m12 5 7 7-7 7"/></svg>`;

function renderRankDelta(delta) {
  if (delta === null || delta === undefined || delta === 0) {
    return `<span class="rank-delta is-flat" title="排名无变化">—</span>`;
  }
  if (delta > 0) {
    return `<span class="rank-delta is-up" title="排名上升 ${delta} 位">↑${delta}</span>`;
  }
  return `<span class="rank-delta is-down" title="排名下降 ${Math.abs(delta)} 位">↓${Math.abs(delta)}</span>`;
}

function renderConfidenceBadge(item) {
  const level = item.confidence_level || "medium";
  const label = item.confidence_label || "中";
  const score = item.confidence_score ?? 0;
  const penalties = item.confidence_penalties || [];
  const tip = penalties.length
    ? `数据可信度 ${score}/100<br>` + penalties.map((p) => `${p.label}（${p.delta}）`).join("<br>")
    : `数据可信度 ${score}/100<br>核心字段完整，无扣分项`;
  return `
    <span class="conf-badge is-${escapeHtml(level)}" data-tip="${escapeHtml(tip)}" tabindex="0">
      <span>${escapeHtml(label)}</span>
      <span class="mono" style="font-size:12px;">${score}</span>
    </span>
  `;
}

function renderContributionBar(item) {
  const dims = item.dimensions || {};
  const ordered = DIM_ORDER.map((k) => dims[k]).filter(Boolean);
  const missingList = ordered.filter((d) => d.missing);

  const tipLines = ordered.map((d) => {
    const isBsrOls = d.key === "sales" && (item.demand_metric_source === "bsr_ols_estimated" || d.demand_metric_source === "bsr_ols_estimated");
    const isBsrPct = d.key === "sales" && (item.demand_metric_source === "bsr_reverse_percentile" || d.demand_metric_source === "bsr_reverse_percentile");
    const dimLabel = isBsrOls ? "销量规模（BSR 回归折算）" : isBsrPct ? "需求规模（超越同类占比）" : d.label;
    if (d.missing) {
      return `<span style="color:#CBD5E1;">● ${escapeHtml(dimLabel)}：缺失（按保守先验 ${d.prior_score ?? 45} 分计入，+${Number(d.contribution || 0).toFixed(1)}）</span>`;
    }
    return `<span style="color:${escapeHtml(d.color)};">●</span> <strong>${escapeHtml(dimLabel)}</strong>：分值 ${d.score.toFixed(1)} × 权重 ${d.effective_weight.toFixed(1)}% = <strong>+${d.contribution.toFixed(1)}</strong><br><span style="color:#CBD5E1;font-size:12px;padding-left:10px;">${escapeHtml(d.detail || "")}</span>`;
  });
  const penaltyMult = Number(item.low_rating_multiplier ?? 1);
  const penaltyLine = item.low_rating_penalty
    ? `<br><span style="color:#FCA5A5;"><strong>差评降权 ×${penaltyMult}</strong>（${Number(item.rating_stars || 0).toFixed(1)}★ · ${formatInt(item.ratings_count || 0)} 评）：加权原分 ${Number(item.pre_penalty_score ?? item.total_score).toFixed(1)} → <strong>${item.total_score.toFixed(1)}</strong></span>`
    : "";
  const tipHtml = `<strong>综合潜力分：${item.total_score.toFixed(1)} / 100</strong>${
    missingList.length ? ` <span style="color:#FBBF24;">（缺 ${missingList.length} 维按保守先验计入）</span>` : ""
  }${penaltyLine}<br>${tipLines.join("<br>")}`;

  const segmentsHtml = ordered
    .filter((d) => d.contribution > 0)
    .map(
      (d) => `
        <span class="contrib-seg" style="width:${Math.max(1.5, d.contribution).toFixed(2)}%;background:${d.missing ? "#CBD5E1" : escapeHtml(d.color)};"></span>
      `
    )
    .join("");

  return `
    <div class="contrib-wrap" data-tip="${escapeHtml(tipHtml)}" tabindex="0">
      <div class="contrib-track" role="img" aria-label="五维得分贡献构成条，悬停或聚焦查看明细">
        ${segmentsHtml || `<span style="font-size:12px;color:var(--ink-muted);padding-left:6px;">权重全为 0</span>`}
      </div>
      ${missingList.length
        ? `<div style="margin-top:4px;"><span class="badge badge-warn">缺 ${missingList.length} 维</span></div>`
        : ""
      }
    </div>
  `;
}

function openSavePresetModal(weights, priceRanges, onSave) {
  const overlay = document.getElementById("review-modal-overlay");
  const body = document.getElementById("review-modal-body");
  const titleEl = document.getElementById("review-modal-title");
  const eyebrowEl = document.getElementById("review-modal-eyebrow");
  if (!overlay || !body) return;

  if (eyebrowEl) eyebrowEl.textContent = "评分策略保存";
  titleEl.textContent = "保存自定义评分方案";
  body.innerHTML = `
    <div class="field">
      <label class="field-label" for="preset-name-input">方案名称</label>
      <input type="text" id="preset-name-input" class="input-control" maxlength="48"
        placeholder="例如：新品高动销优先 / 卫衣高客单策略">
    </div>
    <div style="padding:10px 12px;border-radius:var(--radius-sm);background:var(--bg-elevated);font-size:12.5px;color:var(--ink-secondary);display:flex;flex-direction:column;gap:4px;">
      <div>权重：销量 ${weights.sales} · 机会 ${weights.opportunity} · 星级 ${weights.rating} · 新鲜度 ${weights.freshness} · 价格 ${weights.price}</div>
      <div>目标价格：T-Shirt $${priceRanges.t_shirt.min}–$${priceRanges.t_shirt.max} · Sweatshirt $${priceRanges.sweatshirt.min}–$${priceRanges.sweatshirt.max}</div>
    </div>
    <div id="preset-modal-err"></div>
    <div style="display:flex;justify-content:flex-end;gap:8px;">
      <button type="button" class="btn btn-secondary" id="preset-cancel-btn">取消</button>
      <button type="button" class="btn btn-primary" id="preset-confirm-save-btn">保存到本地数据库</button>
    </div>
  `;

  overlay.classList.add("is-open");
  overlay.setAttribute("aria-hidden", "false");
  document.getElementById("preset-name-input")?.focus();

  const close = () => {
    overlay.classList.remove("is-open");
    overlay.setAttribute("aria-hidden", "true");
  };
  document.getElementById("preset-cancel-btn")?.addEventListener("click", close);
  document.getElementById("preset-confirm-save-btn")?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    const name = (document.getElementById("preset-name-input")?.value || "").trim();
    const errBox = document.getElementById("preset-modal-err");
    if (!name) {
      if (errBox) errBox.innerHTML = `<div class="alert alert-danger">请输入方案名称。</div>`;
      return;
    }
    btn.disabled = true;
    try {
      await onSave({ name, weights, price_ranges: priceRanges });
      close();
    } catch (err) {
      if (errBox) errBox.innerHTML = `<div class="alert alert-danger">${escapeHtml(err.message)}</div>`;
    } finally {
      btn.disabled = false;
    }
  });
}

export function renderScoringTableHtml(
  data,
  { compareMode = "default", unrankedExpanded = false, incompleteExpanded = false, page = 1, pageSize = 50 } = {}
) {
  if (!data) {
    return `<div class="loading-state"><div class="spinner"></div><p>正在计算候选评分…</p></div>`;
  }
  const allItems = data.items || [];
  const totalPages = Math.max(1, Math.ceil(allItems.length / pageSize));
  const safePage = Math.max(1, Math.min(totalPages, Number(page) || 1));
  const startIdx = (safePage - 1) * pageSize;
  const pageItems = allItems.slice(startIdx, startIdx + pageSize);

  const unranked = data.unranked_no_sales || [];
  const incomplete = data.unranked_incomplete || [];

  const mainTableHtml = allItems.length === 0
    ? `<div class="empty-state"><strong>当前筛选条件下无参与排名的候选商品</strong><span>请放宽类目、可信度筛选，或先在 02 数据预处理确认保留样本。</span></div>`
    : `
      <div class="table-scroll" style="border:none;border-radius:0;border-top:1px solid var(--border);">
        <table class="data-table scoring-table" style="min-width:1060px;">
          <thead>
            <tr>
              <th style="width:68px;text-align:center;">排名</th>
              <th>商品 / ASIN</th>
              <th>来源类目</th>
              <th class="num-cell">综合潜力分</th>
              <th style="text-align:center;">数据可信度</th>
              <th class="num-cell">30天销量 / 售价</th>
              <th class="num-cell">星级 / 上架</th>
              <th>五维贡献条（悬停看明细）</th>
            </tr>
          </thead>
          <tbody>
            ${pageItems.map((item, idx) => {
              const rankCls = item.rank === 1 ? "is-top1" : item.rank === 2 ? "is-top2" : item.rank === 3 ? "is-top3" : "";
              const delta = compareMode === "previous" ? item.rank_delta_vs_previous : item.rank_delta_vs_default;
              const scoreCls = item.total_score >= 70 ? "is-high" : item.total_score >= 50 ? "is-mid" : "is-low";
              const salesDim = item.dimensions?.sales || {};
              return `
                <tr data-score-asin="${escapeHtml(item.asin)}" tabindex="0">
                  <td>
                    <div class="rank-cell">
                      <span class="rank-num ${rankCls}">#${item.rank}</span>
                      ${renderRankDelta(delta)}
                    </div>
                  </td>
                  <td>
                    <div class="cell-product">
                      <div class="thumb-box">${renderSafeImage(item.image_url, item.asin, "", idx < 10)}</div>
                      <div style="min-width:0;">
                        <div class="cell-title" title="${escapeHtml(item.title)}">${escapeHtml(item.title || "（无标题）")}</div>
                        <div class="cell-sub">
                          <span class="mono">${escapeHtml(item.asin)}</span>
                          <span>·</span>
                          <span>${escapeHtml(item.brand || "未知")}</span>
                          ${item.collapsed_count > 0
                            ? `<span class="badge badge-both" title="已自动折叠同品牌同图/同款变体：${escapeHtml((item.collapsed_sibling_asins || []).join(', '))}">合并 ${item.collapsed_count + 1} 同款</span>`
                            : ""
                          }
                          ${renderCredibilityAnomalyBadges(item)}
                        </div>
                      </div>
                    </div>
                  </td>
                  <td>
                    <div class="tag-row" style="max-width:170px;">
                      ${(item.source_categories || []).map((s) => renderSourceCategoryBadge(s, { showBsr: false })).join(" ")}
                    </div>
                  </td>
                  <td class="num-cell" style="white-space:nowrap;">
                    <span class="score-pill ${scoreCls}">${formatDecimal(item.total_score, 1)}</span>
                    ${item.low_rating_penalty
                      ? `<div style="margin-top:4px;"><span class="badge ${item.low_rating_penalty === "severe" ? "badge-danger" : "badge-warn"}" title="低星级差评降权（≥10评）：${Number(item.rating_stars || 0).toFixed(1)}★ (${formatInt(item.ratings_count || 0)} 评)，原分 ${formatDecimal(item.pre_penalty_score ?? item.total_score, 1)} × ${item.low_rating_multiplier}">差评 ×${item.low_rating_multiplier}</span></div>`
                      : ""
                    }
                  </td>
                  <td style="text-align:center;">
                    ${renderConfidenceBadge(item)}
                  </td>
                  <td class="num-cell" style="white-space:nowrap;">
                    ${item.listing_sales_30d !== null && item.listing_sales_30d !== undefined
                      ? `<div style="font-weight:700;color:var(--accent-text);">${formatInt(item.listing_sales_30d)}</div>`
                      : item.demand_metric_source === "bsr_ols_estimated"
                        ? `<div style="font-weight:700;color:#0F766E;" title="${escapeHtml(salesDim.detail || "BSR 经同类目 OLS 保守折算")}">${escapeHtml(salesDim.raw_display || (item.best_source_category_bsr ? `BSR #${formatInt(item.best_source_category_bsr)}` : "BSR 折算"))}</div>`
                        : item.demand_metric_source === "bsr_reverse_percentile" && item.best_source_category_bsr
                          ? `<div style="font-weight:700;color:#0F766E;" title="缺少 30 天估算销量，按同类目 BSR 反向百分位参与评分">BSR #${formatInt(item.best_source_category_bsr)}</div>`
                          : `<div style="font-weight:700;color:var(--ink-muted);">—</div>`
                    }
                    <div style="font-size:12px;color:var(--ink-muted);margin-top:2px;">${formatPrice(item.price, item.price_valid)}</div>
                  </td>
                  <td class="num-cell" style="white-space:nowrap;">
                    <div>${formatRatingHtml(item.rating_stars, item.ratings_count, item.rating_stars !== null ? "valid" : "missing")}</div>
                    <div style="font-size:12px;color:var(--ink-muted);margin-top:2px;">${item.days_online !== null && item.days_online !== undefined ? `上架 ${formatInt(item.days_online)} 天` : "上架日期缺失"}</div>
                  </td>
                  <td>
                    ${renderContributionBar(item)}
                  </td>
                </tr>
              `;
            }).join("")}
          </tbody>
        </table>
      </div>
      <div class="table-footer">
        <div>显示第 <strong>${formatInt(startIdx + 1)}–${formatInt(Math.min(startIdx + pageSize, allItems.length))}</strong> 名，共 <strong>${formatInt(allItems.length)}</strong> 件 · 第 <strong>${safePage}</strong> / ${totalPages} 页</div>
        <div class="pagination">
          <button type="button" class="btn btn-secondary btn-sm" id="btn-scoring-page-prev" ${safePage <= 1 ? "disabled" : ""}>上一页</button>
          <button type="button" class="btn btn-secondary btn-sm" id="btn-scoring-page-next" ${safePage >= totalPages ? "disabled" : ""}>下一页</button>
        </div>
      </div>
    `;

  const incompleteHtml = incomplete.length > 0
    ? `
      <div style="margin-top:14px;padding:12px 14px;border-radius:var(--radius-md);background:var(--bg-elevated);" id="scoring-incomplete-section">
        <div class="unranked-fold-head" id="btn-toggle-incomplete" role="button" tabindex="0">
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
            <span class="badge badge-warn">残缺品保护</span>
            <strong style="font-size:13px;">${formatInt(incomplete.length)} 件商品因缺主图或缺失 ≥ 3 个维度已移出主排行榜</strong>
          </div>
          <span class="btn btn-ghost btn-sm">${incompleteExpanded ? "收起" : "展开查看"}</span>
        </div>
        ${incompleteExpanded
          ? `<div class="table-scroll" style="margin-top:10px;border:none;border-radius:0;border-top:1px solid var(--border);background:var(--bg-surface);">
              <table class="data-table" style="min-width:760px;">
                <thead>
                  <tr><th>商品 / ASIN</th><th>来源类目</th><th style="text-align:center;">过滤标签</th><th class="num-cell">销量 / BSR / 售价</th><th style="text-align:center;">可信度</th><th>折叠原因</th></tr>
                </thead>
                <tbody>
                  ${incomplete.map((u) => `
                    <tr data-unranked-asin="${escapeHtml(u.asin)}">
                      <td>
                        <div class="cell-product">
                          <div class="thumb-box">${renderSafeImage(u.image_url, u.asin)}</div>
                          <div style="min-width:0;">
                            <div class="cell-title">${escapeHtml(u.title || "（无标题）")}</div>
                            <div class="cell-sub"><span class="mono">${escapeHtml(u.asin)}</span> · ${escapeHtml(u.brand || "未知")} ${renderCredibilityAnomalyBadges(u)}</div>
                          </div>
                        </div>
                      </td>
                      <td><div class="tag-row">${(u.source_categories || []).map((s) => renderSourceCategoryBadge(s, { showBsr: false })).join(" ")}</div></td>
                      <td style="text-align:center;white-space:nowrap;">
                        <span class="badge badge-warn">${escapeHtml(u.incomplete_tag || "残缺保护")}</span>
                      </td>
                      <td class="num-cell" style="white-space:nowrap;">
                        <div>${u.listing_sales_30d !== null && u.listing_sales_30d !== undefined ? `${formatInt(u.listing_sales_30d)} 件/月` : (u.best_source_category_bsr ? `BSR #${formatInt(u.best_source_category_bsr)}` : "—")}</div>
                        <div style="font-size:12px;color:var(--ink-muted);">${formatPrice(u.price, u.price_valid)}</div>
                      </td>
                      <td style="text-align:center;">${renderConfidenceBadge(u)}</td>
                      <td style="font-size:12px;color:var(--ink-muted);">${escapeHtml(u.reason || "")}</td>
                    </tr>
                  `).join("")}
                </tbody>
              </table>
            </div>`
          : ""
        }
      </div>
    `
    : "";

  const unrankedHtml = unranked.length > 0
    ? `
      <div style="margin-top:12px;padding:12px 14px;border-radius:var(--radius-md);background:var(--bg-elevated);" id="scoring-unranked-section">
        <div class="unranked-fold-head" id="btn-toggle-unranked" role="button" tabindex="0">
          <div style="display:flex;align-items:center;gap:8px;">
            <span class="badge badge-neutral">无销量依据</span>
            <strong style="font-size:13px;">${formatInt(unranked.length)} 件商品近 30 天销量与类目 BSR 均缺失（不参与主榜排名）</strong>
          </div>
          <span class="btn btn-ghost btn-sm">${unrankedExpanded ? "收起" : "展开查看"}</span>
        </div>
        ${unrankedExpanded
          ? `<div class="table-scroll" style="margin-top:10px;border:none;border-radius:0;border-top:1px solid var(--border);background:var(--bg-surface);">
              <table class="data-table" style="min-width:700px;">
                <thead>
                  <tr><th>商品 / ASIN</th><th>来源类目</th><th class="num-cell">售价</th><th style="text-align:center;">可信度</th><th>折叠原因</th></tr>
                </thead>
                <tbody>
                  ${unranked.map((u) => `
                    <tr data-unranked-asin="${escapeHtml(u.asin)}">
                      <td>
                        <div class="cell-product">
                          <div class="thumb-box">${renderSafeImage(u.image_url, u.asin)}</div>
                          <div style="min-width:0;">
                            <div class="cell-title">${escapeHtml(u.title || "（无标题）")}</div>
                            <div class="cell-sub"><span class="mono">${escapeHtml(u.asin)}</span> · ${escapeHtml(u.brand || "未知")} ${renderCredibilityAnomalyBadges(u)}</div>
                          </div>
                        </div>
                      </td>
                      <td><div class="tag-row">${(u.source_categories || []).map((s) => renderSourceCategoryBadge(s, { showBsr: false })).join(" ")}</div></td>
                      <td class="num-cell">${formatPrice(u.price, u.price !== null)}</td>
                      <td style="text-align:center;">${renderConfidenceBadge(u)}</td>
                      <td style="font-size:12px;color:var(--ink-muted);">${escapeHtml(u.reason)}</td>
                    </tr>
                  `).join("")}
                </tbody>
              </table>
            </div>`
          : ""
        }
      </div>
    `
    : "";

  return mainTableHtml + incompleteHtml + unrankedHtml;
}

export function renderScoringView(
  container,
  {
    data,
    presets = [],
    activePresetId = "system_default",
    weights,
    priceRanges,
    filters,
    tuningOpen = false,
    page = 1,
    pageSize = 50,
    loading,
    toast,
    unrankedExpanded = false,
    incompleteExpanded = false,
    handlers,
  }
) {
  if (!data) {
    container.innerHTML = `<div class="loading-state"><div class="spinner"></div><p>正在加载 04 商品候选评分引擎…</p></div>`;
    return;
  }

  if (!data.ready) {
    const st = data.db_status || {};
    container.innerHTML = `
      <div class="empty-state">
        <span class="badge badge-warn">${escapeHtml(st.state || "empty")}</span>
        <h2 style="margin:6px 0 0;font-size:17px;color:var(--ink-primary);">暂无可评分的预处理商品池</h2>
        <p style="max-width:520px;margin:0;font-size:13px;">${escapeHtml(st.message || "请先确认已有快照数据并在 02 数据预处理生成 Ready 候选集。")}</p>
        <button type="button" class="btn btn-primary" id="btn-score-goto-prep">前往 02 数据预处理</button>
      </div>
    `;
    document.getElementById("btn-score-goto-prep")?.addEventListener("click", () => handlers.onJumpToPreprocessing());
    return;
  }

  const s = data.summary || {};
  const conf = s.confidence_counts || { high: 0, medium: 0, low: 0 };
  const dimsMeta = data.dimensions_meta || [];
  const weightSum = Object.values(weights || {}).reduce((acc, v) => acc + Number(v || 0), 0);
  const categories = data.categories || [];
  const highPct = s.scored_count ? Math.round((conf.high / s.scored_count) * 100) : 0;
  const foldedTotal = (s.unranked_no_sales_count || 0) + (s.unranked_incomplete_count || 0);

  const toastHtml = toast
    ? `<div class="prep-toast ${toast.type === "error" ? "alert-danger" : "alert-ok"}" id="scoring-toast" role="status" aria-live="polite">
        <span>${escapeHtml(toast.message)}</span>
        <button type="button" class="btn btn-ghost btn-sm" id="btn-scoring-toast-dismiss">关闭</button>
      </div>`
    : "";

  container.innerHTML = `
    ${toastHtml}

    <!-- 1. Compact 3-Card Executive KPI Strip -->
    <section class="col-kpi-grid" aria-label="候选评分核心概览">
      <article class="kpi-card">
        <div class="kpi-top">
          <span class="kpi-label">参与评分候选</span>
        </div>
        <div class="kpi-value">${formatInt(s.scored_count)} <span style="font-size:15px;font-weight:500;color:var(--ink-muted);">件</span></div>
        <div class="kpi-sub">跨六类目统一竞争 · 残缺/无销折叠 ${formatInt(foldedTotal)} 件</div>
      </article>

      <article class="kpi-card">
        <div class="kpi-top">
          <span class="kpi-label">综合潜力中位分</span>
          <span class="badge badge-info">V2 引擎</span>
        </div>
        <div class="kpi-value" style="color:var(--accent-text);">${s.median_score !== null && s.median_score !== undefined ? formatDecimal(s.median_score, 1) : "—"}</div>
        <div class="kpi-sub">均分 ${s.avg_score !== null && s.avg_score !== undefined ? formatDecimal(s.avg_score, 1) : "—"} · 快照 ${escapeHtml(data.selected_date || "—")}</div>
      </article>

      <article class="kpi-card">
        <div class="kpi-top">
          <span class="kpi-label">高可信度样本</span>
        </div>
        <div class="kpi-value">${formatInt(conf.high)} <span style="font-size:15px;font-weight:500;color:var(--ink-muted);">(${highPct}%)</span></div>
        <div class="kpi-sub">中 ${formatInt(conf.medium)} / 低 ${formatInt(conf.low)} · 价格异常 ${formatInt(s.price_anomaly_count || 0)} / 销量存疑 ${formatInt(s.sales_suspect_count || 0)}</div>
      </article>
    </section>

    <!-- 2. Strategy Preset Bar + Collapsible Custom Weight & Price Tuning Drawer -->
    <section class="panel scoring-controls-panel" aria-label="评分策略与动态权重配置">
      <div class="scoring-preset-bar">
        <div class="scoring-preset-list" id="scoring-preset-list">
          ${presets.map((p) => `
            <span class="preset-chip ${activePresetId === p.preset_id ? "is-active" : ""}" data-load-preset="${escapeHtml(p.preset_id)}" role="button" tabindex="0">
              <span>${escapeHtml(p.name)}</span>
              ${!p.is_default
                ? `<button type="button" class="preset-chip-del" data-del-preset="${escapeHtml(p.preset_id)}" aria-label="删除方案 ${escapeHtml(p.name)}" title="删除方案">×</button>`
                : ""
              }
            </span>
          `).join("")}
        </div>
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
          <span class="scoring-sync-pill" title="权重与价格配置调整后自动同步至 05 印花归组与 06 Top 10">已同步至 05/06</span>
          <button type="button" class="btn btn-secondary btn-sm" id="btn-scoring-toggle-tuning" aria-expanded="${tuningOpen ? "true" : "false"}" aria-controls="scoring-tuning-drawer">
            ${ICON_SLIDERS}
            <span>${tuningOpen ? "收起权重与价格" : "自定义权重与价格"}</span>
          </button>
          <button type="button" class="btn btn-ghost btn-sm" id="btn-scoring-reset">恢复默认</button>
          <button type="button" class="btn btn-primary btn-sm" id="btn-scoring-save-preset">保存方案</button>
        </div>
      </div>

      <div class="scoring-tuning-drawer" id="scoring-tuning-drawer" ${tuningOpen ? "" : "hidden"}>
        <div class="scoring-sliders-grid" id="scoring-sliders-grid">
          ${dimsMeta.map((d) => {
            const val = Number(weights[d.key] ?? d.default_weight);
            const share = weightSum > 0 ? ((val / weightSum) * 100).toFixed(1) : "0.0";
            return `
              <div class="slider-item" title="${escapeHtml(d.description)}">
                <div class="slider-card-head">
                  <span class="slider-label">
                    <span class="cat-dot" style="background:${escapeHtml(d.color)};"></span>
                    <span>${escapeHtml(d.label)}</span>
                  </span>
                  <span class="slider-val" id="slider-val-${escapeHtml(d.key)}">${val}</span>
                </div>
                <input type="range" class="score-range-input" min="0" max="100" step="1"
                  value="${val}" data-weight-slider="${escapeHtml(d.key)}"
                  style="--slider-color:${escapeHtml(d.color)};--slider-pct:${val}%;"
                  aria-label="${escapeHtml(d.label)}权重">
                <div class="slider-share">
                  <span>默认 ${d.default_weight}</span>
                  <strong id="slider-share-${escapeHtml(d.key)}" style="color:${escapeHtml(d.color)};">占比 ${share}%</strong>
                </div>
              </div>
            `;
          }).join("")}
        </div>

        <div class="price-range-strip" aria-label="品类目标价格区间">
          <div class="price-range-group">
            <strong>T-Shirt 目标价 ($)</strong>
            <input type="number" class="price-num-input" id="price-tshirt-min" min="1" max="499" step="1" value="${priceRanges.t_shirt.min}">
            <span>–</span>
            <input type="number" class="price-num-input" id="price-tshirt-max" min="2" max="500" step="1" value="${priceRanges.t_shirt.max}">
          </div>
          <div class="price-range-group">
            <strong>Sweatshirt 目标价 ($)</strong>
            <input type="number" class="price-num-input" id="price-sweat-min" min="1" max="499" step="1" value="${priceRanges.sweatshirt.min}">
            <span>–</span>
            <input type="number" class="price-num-input" id="price-sweat-max" min="2" max="500" step="1" value="${priceRanges.sweatshirt.max}">
          </div>
          <button type="button" class="btn btn-secondary btn-sm" id="btn-apply-price-ranges">应用价格区间</button>
        </div>
      </div>
    </section>

    <!-- 3. Paginated Candidate Ranking Leaderboard (Physically Separated Filters) -->
    <section class="panel" aria-label="候选商品综合潜力排名">
      <div class="panel-header">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
          <h2 class="panel-title">候选商品潜力排行榜</h2>
          <span class="badge badge-neutral" id="scoring-count-badge">${formatInt((data.items || []).length)} 件</span>
          ${loading ? `<span class="badge badge-info">实时重算中…</span>` : ""}
        </div>
        <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
          <div class="contrib-mini-legend">
            ${dimsMeta.map((d) => `
              <span class="contrib-dot-item">
                <span class="contrib-dot" style="background:${escapeHtml(d.color)};"></span>
                <span>${escapeHtml(d.label)}</span>
              </span>
            `).join("")}
          </div>
          <button type="button" class="btn btn-secondary btn-sm" id="btn-scoring-goto-directions">
            <span>前往 05 印花归组</span>
            ${ICON_ARROW_RIGHT}
          </button>
        </div>
      </div>

      <div class="scoring-filter-bar" aria-label="排行榜视图筛选">
        <div class="scoring-filter-left">
          <input type="search" id="scoring-search" class="input-control" style="width:210px;height:32px;font-size:12.5px;"
            placeholder="搜索 ASIN / 标题 / 品牌…" value="${escapeHtml(filters.q || "")}">

          <select id="scoring-category-filter" class="select-control" style="height:32px;font-size:12.5px;" aria-label="类目筛选">
            <option value="all" ${filters.category === "all" ? "selected" : ""}>全部六类目</option>
            ${categories.filter((c) => c.is_configured).map((c) => `
              <option value="${escapeHtml(c.node_id)}" ${filters.category === c.node_id ? "selected" : ""}>${escapeHtml(c.name)}</option>
            `).join("")}
          </select>

          <select id="scoring-confidence-filter" class="select-control" style="height:32px;font-size:12.5px;" aria-label="可信度筛选">
            <option value="all" ${filters.confidence === "all" ? "selected" : ""}>全部可信度</option>
            <option value="high" ${filters.confidence === "high" ? "selected" : ""}>高可信度 (≥80)</option>
            <option value="medium" ${filters.confidence === "medium" ? "selected" : ""}>中可信度 (60–79)</option>
            <option value="low" ${filters.confidence === "low" ? "selected" : ""}>低可信度 (&lt;60)</option>
          </select>

          <button type="button" class="btn btn-ghost btn-sm" id="btn-scoring-clear-filters">清空筛选</button>
        </div>

        <div class="scoring-filter-right">
          <select id="scoring-page-size" class="select-control" style="height:30px;font-size:12px;" aria-label="每页显示条数">
            <option value="50" ${pageSize === 50 ? "selected" : ""}>每页 50 条</option>
            <option value="100" ${pageSize === 100 ? "selected" : ""}>每页 100 条</option>
          </select>
          <div class="segmented" role="group" aria-label="排名变化对比基准">
            <button type="button" class="seg-btn ${filters.compareMode !== "previous" ? "is-active" : ""}" data-compare-mode="default">比默认权重</button>
            <button type="button" class="seg-btn ${filters.compareMode === "previous" ? "is-active" : ""}" data-compare-mode="previous">比上次调整</button>
          </div>
        </div>
      </div>

      <div id="scoring-table-wrap">
        ${renderScoringTableHtml(data, {
          compareMode: filters.compareMode,
          unrankedExpanded,
          incompleteExpanded,
          page,
          pageSize,
        })}
      </div>
    </section>
  `;

  // Bind preset chips
  container.querySelectorAll("[data-load-preset]").forEach((chip) => {
    const trigger = (e) => {
      if (e.target.closest("[data-del-preset]")) return;
      handlers.onLoadPreset(chip.getAttribute("data-load-preset"));
    };
    chip.addEventListener("click", trigger);
    chip.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        trigger(e);
      }
    });
  });
  container.querySelectorAll("[data-del-preset]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      handlers.onDeletePreset(btn.getAttribute("data-del-preset"));
    });
  });

  document.getElementById("btn-scoring-toggle-tuning")?.addEventListener("click", () => handlers.onToggleTuningDrawer?.());
  document.getElementById("btn-scoring-goto-directions")?.addEventListener("click", () => handlers.onJumpToDirections?.());
  document.getElementById("btn-scoring-clear-filters")?.addEventListener("click", () => handlers.onClearScoringFilters?.());
  document.getElementById("btn-scoring-reset")?.addEventListener("click", () => handlers.onResetDefaults());
  document.getElementById("btn-scoring-save-preset")?.addEventListener("click", () => {
    const liveWeights = {};
    container.querySelectorAll("[data-weight-slider]").forEach((el) => {
      liveWeights[el.getAttribute("data-weight-slider")] = Number(el.value || 0);
    });
    const livePriceRanges = {
      t_shirt: {
        min: Number(document.getElementById("price-tshirt-min")?.value || priceRanges.t_shirt.min),
        max: Number(document.getElementById("price-tshirt-max")?.value || priceRanges.t_shirt.max),
      },
      sweatshirt: {
        min: Number(document.getElementById("price-sweat-min")?.value || priceRanges.sweatshirt.min),
        max: Number(document.getElementById("price-sweat-max")?.value || priceRanges.sweatshirt.max),
      },
    };
    openSavePresetModal(
      Object.keys(liveWeights).length ? liveWeights : weights,
      livePriceRanges,
      (payload) => handlers.onSavePreset(payload)
    );
  });
  document.getElementById("btn-scoring-toast-dismiss")?.addEventListener("click", () => handlers.onDismissToast());

  // Live draggable sliders with instant visual feedback + debounced server re-evaluation
  let sliderTimer = null;
  container.querySelectorAll("[data-weight-slider]").forEach((input) => {
    input.addEventListener("input", () => {
      const nextWeights = {};
      container.querySelectorAll("[data-weight-slider]").forEach((el) => {
        const v = Number(el.value || 0);
        el.style.setProperty("--slider-pct", `${v}%`);
        nextWeights[el.getAttribute("data-weight-slider")] = v;
      });
      const sum = Object.values(nextWeights).reduce((a, b) => a + b, 0);
      Object.entries(nextWeights).forEach(([k, v]) => {
        const valEl = document.getElementById(`slider-val-${k}`);
        const shareEl = document.getElementById(`slider-share-${k}`);
        if (valEl) valEl.textContent = String(v);
        if (shareEl) shareEl.textContent = `占比 ${sum > 0 ? ((v / sum) * 100).toFixed(1) : "0.0"}%`;
      });
      clearTimeout(sliderTimer);
      sliderTimer = setTimeout(() => handlers.onWeightsChange(nextWeights), 160);
    });
  });

  // Price range apply
  document.getElementById("btn-apply-price-ranges")?.addEventListener("click", () => {
    const nextRanges = {
      t_shirt: {
        min: Number(document.getElementById("price-tshirt-min")?.value || 15),
        max: Number(document.getElementById("price-tshirt-max")?.value || 26),
      },
      sweatshirt: {
        min: Number(document.getElementById("price-sweat-min")?.value || 26),
        max: Number(document.getElementById("price-sweat-max")?.value || 45),
      },
    };
    handlers.onPriceRangesChange(nextRanges);
  });

  // View filters
  let searchTimer = null;
  document.getElementById("scoring-search")?.addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    const val = e.target.value;
    searchTimer = setTimeout(() => handlers.onFilterChange({ q: val }), 200);
  });
  document.getElementById("scoring-category-filter")?.addEventListener("change", (e) => {
    handlers.onFilterChange({ category: e.target.value });
  });
  document.getElementById("scoring-confidence-filter")?.addEventListener("change", (e) => {
    handlers.onFilterChange({ confidence: e.target.value });
  });
  document.getElementById("scoring-page-size")?.addEventListener("change", (e) => {
    handlers.onChangeScoringPageSize?.(Number(e.target.value));
  });
  container.querySelectorAll("[data-compare-mode]").forEach((btn) => {
    btn.addEventListener("click", () => handlers.onCompareModeChange(btn.getAttribute("data-compare-mode")));
  });

  bindScoringTableEvents(document.getElementById("scoring-table-wrap"), handlers);
}

export function bindScoringTableEvents(root, handlers) {
  if (!root) return;
  bindChartInteractions(root, {});

  root.querySelectorAll("tr[data-score-asin], tr[data-unranked-asin]").forEach((tr) => {
    const asin = tr.getAttribute("data-score-asin") || tr.getAttribute("data-unranked-asin");
    const open = (e) => {
      if (e.target.closest("a, button")) return;
      handlers.onOpenAsin(asin);
    };
    tr.addEventListener("click", open);
    tr.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        handlers.onOpenAsin(asin);
      }
    });
  });

  const toggleInc = document.getElementById("btn-toggle-incomplete");
  if (toggleInc) {
    toggleInc.addEventListener("click", () => handlers.onToggleIncomplete?.());
    toggleInc.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        handlers.onToggleIncomplete?.();
      }
    });
  }

  const toggleUnr = document.getElementById("btn-toggle-unranked");
  if (toggleUnr) {
    toggleUnr.addEventListener("click", () => handlers.onToggleUnranked?.());
    toggleUnr.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        handlers.onToggleUnranked?.();
      }
    });
  }

  document.getElementById("btn-scoring-page-prev")?.addEventListener("click", () => handlers.onChangeScoringPage?.(-1));
  document.getElementById("btn-scoring-page-next")?.addEventListener("click", () => handlers.onChangeScoringPage?.(1));
}
