/**
 * Stage 06: Top 10 Print Directions Output & Standardized Handoff View.
 *
 * Strictly reflects real shortlisted directions persisted in SQLite (Stage 05):
 * - Quality-over-quantity: displays exact shortlisted count without padding fake directions.
 * - Unified priority-ranked output (#1 -> #N) without forced empty Evergreen/Seasonal buckets.
 * - Top bar supports Edit Mode vs Presentation Mode (hides admin buttons for demos & print).
 * - One-screen Executive Overview Directory for instant anchor jumping when >= 2 directions.
 * - Open-air decision cards with Lightbox zoom, Amazon links, commercial evidence strip,
 *   unified Design Brief (variants + style tags + inline handoff notes + 1-click Copy Brief),
 *   fixed bottom floating Toast with 5s Undo on remove, and JSON / CSV export.
 */

import { bindChartInteractions } from "../charts.js";
import {
  escapeHtml,
  formatDecimal,
  formatInt,
  formatPrice,
  renderCredibilityAnomalyBadges,
  renderDirectionAnomalyBadge,
  renderSafeImage,
  renderSourceCategoryBadge,
} from "../format.js";
import {
  openImageLightbox,
  renderDirectionSourceBadge,
  renderDirectionTypeBadge,
} from "./directions.js";

function formatPriceBand(priceRange) {
  if (!priceRange || priceRange.min === null || priceRange.min === undefined) return "—";
  const minVal = Math.round(Number(priceRange.min));
  const maxVal = Math.round(Number(priceRange.max ?? priceRange.min));
  if (!Number.isFinite(minVal)) return "—";
  if (!Number.isFinite(maxVal) || minVal === maxVal) return `$${minVal}`;
  return `$${minVal}–$${maxVal}`;
}

function renderScoreTooltip(dir) {
  const m = dir.metrics || {};
  const b = m.score_breakdown || {};
  const feasSign = (b.feasibility_delta || 0) >= 0 ? "+" : "";
  const hasRealTrim = m.excluded_anomaly_count > 0
    && Math.abs(Number(m.raw_peak_product_score || 0) - Number(b.base_score || 0)) >= 0.05;
  const anomalyNote = m.all_anomalous
    ? `<br>• <span style="color:#FCA5A5;">⚠ 全员异常兜底：回退取原始最高分，请人工核实</span>`
    : (hasRealTrim
        ? `<br>• 已去极值：剔除 ${m.excluded_anomaly_count} 个异常项（原始峰值 ${formatDecimal(m.raw_peak_product_score, 1)} → ${formatDecimal(b.base_score || 0, 1)}）`
        : "");
  return escapeHtml(
    `<strong>方向综合推荐分：${formatDecimal(dir.direction_score, 1)}</strong><br>`
    + `• 头部验证基础分（04 峰值）：<strong>${formatDecimal(b.base_score || 0, 1)}</strong><br>`
    + `• 独立父体广度加分（${m.parent_asin_count || 0} 父体）：<strong>+${formatDecimal(b.breadth_bonus || 0, 1)}</strong><br>`
    + `• 跨类目加分（${b.cross_category_reason || "—"}）：<strong>+${formatDecimal(b.cross_category_bonus || 0, 1)}</strong><br>`
    + `• 二创可行性微调（${dir.feasibility_rating}★）：<strong>${feasSign}${formatDecimal(b.feasibility_delta || 0, 1)}</strong>`
    + anomalyNote
  );
}

function renderStarsReadonly(rating) {
  const r = Number(rating || 3);
  return `<span class="top10-stars" title="人工二创可行性：${r} / 5 星" aria-label="可行性 ${r} 星">${"★".repeat(r)}<span class="top10-stars-empty">${"★".repeat(Math.max(0, 5 - r))}</span></span>`;
}

function renderConfidencePill(conf) {
  if (!conf) return `<span class="badge badge-neutral">无样本</span>`;
  const lvl = conf.overall_level || "medium";
  const tip = `支撑商品可信度分布：高 ${conf.high_count || 0} / 中 ${conf.medium_count || 0} / 低 ${conf.low_count || 0}${conf.avg_confidence !== null && conf.avg_confidence !== undefined ? `（均值 ${conf.avg_confidence}）` : ""}`;
  return `
    <span class="conf-badge is-${escapeHtml(lvl)}" data-tip="${escapeHtml(tip)}" tabindex="0">
      <span>${escapeHtml(conf.overall_label || "中可信")}</span>
      ${conf.avg_confidence !== null && conf.avg_confidence !== undefined ? `<span class="mono" style="font-size:12px;">${Math.round(conf.avg_confidence)}</span>` : ""}
    </span>
  `;
}

function renderSeasonCountdownBadge(dir) {
  if (!dir || !dir.is_seasonal) return "";
  const label = dir.season_label || dir.season_tag || "季节";
  const days = dir.days_until_holiday;
  if (days !== null && days !== undefined) {
    const isUrgent = days >= 0 && days <= 14;
    const isPast = days < 0;
    const baseText = days >= 0
      ? `距 ${label} 还剩 ${formatInt(days)} 天`
      : `${label} 已过 ${formatInt(Math.abs(days))} 天`;
    const warnSuffix = isUrgent
      ? `<span class="badge badge-holiday-urgent" title="POD 测款与物流通常需 14 天以上提前量，建议评估是否作为次年储备款">窗口临近 · 建议次年储备</span>`
      : (isPast
          ? `<span class="badge badge-holiday-urgent" title="当前节日节点已过，建议归档为次年提前上新储备">建议次年储备</span>`
          : "");
    return `
      <span class="badge badge-holiday-countdown" data-season-badge="${escapeHtml(dir.season_tag)}" data-holiday-countdown="${days}" title="目标节日日期：${escapeHtml(dir.holiday_date || "—")}（供 POD 上新节奏参考）">🗓 ${escapeHtml(baseText)}</span>
      ${warnSuffix}
    `;
  }
  return `<span class="badge badge-holiday-countdown" data-season-badge="${escapeHtml(dir.season_tag)}" title="季节方向：${escapeHtml(label)}">🗓 ${escapeHtml(label)}</span>`;
}

function buildDesignBriefText(dir) {
  const m = dir.metrics || {};
  const cats = (m.covered_categories || []).map((c) => c.short_name || c.name).filter(Boolean);
  const variants = Array.isArray(dir.variant_suggestions) ? dir.variant_suggestions : [];
  const styleTags = Array.isArray(dir.style_tags) ? dir.style_tags : [];
  const refs = (dir.supporting_products || []).slice(0, 4);
  const priceStr = formatPriceBand(m.price_range);
  const salesStr = m.max_sales_30d !== null && m.max_sales_30d !== undefined
    ? `最高 ${formatInt(m.max_sales_30d)}/月 · 中位 ${formatInt(m.median_sales_30d)}/月`
    : "—";

  const lines = [
    `【印花设计 Brief #${dir.rank}】${dir.name}`,
    `综合推荐分：${formatDecimal(dir.direction_score, 1)} ｜ 二创可行性：${dir.feasibility_rating || 3}/5★`,
  ];
  if (dir.target_audience) lines.push(`目标受众：${dir.target_audience}`);
  if (dir.is_seasonal) {
    const label = dir.season_label || dir.season_tag || "季节";
    const days = dir.days_until_holiday;
    const timing = days !== null && days !== undefined
      ? (days >= 0 ? `距 ${label} 剩 ${days} 天${days <= 14 ? "（窗口临近，建议评估次年储备）" : ""}` : `${label} 已过 ${Math.abs(days)} 天（建议次年储备）`)
      : label;
    lines.push(`节日窗口：${timing}`);
  }
  if (styleTags.length) lines.push(`风格标签：${styleTags.join(" / ")}`);
  lines.push(`商业实证：${formatInt(m.parent_asin_count || 0)} 独立父体（${formatInt(m.asin_count || 0)} ASIN · ${formatInt(m.brand_count || 0)} 品牌）｜ 30天销量 ${salesStr} ｜ 售价带 ${priceStr}${cats.length ? ` ｜ 覆盖类目：${cats.join("、")}` : ""}`);

  if (variants.length) {
    lines.push("", "【可测试 POD 二创变体建议】");
    variants.forEach((v, i) => lines.push(`${i + 1}. ${v}`));
  }
  if (dir.notes) {
    lines.push("", "【设计师交接备注】", dir.notes.trim());
  }
  if (refs.length) {
    lines.push("", "【参考爆款样本】");
    refs.forEach((p, i) => {
      const url = p.amazon_url || `https://www.amazon.com/dp/${p.asin}`;
      lines.push(`${i + 1}. ${p.asin}（${formatInt(p.listing_sales_30d)}/月 · ${formatPrice(p.sales_price_usd, p.price_valid)}）${url}`);
    });
  }
  return lines.join("\n");
}

function renderDesignBriefSection(dir, { isExpanded, isEditingNote, presentationMode }) {
  const variants = Array.isArray(dir.variant_suggestions) ? dir.variant_suggestions : [];
  const supporting = dir.supporting_products || [];
  const styleTags = Array.isArray(dir.style_tags) ? dir.style_tags : [];

  const variantsHtml = variants.length > 0
    ? `<div class="top10-brief-sub" data-variants-box="${dir.id}">
        <div class="top10-brief-sub-head">
          <div class="top10-brief-label-row">
            <span class="top10-brief-sub-title">✦ POD 可测试二创变体建议</span>
            <span class="badge badge-info top10-pill-12">${variants.length} 条</span>
          </div>
          ${presentationMode
            ? ""
            : `<button type="button" class="btn btn-ghost btn-sm top10-edit-only" data-generate-variants="${dir.id}" title="使用 AI 重新生成 3~5 条二创变体建议（先弹窗预检确认）">
                ↻ 重新生成
              </button>`
          }
        </div>
        <ol class="top10-variants-list">
          ${variants.map((v) => `<li>${escapeHtml(v)}</li>`).join("")}
        </ol>
      </div>`
    : (presentationMode
        ? ""
        : `<div class="top10-brief-sub top10-brief-empty-row top10-edit-only" data-variants-box="${dir.id}">
            <span class="top10-brief-hint">
              <strong>POD 二创变体建议：</strong>暂未生成（可按需生成 3~5 条构图 / 配色 / 文案快速测款方案）
            </span>
            <button type="button" class="btn btn-ai-accent btn-sm" data-generate-variants="${dir.id}">
              ✦ 生成变体建议
            </button>
          </div>`
      );

  const noteAreaHtml = isEditingNote && !presentationMode
    ? `<div class="top10-note-editor">
        <textarea class="textarea-control" id="top10-note-input-${dir.id}" rows="3"
          placeholder="填写供设计团队直接执行的二创要点、构图约束、文案风格与避坑提醒…">${escapeHtml(dir.notes || "")}</textarea>
        <div class="top10-note-editor-actions">
          <button type="button" class="btn btn-secondary btn-sm" data-note-cancel="${dir.id}">取消</button>
          <button type="button" class="btn btn-primary btn-sm" data-note-save="${dir.id}">保存设计交接备注</button>
        </div>
      </div>`
    : (presentationMode
        ? (dir.notes
            ? `<div class="top10-note-display is-readonly">
                <span>${escapeHtml(dir.notes)}</span>
              </div>`
            : "")
        : `<div class="top10-note-display" data-note-edit="${dir.id}" role="button" tabindex="0" title="点击或按回车编辑设计交接备注">
            <span class="${dir.notes ? "" : "top10-note-placeholder"}">
              ${dir.notes ? escapeHtml(dir.notes) : "点击补充供下游设计团队阅读的二创交接备注（构图、文案、风格参考）…"}
            </span>
            <span class="badge badge-neutral top10-note-edit-pill">编辑备注</span>
          </div>`
      );

  return `
    <div class="top10-brief-section">
      <div class="top10-brief-head">
        <div class="tag-row">
          <span class="top10-brief-title">设计执行单</span>
          ${styleTags.map((t) => `<span class="badge badge-info top10-pill-12">${escapeHtml(t)}</span>`).join("")}
        </div>
        <div class="top10-brief-actions">
          <button type="button" class="btn btn-secondary btn-sm" data-copy-brief="${dir.id}" title="一键复制完整设计 Brief（含实证、变体建议、备注与参考链接）">
            📋 复制 Brief
          </button>
          <button type="button" class="btn btn-ghost btn-sm" data-top10-expand="${dir.id}">
            ${isExpanded ? `收起全部 ${supporting.length} 个支撑商品 ▴` : `查看全部 ${supporting.length} 个支撑商品 ▾`}
          </button>
        </div>
      </div>
      ${variantsHtml}
      ${noteAreaHtml}
    </div>
  `;
}

function renderTop10Card(dir, { isExpanded, isEditingNote, presentationMode }) {
  const m = dir.metrics || {};
  const supporting = dir.supporting_products || [];
  const topPhotos = supporting.slice(0, 4);
  const rankCls = dir.rank === 1 ? "is-top1" : dir.rank === 2 ? "is-top2" : dir.rank === 3 ? "is-top3" : "";
  const priceStr = formatPriceBand(m.price_range);
  const salesStr = m.max_sales_30d !== null && m.max_sales_30d !== undefined
    ? `${formatInt(m.max_sales_30d)} / ${formatInt(m.median_sales_30d)}`
    : "—";
  const coveredCats = m.covered_categories || [];
  const hasRealTrim = m.excluded_anomaly_count > 0
    && Math.abs(Number(m.raw_peak_product_score || 0) - Number(m.peak_product_score || 0)) >= 0.05;

  const galleryHtml = topPhotos.length > 0
    ? `<div class="top10-gallery">
        ${topPhotos.map((p, idx) => `
          <div class="top10-photo-tile">
            <div class="top10-photo-box" data-top10-zoom="${dir.id}:${escapeHtml(p.asin)}" role="button" tabindex="0" aria-label="放大查看 ${escapeHtml(p.asin)} 高清印花细节" title="点击放大查看高清印花细节">
              ${p.total_score !== null && p.total_score !== undefined
                ? `<span class="dir-score-chip" style="left:8px;right:auto;">★ ${formatDecimal(p.total_score, 1)}</span>`
                : ""
              }
              <a class="top10-amz-link" href="${escapeHtml(p.amazon_url || `https://www.amazon.com/dp/${p.asin}`)}"
                target="_blank" rel="noopener noreferrer" title="打开 Amazon 商品页 (${escapeHtml(p.asin)})">↗</a>
              ${renderSafeImage(p.main_image_url || p.image_url, p.title || p.asin, "", dir.rank <= 6 && idx < 4)}
            </div>
            <div class="top10-photo-cap">
              <span class="top10-cap-sales">${formatInt(p.listing_sales_30d)}/月</span>
              <span class="top10-cap-price">${formatPrice(p.sales_price_usd, p.price_valid)}</span>
            </div>
            ${(p.price_anomaly || p.sales_suspect)
              ? `<div class="tag-row" style="margin-top:4px;">${renderCredibilityAnomalyBadges(p)}</div>`
              : ""
            }
          </div>
        `).join("")}
      </div>`
    : `<div class="empty-state" style="padding:20px;"><span>该方向暂未关联商品主图，可前往 05 补充归组样本。</span></div>`;

  const detailTableHtml = isExpanded
    ? `<div class="table-scroll" style="margin-top:2px;">
        <table class="data-table" style="min-width:860px;">
          <thead>
            <tr>
              <th>支撑商品 / ASIN</th>
              <th>来源类目</th>
              <th class="num-cell">04 潜力分</th>
              <th class="num-cell">30天估算销量</th>
              <th class="num-cell">有效售价</th>
              <th style="text-align:center;">可信度</th>
              <th>链接</th>
            </tr>
          </thead>
          <tbody>
            ${supporting.map((p) => `
              <tr>
                <td>
                  <div class="cell-product">
                    <div class="thumb-box" style="cursor:pointer;" data-top10-zoom="${dir.id}:${escapeHtml(p.asin)}" role="button" tabindex="0" title="点击放大看图">
                      ${renderSafeImage(p.main_image_url || p.image_url, p.asin)}
                    </div>
                    <div style="min-width:0;">
                      <div class="cell-title" title="${escapeHtml(p.title)}">${escapeHtml(p.title || "（无标题）")}</div>
                      <div class="cell-sub">
                        <span class="mono">${escapeHtml(p.asin)}</span>
                        <span>·</span>
                        <span class="mono">Parent: ${escapeHtml(p.parent_asin || "缺失")}</span>
                        <span>·</span>
                        <span>${escapeHtml(p.brand || "未知")}</span>
                        ${renderCredibilityAnomalyBadges(p)}
                      </div>
                    </div>
                  </div>
                </td>
                <td>
                  <div class="tag-row">
                    ${(p.source_categories || []).map((s) => renderSourceCategoryBadge(s, { showBsr: false })).join(" ")}
                  </div>
                </td>
                <td class="num-cell top10-accent-num">${p.total_score !== null && p.total_score !== undefined ? formatDecimal(p.total_score, 1) : "—"}</td>
                <td class="num-cell" style="font-weight:700;">${formatInt(p.listing_sales_30d)}</td>
                <td class="num-cell">${formatPrice(p.sales_price_usd, p.price_valid)}</td>
                <td style="text-align:center;">
                  <span class="conf-badge is-${escapeHtml(p.confidence_level || "medium")}">${escapeHtml(p.confidence_label || "中")} ${p.confidence_score ?? "—"}</span>
                </td>
                <td>
                  <a href="${escapeHtml(p.amazon_url || `https://www.amazon.com/dp/${p.asin}`)}" target="_blank" rel="noopener noreferrer" class="btn btn-ghost btn-sm">Amazon ↗</a>
                </td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>`
    : "";

  return `
    <article class="top10-card" id="top10-card-${dir.id}" data-top10-id="${dir.id}">
      <div class="top10-card-head">
        <div class="top10-card-head-main">
          <span class="top10-rank-badge ${rankCls}">#${dir.rank}</span>
          <div class="top10-card-title-wrap">
            <div class="top10-card-title-row">
              <h2 class="top10-card-title">${escapeHtml(dir.name)}</h2>
              ${renderDirectionTypeBadge(dir)}
              ${renderSeasonCountdownBadge(dir)}
              ${dir.source === "ai_suggested" ? renderDirectionSourceBadge(dir) : ""}
              ${renderDirectionAnomalyBadge(dir, { compact: false })}
              ${renderStarsReadonly(dir.feasibility_rating)}
            </div>
            ${dir.target_audience
              ? `<div class="top10-card-sub">目标受众：${escapeHtml(dir.target_audience)}</div>`
              : ""
            }
          </div>
        </div>

        <div class="top10-card-head-actions">
          <span class="score-pill ${dir.direction_score >= 75 ? "is-high" : dir.direction_score >= 55 ? "is-mid" : "is-low"}"
            style="cursor:help;" data-tip="${renderScoreTooltip(dir)}" tabindex="0">
            ${formatDecimal(dir.direction_score, 1)}
          </span>
          ${presentationMode
            ? ""
            : `<button type="button" class="btn btn-secondary btn-sm top10-edit-only" data-remove-shortlist="${dir.id}" title="移出 Top 10 入选，状态退回草稿（移出后 5 秒内可撤销）">
                移出入选
              </button>`
          }
        </div>
      </div>

      ${m.all_anomalous
        ? `<div class="dir-anomaly-banner is-warn">
            <span><strong>⚠ 全员异常兜底警示：</strong>本方向所有支撑商品均存在价格异常或销量存疑（<span class="mono">${escapeHtml((m.anomalous_asins || []).join(", "))}</span>），方向综合分已回退取原始峰值（${formatDecimal(m.peak_product_score, 1)}），请人工重点核实。</span>
          </div>`
        : (hasRealTrim
            ? `<div class="dir-anomaly-banner is-info">
                <span><strong>方向分已去极值：</strong>已剔除 <strong>${formatInt(m.excluded_anomaly_count)}</strong> 个异常支撑商品（<span class="mono">${escapeHtml((m.anomalous_asins || []).join(", "))}</span>），头部峰值分由 <strong>${formatDecimal(m.raw_peak_product_score, 1)}</strong> 修正为正常样本最高分 <strong>${formatDecimal(m.peak_product_score, 1)}</strong>。</span>
              </div>`
            : "")
      }

      <!-- Visual Reference Gallery (Core 3-4 Large Photos) -->
      ${galleryHtml}

      <!-- Open-Air Commercial Evidence Strip -->
      <div class="top10-evidence-bar">
        <div class="top10-ev-cell">
          <span class="top10-ev-lbl">独立父体 / 去重 ASIN</span>
          <span class="top10-ev-val top10-accent-num">${formatInt(m.parent_asin_count || 0)} 父体 <span class="top10-ev-sub">(${formatInt(m.asin_count || 0)} ASIN · ${formatInt(m.brand_count || 0)} 品牌)</span></span>
        </div>
        <div class="top10-ev-cell" title="仅统计最高值与中位数，严禁累加销量">
          <span class="top10-ev-lbl">最高 / 中位 30天销量</span>
          <span class="top10-ev-val">${salesStr}</span>
        </div>
        <div class="top10-ev-cell">
          <span class="top10-ev-lbl">有效售价带</span>
          <span class="top10-ev-val">${priceStr}</span>
        </div>
        <div class="top10-ev-cell">
          <span class="top10-ev-lbl">覆盖类目 (${coveredCats.length})</span>
          <div class="tag-row" style="margin-top:2px;">
            ${coveredCats.length
              ? coveredCats.map((c) => `<span class="badge badge-neutral top10-cat-pill"><span class="cat-dot" style="background:${escapeHtml(c.color || "#2552D9")};margin-right:4px;"></span>${escapeHtml(c.short_name || c.name)}</span>`).join("")
              : `<span class="top10-ev-sub">—</span>`
            }
          </div>
        </div>
        <div class="top10-ev-cell">
          <span class="top10-ev-lbl">支撑数据可信度</span>
          <div style="margin-top:2px;">${renderConfidencePill(dir.confidence_summary)}</div>
        </div>
      </div>

      <!-- Unified Design Brief (Variants + Style Tags + Notes + Copy Brief) -->
      ${renderDesignBriefSection(dir, { isExpanded, isEditingNote, presentationMode })}

      ${detailTableHtml}
    </article>
  `;
}

export function renderTop10View(
  container,
  {
    data,
    expandedIds = [],
    editingNoteId = null,
    presentationMode = false,
    toast = null,
    exportUrls = { json: "#", csv: "#" },
    handlers,
  }
) {
  if (!data) {
    container.innerHTML = `<div class="loading-state"><div class="spinner"></div><p>正在加载 Top 10 印花方向输出…</p></div>`;
    return;
  }

  const s = data.summary || {};
  const items = data.items || [];
  const shortlistedCount = s.shortlisted_count || 0;
  const exceedsLimit = Boolean(s.exceeds_limit || shortlistedCount > 10);
  const catDist = s.category_distribution || [];
  const expandedSet = new Set(expandedIds || []);

  const toastHtml = toast
    ? `<div class="top10-floating-toast ${toast.type === "error" ? "is-error" : "is-ok"}" id="top10-toast" role="status" aria-live="polite">
        <span class="top10-toast-msg">${escapeHtml(toast.message)}</span>
        <div class="top10-toast-actions">
          ${toast.undoDirId
            ? `<button type="button" class="btn btn-primary btn-sm" id="btn-top10-toast-undo" data-undo-dir="${toast.undoDirId}">撤销移出</button>`
            : ""
          }
          <button type="button" class="btn btn-ghost btn-sm" id="btn-top10-toast-dismiss">关闭</button>
        </div>
      </div>`
    : "";

  const exceedsBannerHtml = exceedsLimit && !presentationMode
    ? `<div class="alert alert-warn" id="top10-exceeds-banner" style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;">
        <span><strong>当前已入选 ${shortlistedCount} 个方向，超过 10 个目标上限</strong>，建议通过下方「移出入选」精简至 10 个以内再导出交接。</span>
        <button type="button" class="btn btn-secondary btn-sm" id="btn-top10-goto-s5-trim">前往 05 调整归组 →</button>
      </div>`
    : "";

  const tocHtml = shortlistedCount >= 2
    ? `<nav class="top10-toc" id="top10-toc" aria-label="一屏决策目录">
        <span class="top10-toc-label">快速跳转</span>
        <div class="top10-toc-list">
          ${items.map((d) => `
            <button type="button" class="top10-toc-chip" data-toc-jump="${d.id}" title="跳转到 #${d.rank} ${escapeHtml(d.name)}">
              <span class="top10-toc-rank">#${d.rank}</span>
              <span class="top10-toc-name">${escapeHtml(d.name)}</span>
              <span class="top10-toc-score">${formatDecimal(d.direction_score, 1)}</span>
            </button>
          `).join("")}
        </div>
      </nav>`
    : "";

  container.innerHTML = `
    <div class="top10-root ${presentationMode ? "is-presentation" : ""}" id="top10-root">
      <!-- Top Decision & Export Bar -->
      <section class="panel top10-summary-panel" aria-label="Top 10 决策与交接导出栏">
        <div class="top10-hero-bar">
          <div class="top10-hero-left">
            <div class="top10-status-big">
              <span class="top10-status-lbl">已入选</span>
              <span class="top10-count-num" id="top10-shortlisted-count" style="color:${shortlistedCount === 0 ? "var(--ink-faint)" : exceedsLimit ? "var(--warn)" : "var(--top10-accent-ink)"};">${shortlistedCount}</span>
              <span class="top10-status-unit">/ 10 个印花方向</span>
            </div>
            <span class="badge ${shortlistedCount === 0 ? "badge-neutral" : exceedsLimit ? "badge-warn" : "badge-ok"}">
              ${shortlistedCount === 0 ? "待从 05 标记入选" : exceedsLimit ? "建议精简至 10 个以内" : "宁缺毋滥 · 真实入选"}
            </span>
            <span class="badge badge-neutral mono">快照：${escapeHtml(data.selected_date || "—")}</span>
          </div>

          <div class="top10-hero-actions">
            ${shortlistedCount > 0
              ? `<div class="top10-mode-switch" role="group" aria-label="视图模式切换">
                  <button type="button" class="top10-mode-btn ${!presentationMode ? "is-active" : ""}" id="btn-top10-mode-edit" aria-pressed="${!presentationMode}">编辑模式</button>
                  <button type="button" class="top10-mode-btn ${presentationMode ? "is-active" : ""}" id="btn-top10-mode-present" aria-pressed="${presentationMode}">汇报模式</button>
                </div>`
              : ""
            }
            ${presentationMode
              ? ""
              : `<button type="button" class="btn btn-ghost btn-sm top10-edit-only" id="btn-top10-goto-s5">← 返回 05 印花归组</button>`
            }
            <a class="btn btn-secondary btn-sm ${shortlistedCount === 0 ? "is-disabled" : ""}" id="btn-export-csv"
              href="${shortlistedCount > 0 ? escapeHtml(exportUrls.csv) : "#"}"
              ${shortlistedCount === 0 ? 'aria-disabled="true" data-disabled-link="1" style="opacity:0.48;pointer-events:none;"' : 'download'}>
              导出 CSV 表格
            </a>
            <a class="btn btn-primary btn-sm ${shortlistedCount === 0 ? "is-disabled" : ""}" id="btn-export-json"
              href="${shortlistedCount > 0 ? escapeHtml(exportUrls.json) : "#"}"
              ${shortlistedCount === 0 ? 'aria-disabled="true" data-disabled-link="1" style="opacity:0.48;pointer-events:none;"' : 'download'}>
              导出 JSON 交接包
            </a>
          </div>
        </div>

        ${shortlistedCount > 0
          ? `<div class="top10-kpi-grid">
              <div class="top10-kpi-item">
                <div class="top10-ev-lbl">覆盖独立父体总数（去重）</div>
                <div class="top10-kpi-val top10-accent-num">${formatInt(s.total_unique_parents || 0)} <span class="top10-kpi-sub">父体 / ${formatInt(s.total_unique_asins || 0)} ASIN</span></div>
              </div>
              <div class="top10-kpi-item">
                <div class="top10-ev-lbl">覆盖目标类目数</div>
                <div class="top10-kpi-val">${formatInt(s.covered_category_count || 0)} <span class="top10-kpi-sub">/ 6 目标类目</span></div>
              </div>
              <div class="top10-kpi-item">
                <div class="top10-ev-lbl">类目覆盖分布</div>
                <div class="tag-row" style="margin-top:4px;">
                  ${catDist.length
                    ? catDist.map((c) => `<span class="badge badge-neutral top10-cat-pill"><span class="cat-dot" style="background:${escapeHtml(c.color)};margin-right:4px;"></span>${escapeHtml(c.short_name)} ×${c.direction_count}</span>`).join("")
                    : `<span class="top10-ev-sub">暂无</span>`
                  }
                </div>
              </div>
            </div>
            ${tocHtml}`
          : ""
        }
      </section>

      ${exceedsBannerHtml}

      <!-- Unified Ranked Top 10 Decision Cards or Empty Guide -->
      ${shortlistedCount === 0
        ? `<section class="panel" id="top10-empty-state" aria-label="暂无入选方向">
            <div class="empty-state" style="padding:52px 24px;">
              <span class="badge badge-info">宁缺毋滥 · 真实数据驱动</span>
              <h2 class="top10-card-title" style="margin-top:6px;">当前尚未标记入选 Top 10 的印花方向</h2>
              <p style="max-width:520px;margin:0;font-size:13px;line-height:1.6;">
                06 页面严格读取 05 模块中标记为「入选 Top 10 候选」的真实方向，绝不强行凑满 10 个或生成演示数据。
                请先在 05 印花归组完成主图聚合，并对高潜力方向点击「入选 Top 10 候选」。
              </p>
              <button type="button" class="btn btn-primary" id="btn-empty-goto-s5" style="margin-top:6px;">
                前往 05 归组并标记入选 →
              </button>
            </div>
          </section>`
        : `<div class="top10-card-list" id="top10-card-list" aria-label="Top 10 印花方向决策输出">
            ${items.map((dir) => renderTop10Card(dir, {
              isExpanded: expandedSet.has(dir.id),
              isEditingNote: editingNoteId === dir.id,
              presentationMode,
            })).join("")}
          </div>`
      }

      ${toastHtml}
    </div>
  `;

  bindChartInteractions(container);

  const dirMap = new Map(items.map((d) => [d.id, d]));

  document.getElementById("btn-top10-toast-dismiss")?.addEventListener("click", () => handlers.onDismissToast());
  document.getElementById("btn-top10-toast-undo")?.addEventListener("click", (e) => {
    const dirId = Number(e.currentTarget.getAttribute("data-undo-dir"));
    if (dirId) handlers.onUndoRemoveShortlist?.(dirId);
  });
  document.getElementById("btn-top10-goto-s5")?.addEventListener("click", () => handlers.onJumpToDirections());
  document.getElementById("btn-top10-goto-s5-trim")?.addEventListener("click", () => handlers.onJumpToDirections());
  document.getElementById("btn-empty-goto-s5")?.addEventListener("click", () => handlers.onJumpToDirections());

  document.getElementById("btn-top10-mode-edit")?.addEventListener("click", () => handlers.onSetPresentationMode?.(false));
  document.getElementById("btn-top10-mode-present")?.addEventListener("click", () => handlers.onSetPresentationMode?.(true));

  // Executive Overview Directory smooth jump
  container.querySelectorAll("[data-toc-jump]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.getAttribute("data-toc-jump");
      const target = document.getElementById(`top10-card-${id}`);
      if (!target) return;
      target.scrollIntoView({ behavior: "smooth", block: "start" });
      target.classList.remove("is-flash");
      void target.offsetWidth;
      target.classList.add("is-flash");
      setTimeout(() => target.classList.remove("is-flash"), 1400);
    });
  });

  // 1-Click Copy Design Brief
  container.querySelectorAll("[data-copy-brief]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = Number(btn.getAttribute("data-copy-brief"));
      const dir = dirMap.get(id);
      if (!dir) return;
      const text = buildDesignBriefText(dir);
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(text);
        } else {
          const ta = document.createElement("textarea");
          ta.value = text;
          ta.style.position = "fixed";
          ta.style.opacity = "0";
          document.body.appendChild(ta);
          ta.select();
          document.execCommand("copy");
          document.body.removeChild(ta);
        }
        handlers.onCopyBrief?.(dir.name);
      } catch {
        handlers.onCopyBrief?.(dir.name, true);
      }
    });
  });

  // On-demand POD remix variant suggestions generation
  container.querySelectorAll("[data-generate-variants]").forEach((btn) => {
    btn.addEventListener("click", () => {
      handlers.onGenerateVariants?.(Number(btn.getAttribute("data-generate-variants")));
    });
  });

  // Remove from shortlist -> status back to draft (with 5s undo toast)
  container.querySelectorAll("[data-remove-shortlist]").forEach((btn) => {
    btn.addEventListener("click", () => {
      handlers.onRemoveShortlist(Number(btn.getAttribute("data-remove-shortlist")));
    });
  });

  // Expand / collapse supporting products
  container.querySelectorAll("[data-top10-expand]").forEach((btn) => {
    btn.addEventListener("click", () => {
      handlers.onToggleExpand(Number(btn.getAttribute("data-top10-expand")));
    });
  });

  // Inline click-to-edit handoff notes (mouse + keyboard Enter/Space)
  container.querySelectorAll("[data-note-edit]").forEach((el) => {
    const startEdit = () => handlers.onStartEditNote(Number(el.getAttribute("data-note-edit")));
    el.addEventListener("click", startEdit);
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        startEdit();
      }
    });
  });
  container.querySelectorAll("[data-note-cancel]").forEach((btn) => {
    btn.addEventListener("click", () => handlers.onCancelEditNote());
  });
  container.querySelectorAll("[data-note-save]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = Number(btn.getAttribute("data-note-save"));
      const textarea = document.getElementById(`top10-note-input-${id}`);
      handlers.onSaveNote(id, textarea ? textarea.value : "");
    });
  });

  // Prevent disabled export links & keep Amazon link clicks from triggering lightbox zoom
  container.querySelectorAll("[data-disabled-link]").forEach((a) => {
    a.addEventListener("click", (e) => e.preventDefault());
  });
  container.querySelectorAll(".top10-amz-link").forEach((a) => {
    a.addEventListener("click", (e) => e.stopPropagation());
  });

  // HD Lightbox zoom on any reference photo or expanded product thumbnail (mouse + keyboard)
  container.querySelectorAll("[data-top10-zoom]").forEach((el) => {
    const openZoom = () => {
      const [idStr, asin] = el.getAttribute("data-top10-zoom").split(":");
      const dir = dirMap.get(Number(idStr));
      const prod = (dir?.supporting_products || []).find((p) => p.asin === asin);
      if (prod) openImageLightbox(prod, {});
    };
    el.addEventListener("click", openZoom);
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        openZoom();
      }
    });
  });
}
