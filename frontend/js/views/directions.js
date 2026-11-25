/**
 * Stage 05: Print Pattern Recognition & Direction Grouping Workbench.
 * Split 60/40 workspace:
 * - Left (60%): High-potential candidate image pool from Stage 04 with Lightbox zoom & batch selection.
 * - Right (40%): Print direction cards with 2x2 photo collage, real-time commercial aggregation,
 *   explainable direction score tooltip, feasibility stars, and Top 10 shortlist toggle.
 */

import { bindChartInteractions } from "../charts.js";
import {
  escapeHtml,
  formatDecimal,
  formatInt,
  formatPrice,
  renderAiV2Tags,
  renderCredibilityAnomalyBadges,
  renderDirectionAnomalyBadge,
  renderSafeImage,
  renderSourceCategoryBadge,
} from "../format.js";

const STATUS_META = {
  shortlisted: { cls: "badge-ok", label: "入选 Top 10 候选" },
  draft: { cls: "badge-neutral", label: "草稿" },
  archived: { cls: "badge-warn", label: "已归档" },
};

export function renderDirectionTypeBadge(dir) {
  const isStandalone = dir && dir.direction_type === "standalone_hit";
  return isStandalone
    ? `<span class="badge badge-standalone" title="单品数据极强的独立爆款方向">独立爆款</span>`
    : `<span class="badge badge-cluster" title="多商品聚合验证的趋势集群方向">趋势集群</span>`;
}

export function renderDirectionSourceBadge(dir) {
  const isAi = dir && dir.source === "ai_suggested";
  return isAi
    ? `<span class="badge badge-ai-source" title="由 DeepSeek 视觉与聚类模型自动归纳生成">AI 推荐</span>`
    : `<span class="badge badge-neutral" title="由人工手动创建">人工创建</span>`;
}

function renderScoreBreakdownTooltip(dir) {
  const m = dir.metrics || {};
  const b = m.score_breakdown || {};
  const feasSign = (b.feasibility_delta || 0) >= 0 ? "+" : "";
  const anomalyNote = m.all_anomalous
    ? `<br>• <span style="color:#FCA5A5;">⚠ 全员异常兜底：方向内全部商品均异常，回退取原始峰值</span>`
    : (m.excluded_anomaly_count > 0
        ? `<br>• 去极值修正：已剔除 ${m.excluded_anomaly_count} 个异常项（原始峰值 ${formatDecimal(m.raw_peak_product_score, 1)} → ${formatDecimal(b.base_score || 0, 1)}）`
        : "");
  return escapeHtml(
    `<strong>方向综合推荐分：${formatDecimal(dir.direction_score, 1)}</strong><br>`
    + `• 头部验证基础分（峰值 04 分）：<strong>${formatDecimal(b.base_score || 0, 1)}</strong><br>`
    + `• 独立父体广度加分（${m.parent_asin_count || 0} 父体）：<strong>+${formatDecimal(b.breadth_bonus || 0, 1)}</strong><br>`
    + `• 跨类目加分（${b.cross_category_reason || "—"}）：<strong>+${formatDecimal(b.cross_category_bonus || 0, 1)}</strong><br>`
    + `• 二创可行性微调（${dir.feasibility_rating}★）：<strong>${feasSign}${formatDecimal(b.feasibility_delta || 0, 1)}</strong>`
    + anomalyNote
  );
}

export function closeImageLightbox() {
  const el = document.getElementById("dir-lightbox-overlay");
  if (!el) return;
  el.classList.remove("is-open");
  el.setAttribute("aria-hidden", "true");
}

export function openImageLightbox(product, { isSelected = false, onToggleSelect } = {}) {
  let overlay = document.getElementById("dir-lightbox-overlay");
  if (!overlay) {
    overlay = document.createElement("div");
    overlay.id = "dir-lightbox-overlay";
    overlay.className = "lightbox-overlay";
    overlay.setAttribute("aria-hidden", "true");
    document.body.appendChild(overlay);
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) closeImageLightbox();
    });
  }

  const photos = (product.photos && product.photos.length)
    ? product.photos
    : (product.image_url ? [product.image_url] : []);
  const mainUrl = photos[0] || "";
  let selectedNow = Boolean(isSelected);

  overlay.innerHTML = `
    <div class="lightbox-card" role="dialog" aria-modal="true" aria-label="高清印花主图预览">
      <div class="lightbox-media">
        <div id="lb-main-wrap" style="width:100%;display:flex;align-items:center;justify-content:center;min-height:360px;">
          ${mainUrl
            ? `<img src="${escapeHtml(mainUrl)}" alt="${escapeHtml(product.title || product.asin)}" class="lightbox-main-img" referrerpolicy="no-referrer">`
            : `<div class="img-fallback" style="height:320px;"><span>无可用主图</span></div>`
          }
        </div>
        ${photos.length > 1
          ? `<div class="drawer-thumbs" style="justify-content:center;">
              ${photos.slice(0, 7).map((url, idx) => `
                <button type="button" class="drawer-thumb-btn ${idx === 0 ? "is-active" : ""}" data-lb-photo="${escapeHtml(url)}">
                  <img src="${escapeHtml(url)}" alt="图 ${idx + 1}" loading="lazy" referrerpolicy="no-referrer">
                </button>
              `).join("")}
            </div>`
          : ""
        }
      </div>
      <div class="lightbox-info">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;">
          <div class="tag-row">
            ${product.total_score !== null && product.total_score !== undefined
              ? `<span class="badge badge-info">04 评分 ${formatDecimal(product.total_score, 1)}</span>`
              : ""
            }
            ${product.rank ? `<span class="badge badge-neutral mono">#${product.rank}</span>` : ""}
            ${renderCredibilityAnomalyBadges(product, { compact: false })}
          </div>
          <button type="button" class="icon-btn" id="lb-close-btn" aria-label="关闭放大预览">✕</button>
        </div>

        <h3 style="margin:0;font-size:14.5px;line-height:1.45;color:var(--ink-primary);">${escapeHtml(product.title || "（无标题）")}</h3>

        <div class="mono" style="font-size:11.5px;color:var(--ink-muted);">
          ASIN: ${escapeHtml(product.asin)} · Parent: ${escapeHtml(product.parent_asin || "缺失")} · ${escapeHtml(product.brand || "未知品牌")}
        </div>

        <div class="tag-row">
          ${(product.source_categories || []).map((s) => renderSourceCategoryBadge(s, { showBsr: false })).join(" ")}
        </div>

        <div class="kv-grid" style="margin-top:4px;">
          <div class="kv-box">
            <div class="kv-label">30 天估算销量</div>
            <div class="kv-val" style="color:var(--accent);">${formatInt(product.listing_sales_30d)}</div>
          </div>
          <div class="kv-box">
            <div class="kv-label">有效售价</div>
            <div class="kv-val">${formatPrice(product.price, product.price_valid !== false && product.price !== null)}</div>
          </div>
        </div>

        ${(product.ai_analysis && (product.ai_analysis.print_subject || product.ai_analysis.slogan_text || product.ai_analysis.season_tag || product.ai_analysis.motif || product.ai_analysis.transferability || (product.ai_analysis.theme_tags || []).length))
          ? `<div style="padding:8px 10px;border-radius:8px;background:var(--info-soft);border:1px solid var(--info-border);font-size:12px;">
              <div style="font-weight:700;color:var(--ink-primary);margin-bottom:4px;">✦ AI 视觉识别摘要</div>
              ${renderAiV2Tags(product.ai_analysis, { compact: true })
                ? `<div class="tag-row" style="margin-bottom:4px;">${renderAiV2Tags(product.ai_analysis, { compact: true })}</div>`
                : ""
              }
              ${product.ai_analysis.print_subject ? `<div>主体：${escapeHtml(product.ai_analysis.print_subject)}</div>` : ""}
              ${product.ai_analysis.slogan_text ? `<div>文案：<span class="mono">${escapeHtml(product.ai_analysis.slogan_text)}</span></div>` : ""}
              ${(product.ai_analysis.theme_tags || []).length
                ? `<div class="tag-row" style="margin-top:4px;">${product.ai_analysis.theme_tags.map((t) => `<span class="badge badge-info" style="font-size:10.5px;">${escapeHtml(t)}</span>`).join("")}</div>`
                : ""
              }
            </div>`
          : ""
        }

        ${(product.assigned_directions && product.assigned_directions.length)
          ? `<div>
              <div style="font-size:11.5px;color:var(--ink-muted);margin-bottom:5px;">已归入方向：</div>
              <div class="tag-row">
                ${product.assigned_directions.map((d) => `<span class="badge badge-ok">${escapeHtml(d.name)}</span>`).join("")}
              </div>
            </div>`
          : `<div style="font-size:12px;color:var(--ink-faint);">尚未归入任何印花方向</div>`
        }

        <div style="margin-top:auto;display:flex;gap:8px;flex-wrap:wrap;padding-top:10px;border-top:1px solid var(--border);">
          ${onToggleSelect
            ? `<button type="button" class="btn ${selectedNow ? "btn-secondary" : "btn-primary"}" id="lb-toggle-select" style="flex:1;">
                ${selectedNow ? "✓ 已勾选（点击取消）" : "+ 勾选此商品用于归组"}
              </button>`
            : ""
          }
          <button type="button" class="btn btn-secondary" id="lb-close-btn-bottom">关闭</button>
        </div>
      </div>
    </div>
  `;

  overlay.classList.add("is-open");
  overlay.setAttribute("aria-hidden", "false");

  overlay.querySelectorAll("[data-lb-photo]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const url = btn.getAttribute("data-lb-photo");
      const wrap = document.getElementById("lb-main-wrap");
      if (wrap) {
        wrap.innerHTML = `<img src="${escapeHtml(url)}" alt="${escapeHtml(product.asin)}" class="lightbox-main-img" referrerpolicy="no-referrer">`;
      }
      overlay.querySelectorAll(".drawer-thumb-btn").forEach((b) => b.classList.remove("is-active"));
      btn.classList.add("is-active");
    });
  });

  document.getElementById("lb-close-btn")?.addEventListener("click", closeImageLightbox);
  document.getElementById("lb-close-btn-bottom")?.addEventListener("click", closeImageLightbox);
  document.getElementById("lb-toggle-select")?.addEventListener("click", (e) => {
    selectedNow = !selectedNow;
    onToggleSelect(product.asin, selectedNow);
    const btn = e.currentTarget;
    btn.className = `btn ${selectedNow ? "btn-secondary" : "btn-primary"}`;
    btn.textContent = selectedNow ? "✓ 已勾选（点击取消）" : "+ 勾选此商品用于归组";
  });
}

function openDirectionFormModal({ mode = "create", direction = null, initialAsins = [], onSubmit }) {
  const overlay = document.getElementById("review-modal-overlay");
  const body = document.getElementById("review-modal-body");
  const titleEl = document.getElementById("review-modal-title");
  if (!overlay || !body) return;

  const isEdit = mode === "edit" && direction;
  titleEl.textContent = isEdit ? `编辑印花方向 · ${direction.name}` : "新建印花方向";
  const tagsStr = isEdit ? (direction.style_tags || []).join(", ") : "";
  const feas = isEdit ? Number(direction.feasibility_rating || 3) : 3;
  const status = isEdit ? direction.status : "draft";

  body.innerHTML = `
    ${!isEdit && initialAsins.length
      ? `<div class="alert alert-info" style="margin:0;">
          创建后将自动归入当前已勾选的 <strong>${initialAsins.length}</strong> 件商品，并立即计算方向商业聚合指标。
        </div>`
      : ""
    }

    <label class="field">
      <span class="field-label">方向主题名称 *</span>
      <input type="text" id="dir-form-name" class="input-control" maxlength="64"
        value="${escapeHtml(isEdit ? direction.name : "")}"
        placeholder="例如：复古美式圣诞拼色文字 / 幽默节日南瓜插画">
    </label>

    <div class="form-grid">
      <label class="field">
        <span class="field-label">视觉风格 / 元素标签（逗号分隔）</span>
        <input type="text" id="dir-form-tags" class="input-control"
          value="${escapeHtml(tagsStr)}"
          placeholder="复古做旧, 手绘插画, 文字梗">
      </label>
      <label class="field">
        <span class="field-label">目标受众</span>
        <input type="text" id="dir-form-audience" class="input-control" maxlength="80"
          value="${escapeHtml(isEdit ? direction.target_audience || "" : "")}"
          placeholder="例如：年轻女性 / 节日派对人群 / 通用">
      </label>
    </div>

    <div class="form-grid">
      <label class="field">
        <span class="field-label">人工二创可行性评分（1~5 星）</span>
        <select id="dir-form-feasibility" class="select-control">
          <option value="5" ${feas === 5 ? "selected" : ""}>5 ★ — 极易二创且差异化空间大 (+6分)</option>
          <option value="4" ${feas === 4 ? "selected" : ""}>4 ★ — 较易二创 (+3分)</option>
          <option value="3" ${feas === 3 ? "selected" : ""}>3 ★ — 常规可行 (0分)</option>
          <option value="2" ${feas === 2 ? "selected" : ""}>2 ★ — 有一定设计门槛 (-3分)</option>
          <option value="1" ${feas === 1 ? "selected" : ""}>1 ★ — 较难规避同质化 (-6分)</option>
        </select>
      </label>
      <label class="field">
        <span class="field-label">方向状态</span>
        <select id="dir-form-status" class="select-control">
          <option value="draft" ${status === "draft" ? "selected" : ""}>草稿（持续归组中）</option>
          <option value="shortlisted" ${status === "shortlisted" ? "selected" : ""}>入选 Top 10 候选</option>
          <option value="archived" ${status === "archived" ? "selected" : ""}>归档（暂不开发）</option>
        </select>
      </label>
    </div>

    <label class="field">
      <span class="field-label">设计参考要点与备注</span>
      <textarea id="dir-form-notes" class="textarea-control" placeholder="记录核心文案结构、字体风格、配色或可二创切入点…">${escapeHtml(isEdit ? direction.notes || "" : "")}</textarea>
    </label>

    <div id="dir-form-err"></div>

    <div style="display:flex;justify-content:flex-end;gap:8px;">
      <button type="button" class="btn btn-secondary" id="dir-form-cancel">取消</button>
      <button type="button" class="btn btn-primary" id="dir-form-submit">
        ${isEdit ? "保存修改" : initialAsins.length ? `创建方向并归入 ${initialAsins.length} 件` : "创建方向"}
      </button>
    </div>
  `;

  overlay.classList.add("is-open");
  overlay.setAttribute("aria-hidden", "false");
  document.getElementById("dir-form-name")?.focus();

  const close = () => {
    overlay.classList.remove("is-open");
    overlay.setAttribute("aria-hidden", "true");
  };
  document.getElementById("dir-form-cancel")?.addEventListener("click", close);
  document.getElementById("dir-form-submit")?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    const name = (document.getElementById("dir-form-name")?.value || "").trim();
    const errBox = document.getElementById("dir-form-err");
    if (!name) {
      if (errBox) errBox.innerHTML = `<div class="alert alert-danger">请输入方向主题名称。</div>`;
      return;
    }
    const payload = {
      name,
      style_tags: document.getElementById("dir-form-tags")?.value || "",
      target_audience: document.getElementById("dir-form-audience")?.value || "",
      feasibility_rating: Number(document.getElementById("dir-form-feasibility")?.value || 3),
      status: document.getElementById("dir-form-status")?.value || "draft",
      notes: document.getElementById("dir-form-notes")?.value || "",
    };
    if (!isEdit && initialAsins.length) {
      payload.asins = initialAsins;
    }
    btn.disabled = true;
    try {
      await onSubmit(payload);
      close();
    } catch (err) {
      if (errBox) errBox.innerHTML = `<div class="alert alert-danger">${escapeHtml(err.message)}</div>`;
    } finally {
      btn.disabled = false;
    }
  });
}

function openDeleteDirectionModal(dir, onConfirm) {
  const overlay = document.getElementById("review-modal-overlay");
  const body = document.getElementById("review-modal-body");
  const titleEl = document.getElementById("review-modal-title");
  if (!overlay || !body) return;

  titleEl.textContent = "确认删除印花方向";
  body.innerHTML = `
    <div class="alert alert-warn" style="margin:0;">
      <strong>确认删除印花方向「${escapeHtml(dir.name)}」？</strong>
      <div style="margin-top:4px;font-size:12px;">
        将解除该方向与 <strong>${dir.metrics?.asin_count || 0}</strong> 件商品的归组关系，<strong>不会删除或影响任何原始商品数据与预处理审核状态</strong>。
      </div>
    </div>
    <div style="display:flex;justify-content:flex-end;gap:8px;">
      <button type="button" class="btn btn-secondary" id="dir-del-cancel">取消</button>
      <button type="button" class="btn btn-primary" style="background:var(--danger);" id="dir-del-confirm">确认删除</button>
    </div>
  `;

  overlay.classList.add("is-open");
  overlay.setAttribute("aria-hidden", "false");
  const close = () => {
    overlay.classList.remove("is-open");
    overlay.setAttribute("aria-hidden", "true");
  };
  document.getElementById("dir-del-cancel")?.addEventListener("click", close);
  document.getElementById("dir-del-confirm")?.addEventListener("click", async (e) => {
    e.currentTarget.disabled = true;
    try {
      await onConfirm(dir.id);
      close();
    } finally {
      e.currentTarget.disabled = false;
    }
  });
}

export function closeDirectionDetailModal() {
  const el = document.getElementById("dir-detail-overlay");
  if (!el) return;
  el.classList.remove("is-open");
  el.setAttribute("aria-hidden", "true");
}

export function openDirectionDetailModal(dir, handlers = {}) {
  if (!dir) return;
  let overlay = document.getElementById("dir-detail-overlay");
  if (!overlay) {
    overlay = document.createElement("div");
    overlay.id = "dir-detail-overlay";
    overlay.className = "dir-detail-overlay";
    overlay.setAttribute("aria-hidden", "true");
    document.body.appendChild(overlay);
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) {
        closeDirectionDetailModal();
        handlers.onCloseDirectionDetail?.();
      }
    });
  }

  const prevBodyScroll = overlay.querySelector(".dir-detail-body")?.scrollTop || 0;
  const m = dir.metrics || {};
  const b = m.score_breakdown || {};
  const stMeta = STATUS_META[dir.status] || STATUS_META.draft;
  const isShortlisted = dir.status === "shortlisted";
  const linked = dir.linked_products || [];
  const coveredCats = m.covered_categories || [];
  const priceStr = m.price_range
    ? `$${formatDecimal(m.price_range.min, 2)} – $${formatDecimal(m.price_range.max, 2)}`
    : "—";
  const salesStr = m.max_sales_30d !== null && m.max_sales_30d !== undefined
    ? `${formatInt(m.max_sales_30d)} / ${formatInt(m.median_sales_30d)}`
    : "—";
  const feasSign = (b.feasibility_delta || 0) >= 0 ? "+" : "";

  overlay.innerHTML = `
    <div class="dir-detail-dialog" role="dialog" aria-modal="true" aria-label="印花方向详情与关联商品">
      <div class="dir-detail-head">
        <div style="min-width:0;flex:1;">
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
            <span class="drawer-eyebrow" style="margin:0;">PRINT DIRECTION DETAIL · #${dir.id}</span>
            ${renderDirectionTypeBadge(dir)}
            ${renderDirectionSourceBadge(dir)}
            <span class="badge ${stMeta.cls}">${stMeta.label}</span>
            ${renderDirectionAnomalyBadge(dir, { compact: false })}
          </div>
          <h2 style="margin:4px 0 0;font-size:19px;font-weight:800;color:var(--ink-primary);line-height:1.3;">
            ${escapeHtml(dir.name)}
          </h2>
        </div>

        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
          <div style="display:flex;align-items:center;gap:5px;padding:4px 9px;border-radius:var(--radius-sm);background:var(--bg-elevated);border:1px solid var(--border);">
            <span style="font-size:11.5px;color:var(--ink-muted);">可行性</span>
            <div class="star-picker" role="group" aria-label="人工二创可行性评分">
              ${[1, 2, 3, 4, 5].map((star) => `
                <button type="button" class="star-btn ${star <= dir.feasibility_rating ? "is-on" : ""}"
                  data-modal-rate="${star}" title="评为 ${star} 星">★</button>
              `).join("")}
            </div>
          </div>
          <button type="button" class="btn btn-secondary btn-sm" id="dir-detail-edit-btn">编辑方向</button>
          <button type="button" class="btn ${isShortlisted ? "btn-ok-soft" : "btn-primary"} btn-sm"
            id="dir-detail-shortlist-btn" data-next-status="${isShortlisted ? "draft" : "shortlisted"}">
            ${isShortlisted ? "✓ 已入选 Top 10" : "入选 Top 10 候选"}
          </button>
          <button type="button" class="icon-btn" id="dir-detail-close-btn" aria-label="关闭方向详情">✕</button>
        </div>
      </div>

      <div class="dir-detail-body">
        <!-- Top Commercial Overview & Score Breakdown -->
        <div class="dir-detail-overview">
          <div class="dir-detail-score-box">
            <div style="font-size:11.5px;color:var(--ink-muted);font-weight:600;">方向综合推荐分</div>
            <div class="dir-detail-score-num">${formatDecimal(dir.direction_score, 1)}</div>
            <div class="dir-detail-score-parts">
              <span>峰值04分 <strong>${formatDecimal(b.base_score || 0, 1)}</strong></span>
              <span>广度 <strong>+${formatDecimal(b.breadth_bonus || 0, 1)}</strong></span>
              <span>跨类目 <strong>+${formatDecimal(b.cross_category_bonus || 0, 1)}</strong></span>
              <span>可行性 <strong>${feasSign}${formatDecimal(b.feasibility_delta || 0, 1)}</strong></span>
            </div>
          </div>

          <div class="dir-detail-kpis">
            <div class="kv-box">
              <div class="kv-label">独立父体 / 去重 ASIN / 品牌</div>
              <div class="kv-val" style="color:var(--accent);">${formatInt(m.parent_asin_count || 0)} <span style="font-size:12px;font-weight:500;color:var(--ink-muted);">父体 · ${formatInt(m.asin_count || 0)} ASIN · ${formatInt(m.brand_count || 0)} 品牌</span></div>
            </div>
            <div class="kv-box">
              <div class="kv-label">最高 / 中位 30 天估算销量</div>
              <div class="kv-val">${salesStr}</div>
            </div>
            <div class="kv-box">
              <div class="kv-label">有效价格带 (USD)</div>
              <div class="kv-val">${priceStr}</div>
            </div>
            <div class="kv-box">
              <div class="kv-label">覆盖类目 (${coveredCats.length})</div>
              <div class="tag-row" style="margin-top:3px;">
                ${coveredCats.length
                  ? coveredCats.map((c) => `<span class="badge badge-neutral" style="font-size:11px;"><span class="cat-dot" style="background:${escapeHtml(c.color || "#3B6BF6")};margin-right:3px;"></span>${escapeHtml(c.short_name || c.name)}</span>`).join("")
                  : `<span style="font-size:12px;color:var(--ink-faint);">暂无</span>`
                }
              </div>
            </div>
          </div>
        </div>

        <!-- Anomaly / Outlier Trimming Notice (when applicable) -->
        ${m.all_anomalous
          ? `<div class="dir-anomaly-banner is-warn">
              <div>
                <strong>⚠ 全员异常兜底警示：</strong>该方向全部关联商品均触发「价格异常」或「销量存疑」（异常 ASIN：<span class="mono">${escapeHtml((m.anomalous_asins || []).join(", "))}</span>），方向峰值分已兜底回退取原始最高分（${formatDecimal(m.peak_product_score, 1)}），建议人工核实后再决定是否入选。
              </div>
            </div>`
          : (m.excluded_anomaly_count > 0
              ? `<div class="dir-anomaly-banner is-info">
                  <div>
                    <strong>方向分已去极值：</strong>计算头部峰值分时已自动剔除 <strong>${formatInt(m.excluded_anomaly_count)}</strong> 个异常商品（<span class="mono">${escapeHtml((m.anomalous_asins || []).join(", "))}</span>），峰值由 <strong>${formatDecimal(m.raw_peak_product_score, 1)}</strong> 修正为正常样本最高分 <strong>${formatDecimal(m.peak_product_score, 1)}</strong>（原始商品仍保留在此列表中供核验）。
                  </div>
                </div>`
              : "")
        }

        <!-- Complete LLM Analysis / Design Direction Output (NEVER truncated!) -->
        <div class="dir-detail-notes-box">
          <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;">
            <strong style="font-size:13.5px;color:var(--ink-primary);">
              ${dir.source === "ai_suggested" ? "✦ 大模型完整趋势分析与二创建议" : "方向设计要点与备注"}
            </strong>
            ${dir.target_audience
              ? `<span class="badge badge-both">目标受众：${escapeHtml(dir.target_audience)}</span>`
              : `<span class="badge badge-neutral">目标受众待补充</span>`
            }
          </div>
          ${(dir.style_tags || []).length
            ? `<div class="tag-row">
                ${dir.style_tags.map((t) => `<span class="badge badge-info">${escapeHtml(t)}</span>`).join("")}
              </div>`
            : ""
          }
          <div class="dir-detail-notes-full">${dir.notes ? escapeHtml(dir.notes.trim()) : '<span style="color:var(--ink-faint);">暂无详细说明，可点击右上角「编辑方向」补充二创要点。</span>'}</div>
        </div>

        <!-- Linked Products Detail Grid -->
        <div>
          <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:10px;flex-wrap:wrap;">
            <h3 style="margin:0;font-size:14.5px;font-weight:700;color:var(--ink-primary);">
              该方向关联商品明细 (${formatInt(linked.length)} 件)
            </h3>
            <span style="font-size:12px;color:var(--ink-muted);">点击主图可放大高清查看印花细节</span>
          </div>

          ${linked.length === 0
            ? `<div class="empty-state" style="padding:28px 16px;">
                <strong>该方向暂未关联商品</strong>
                <span>关闭弹窗后，在左侧高潜力选图池勾选商品并点击「归入已有方向」即可添加。</span>
              </div>`
            : `<div class="dir-detail-prod-grid">
                ${linked.map((lp) => {
                  const ai = lp.ai_analysis || null;
                  return `
                    <article class="dir-detail-prod-card">
                      <div class="dir-detail-prod-media" data-detail-zoom="${escapeHtml(lp.asin)}" title="点击放大高清看图">
                        ${lp.total_score !== null && lp.total_score !== undefined
                          ? `<span class="dir-score-chip" style="left:6px;right:auto;font-size:10.5px;">★ ${formatDecimal(lp.total_score, 1)}</span>`
                          : ""
                        }
                        ${renderSafeImage(lp.image_url, lp.title || lp.asin, "", true)}
                      </div>
                      <div class="dir-detail-prod-info">
                        <div class="dir-detail-prod-title" title="${escapeHtml(lp.title || "")}">
                          ${escapeHtml(lp.title || "（无标题）")}
                        </div>
                        <div class="mono" style="font-size:11px;color:var(--ink-muted);">
                          ${escapeHtml(lp.asin)} · ${escapeHtml(lp.brand || "未知品牌")}
                        </div>
                        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:12px;">
                          <strong style="color:var(--accent);">${formatInt(lp.listing_sales_30d)}/月</strong>
                          <strong style="color:var(--ink-primary);">${formatPrice(lp.price, lp.price_valid)}</strong>
                          ${lp.rating_stars ? `<span style="color:#D97706;">${formatDecimal(lp.rating_stars, 1)}★ (${formatInt(lp.ratings_count)})</span>` : ""}
                        </div>
                        <div class="tag-row">
                          ${renderCredibilityAnomalyBadges(lp)}
                          ${(lp.source_categories || []).slice(0, 3).map((s) => renderSourceCategoryBadge(s, { showBsr: false })).join("")}
                        </div>
                        ${ai && (ai.print_subject || ai.slogan_text || ai.season_tag || ai.motif || ai.transferability || ai.needs_v2_upgrade)
                          ? `<div class="dir-detail-prod-ai">
                              ${renderAiV2Tags(ai, { compact: true })
                                ? `<div class="tag-row" data-ai-v2-row="dir-detail" style="margin-bottom:2px;">${renderAiV2Tags(ai, { compact: true })}</div>`
                                : ""
                              }
                              ${ai.print_subject ? `<div><strong>AI 印花主体：</strong>${escapeHtml(ai.print_subject)}</div>` : ""}
                              ${ai.slogan_text ? `<div><strong>文案 OCR：</strong><span class="mono">${escapeHtml(ai.slogan_text)}</span></div>` : ""}
                            </div>`
                          : ""
                        }
                        <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:auto;padding-top:4px;">
                          <a href="${escapeHtml(lp.amazon_url || `https://www.amazon.com/dp/${lp.asin}`)}" target="_blank" rel="noopener noreferrer" class="btn btn-ghost btn-sm" style="height:26px;font-size:11.5px;">Amazon ↗</a>
                          <button type="button" class="btn btn-ghost btn-sm" style="color:var(--danger);height:26px;font-size:11.5px;" data-detail-unlink="${escapeHtml(lp.asin)}">移出此方向</button>
                        </div>
                      </div>
                    </article>
                  `;
                }).join("")}
              </div>`
          }
        </div>
      </div>
    </div>
  `;

  overlay.classList.add("is-open");
  overlay.setAttribute("aria-hidden", "false");
  const bodyEl = overlay.querySelector(".dir-detail-body");
  if (bodyEl && prevBodyScroll > 0) {
    bodyEl.scrollTop = prevBodyScroll;
  }

  const prodMap = new Map(linked.map((p) => [p.asin, p]));
  document.getElementById("dir-detail-close-btn")?.addEventListener("click", () => {
    closeDirectionDetailModal();
    handlers.onCloseDirectionDetail?.();
  });
  document.getElementById("dir-detail-edit-btn")?.addEventListener("click", () => {
    openDirectionFormModal({
      mode: "edit",
      direction: dir,
      onSubmit: (payload) => handlers.onUpdateDirection?.(dir.id, payload),
    });
  });
  document.getElementById("dir-detail-shortlist-btn")?.addEventListener("click", (e) => {
    const nextStatus = e.currentTarget.getAttribute("data-next-status");
    handlers.onUpdateDirection?.(dir.id, { status: nextStatus });
  });
  overlay.querySelectorAll("[data-modal-rate]").forEach((btn) => {
    btn.addEventListener("click", () => {
      handlers.onUpdateDirection?.(dir.id, { feasibility_rating: Number(btn.getAttribute("data-modal-rate")) });
    });
  });
  overlay.querySelectorAll("[data-detail-zoom]").forEach((el) => {
    el.addEventListener("click", () => {
      const p = prodMap.get(el.getAttribute("data-detail-zoom"));
      if (p) openImageLightbox(p, {});
    });
  });
  overlay.querySelectorAll("[data-detail-unlink]").forEach((btn) => {
    btn.addEventListener("click", () => {
      handlers.onRemoveLinks?.(dir.id, [btn.getAttribute("data-detail-unlink")]);
    });
  });
}

function renderDirectionCard(dir, selectedCount = 0) {
  const m = dir.metrics || {};
  const stMeta = STATUS_META[dir.status] || STATUS_META.draft;
  const isShortlisted = dir.status === "shortlisted";
  const imgs = dir.preview_images || [];
  const priceStr = m.price_range
    ? `$${formatDecimal(m.price_range.min, 0)}–$${formatDecimal(m.price_range.max, 0)}`
    : "—";
  const salesStr = m.max_sales_30d !== null && m.max_sales_30d !== undefined
    ? `${formatInt(m.max_sales_30d)} / ${formatInt(m.median_sales_30d)}`
    : "—";
  const catNames = (m.covered_categories || []).map((c) => c.short_name || c.name);
  const catSummary = catNames.length
    ? (catNames.length <= 2 ? catNames.join(" · ") : `${catNames.slice(0, 2).join(" · ")} +${catNames.length - 2}`)
    : "未覆盖";
  const tags = dir.style_tags || [];
  const visibleTags = tags.slice(0, 3);
  const extraTagCount = Math.max(0, tags.length - 3);

  const collageHtml = imgs.length > 0
    ? `<div class="dir-collage ${imgs.length === 1 ? "is-single" : ""}" data-open-dir-detail="${dir.id}" title="点击打开方向详情与全部商品">
        ${imgs.slice(0, 4).map((im) => `
          <div class="dir-collage-cell">
            <img src="${escapeHtml(im.image_url)}" alt="${escapeHtml(im.asin)}" loading="lazy" referrerpolicy="no-referrer">
          </div>
        `).join("")}
      </div>`
    : `<div class="dir-collage-empty" data-open-dir-detail="${dir.id}" title="点击打开方向详情">暂无主图<br>从左侧归入</div>`;

  return `
    <article class="dir-card ${isShortlisted ? "is-shortlisted" : ""} ${dir.status === "archived" ? "is-archived" : ""}" data-direction-id="${dir.id}">
      <div class="dir-card-top">
        ${collageHtml}
        <div style="min-width:0;display:flex;flex-direction:column;gap:6px;">
          <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:8px;">
            <div style="min-width:0;cursor:pointer;" data-open-dir-detail="${dir.id}" title="点击查看完整大模型建议与关联商品详情">
              <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
                <h3 style="margin:0;font-size:15px;font-weight:700;color:var(--ink-primary);line-height:1.3;">${escapeHtml(dir.name)}</h3>
                ${renderDirectionTypeBadge(dir)}
                ${isShortlisted ? `<span class="badge ${stMeta.cls}">${stMeta.label}</span>` : ""}
                ${renderDirectionAnomalyBadge(dir)}
              </div>
              <div style="font-size:12px;color:var(--ink-muted);margin-top:3px;">
                ${dir.source === "ai_suggested" ? "AI 归纳" : "人工创建"}${dir.target_audience ? ` · 受众：${escapeHtml(dir.target_audience)}` : ""}
              </div>
            </div>
            <span class="score-pill ${dir.direction_score >= 75 ? "is-high" : dir.direction_score >= 55 ? "is-mid" : "is-low"}"
              style="min-width:52px;font-size:15px;padding:4px 9px;cursor:help;"
              data-tip="${renderScoreBreakdownTooltip(dir)}" tabindex="0">
              ${formatDecimal(dir.direction_score, 1)}
            </span>
          </div>

          ${visibleTags.length
            ? `<div class="tag-row">
                ${visibleTags.map((t) => `<span class="badge badge-info">${escapeHtml(t)}</span>`).join("")}
                ${extraTagCount > 0 ? `<span class="badge badge-neutral">+${extraTagCount}</span>` : ""}
              </div>`
            : ""
          }

          ${dir.notes
            ? `<div class="dir-card-notes-preview" data-open-dir-detail="${dir.id}" title="点击查看完整分析与关联商品">
                <span>${escapeHtml(dir.notes)}</span>
              </div>`
            : ""
          }
        </div>
      </div>

      <div class="dir-kpi-strip" style="cursor:pointer;" data-open-dir-detail="${dir.id}" title="点击查看方向详情与全部关联商品">
        <div class="dir-kpi-cell" title="按 parent_asin 折叠去重后的独立父体数（关联 ${m.asin_count || 0} 个 ASIN · ${m.brand_count || 0} 个品牌）">
          <span class="dir-kpi-lbl">父体 / ASIN</span>
          <span class="dir-kpi-val" style="color:var(--accent-text);">${formatInt(m.parent_asin_count || 0)} <span style="font-weight:500;color:var(--ink-muted);font-size:12px;">/ ${formatInt(m.asin_count || 0)}</span></span>
        </div>
        <div class="dir-kpi-cell" title="覆盖类目：${escapeHtml(catNames.join('、') || '暂无')}">
          <span class="dir-kpi-lbl">覆盖类目 (${catNames.length})</span>
          <span class="dir-kpi-val">${escapeHtml(catSummary)}</span>
        </div>
        <div class="dir-kpi-cell" title="最高 30 天估算销量 / 中位数（严禁累加销量）">
          <span class="dir-kpi-lbl">最高/中位月销</span>
          <span class="dir-kpi-val">${salesStr}</span>
        </div>
        <div class="dir-kpi-cell" title="关联商品有效售价区间（USD）">
          <span class="dir-kpi-lbl">有效价格带</span>
          <span class="dir-kpi-val">${priceStr}</span>
        </div>
      </div>

      <button type="button" class="btn btn-primary btn-sm dir-quick-assign-btn"
        data-quick-assign-dir="${dir.id}"
        style="${selectedCount > 0 ? "" : "display:none;"}">
        + 归入此方向 (${selectedCount} 件)
      </button>

      <div class="dir-card-actions">
        <div style="display:flex;align-items:center;gap:6px;">
          <span style="font-size:12px;color:var(--ink-muted);">可行性</span>
          <div class="star-picker" role="group" aria-label="人工二创可行性评分">
            ${[1, 2, 3, 4, 5].map((star) => `
              <button type="button" class="star-btn ${star <= dir.feasibility_rating ? "is-on" : ""}"
                data-rate-dir="${dir.id}:${star}" title="评为 ${star} 星（微调 ${(star - 3) * 3 >= 0 ? "+" : ""}${(star - 3) * 3} 分）">★</button>
            `).join("")}
          </div>
        </div>

        <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
          <button type="button" class="btn ${isShortlisted ? "btn-ok-soft" : "btn-secondary"} btn-sm"
            data-toggle-shortlist="${dir.id}" data-next-status="${isShortlisted ? "draft" : "shortlisted"}">
            ${isShortlisted ? "✓ 已入选 Top 10" : "入选 Top 10"}
          </button>
          <button type="button" class="btn btn-secondary btn-sm" data-open-dir-detail="${dir.id}">
            详情 (${m.asin_count || 0})
          </button>
          <button type="button" class="btn btn-ghost btn-sm" data-edit-dir="${dir.id}">编辑</button>
          <button type="button" class="btn btn-ghost btn-sm" style="color:var(--danger);margin-left:4px;" data-delete-dir="${dir.id}" title="删除方向">删除</button>
        </div>
      </div>
    </article>
  `;
}

function renderStandaloneCard(item) {
  const ai = item.ai_analysis || null;
  const reasons = item.standalone_reasons || [];
  return `
    <article class="standalone-card" data-standalone-asin="${escapeHtml(item.asin)}">
      <div class="standalone-media" data-zoom-standalone="${escapeHtml(item.asin)}" title="点击放大查看高清印花主图">
        <span class="dir-score-chip" style="left:6px;right:auto;font-size:10.5px;">★ ${formatDecimal(item.total_score, 1)}</span>
        ${renderSafeImage(item.image_url, item.title || item.asin)}
      </div>
      <div class="standalone-info">
        <div style="display:flex;align-items:center;gap:5px;flex-wrap:wrap;">
          <span class="badge badge-standalone" style="font-size:10.5px;">孤品推荐</span>
          ${reasons.map((r) => `<span class="badge badge-info" style="font-size:10.5px;">${escapeHtml(r)}</span>`).join("")}
          ${renderCredibilityAnomalyBadges(item)}
        </div>
        <h4 class="standalone-title" title="${escapeHtml(item.title || "")}">
          ${escapeHtml(item.title || "（无标题）")}
        </h4>
        <div class="mono" style="font-size:11px;color:var(--ink-muted);">
          ${escapeHtml(item.asin)} · ${escapeHtml(item.brand || "未知品牌")} · <strong style="color:var(--accent);">${formatInt(item.listing_sales_30d)}/月</strong> · ${formatPrice(item.price, item.price_valid)}
        </div>
        ${ai && (ai.print_subject || (ai.theme_tags || []).length)
          ? `<div style="font-size:11.5px;color:var(--ink-secondary);line-height:1.4;">
              ${ai.print_subject ? `<span>✦ ${escapeHtml(ai.print_subject)}</span>` : ""}
              ${(ai.theme_tags || []).length
                ? ` <span class="tag-row" style="display:inline-flex;margin-left:4px;">${ai.theme_tags.slice(0, 3).map((t) => `<span class="badge badge-neutral" style="font-size:10px;">${escapeHtml(t)}</span>`).join("")}</span>`
                : ""
              }
            </div>`
          : ""
        }
        <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:4px;flex-wrap:wrap;">
          <div class="tag-row">
            ${(item.source_categories || []).slice(0, 2).map((s) => renderSourceCategoryBadge(s, { showBsr: false })).join("")}
          </div>
          <button type="button" class="btn btn-primary btn-sm" data-promote-standalone="${escapeHtml(item.asin)}">
            + 作为独立方向入选
          </button>
        </div>
      </div>
    </article>
  `;
}

export function renderDirectionsView(
  container,
  {
    candidatesData,
    directionsData,
    selectedAsins = [],
    rightTab = "directions",
    showStandaloneNotice = false,
    basisOpen = false,
    shortlistedOnly = false,
    detailModalId = null,
    aiTopN = 60,
    aiRunning = false,
    filters,
    toast,
    handlers,
  }
) {
  if (!candidatesData || !directionsData) {
    container.innerHTML = `<div class="loading-state"><div class="spinner"></div><p>正在加载 05 印花识别与方向归组工作台…</p></div>`;
    return;
  }

  // Capture current scroll positions before re-rendering so clicking buttons never jumps to top!
  const prevScroll = {
    windowY: window.scrollY || document.documentElement.scrollTop || 0,
    mainTop: document.getElementById("main-content")?.scrollTop || 0,
    leftPane: container.querySelector(".dir-pane-left")?.scrollTop || 0,
    rightPane: container.querySelector(".dir-pane-right")?.scrollTop || 0,
    cardList: container.querySelector("#dir-card-list")?.scrollTop || 0,
    standaloneList: container.querySelector("#dir-standalone-list")?.scrollTop || 0,
  };

  if (!candidatesData.ready) {
    container.innerHTML = `
      <div class="empty-state">
        <span class="badge badge-warn">暂无可用数据</span>
        <h2 style="margin:6px 0 0;font-size:17px;color:var(--ink-primary);">请先准备快照数据与预处理商品池</h2>
        <button type="button" class="btn btn-primary" id="btn-dir-goto-scoring">前往 04 候选评分</button>
      </div>
    `;
    document.getElementById("btn-dir-goto-scoring")?.addEventListener("click", () => handlers.onJumpToScoring());
    return;
  }

  const candItems = candidatesData.items || [];
  const standaloneItems = candidatesData.standalone_candidates || [];
  const counts = candidatesData.counts || {};
  const dirs = directionsData.items || [];
  const stCounts = directionsData.status_counts || {};
  const categories = candidatesData.categories || [];
  const presets = candidatesData.presets || [];
  const activeCfg = candidatesData.active_scoring_config || null;
  const weightShares = (activeCfg && activeCfg.weight_shares) || null;
  const selectedSet = new Set(selectedAsins || []);
  const selectedCount = selectedSet.size;
  const allVisibleSelected = candItems.length > 0 && candItems.every((i) => selectedSet.has(i.asin));
  const activeRightTab = rightTab === "standalone" ? "standalone" : "directions";
  const shortlistedCount = Number(stCounts.shortlisted || 0);
  const visibleDirs = shortlistedOnly ? dirs.filter((d) => d.status === "shortlisted") : dirs;

  const toastHtml = toast
    ? `<div class="prep-toast ${toast.type === "error" ? "alert-danger" : "alert-ok"}" id="dir-toast" role="status" aria-live="polite">
        <span>${escapeHtml(toast.message)}</span>
        <button type="button" class="btn btn-ghost btn-sm" id="btn-dir-toast-dismiss">关闭</button>
      </div>`
    : "";

  container.innerHTML = `
    <!-- 1. Compact Single-Row AI Grouping Bar -->
    <section class="panel" style="padding:12px 18px;" aria-label="AI 一键归纳操作栏">
      <div class="prep-header-bar">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
          <span class="badge badge-ai-source" title="支持一图多组（1~3 维度），绝不覆盖已有人工或已入选方向；未归组孤品自动进入独立爆款单品兜底">DeepSeek 视觉聚类</span>
          <span style="font-size:12.5px;color:var(--ink-secondary);">自动归组同题材印花，绝不覆盖人工已整理方向</span>
        </div>
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
          <select id="dir-ai-topn-select" class="select-control" style="height:31px;font-size:12px;" aria-label="AI 归纳样本范围">
            <option value="50" ${Number(aiTopN) === 50 ? "selected" : ""}>AI 范围：Top 50</option>
            <option value="60" ${Number(aiTopN) === 60 ? "selected" : ""}>AI 范围：Top 60</option>
            <option value="80" ${Number(aiTopN) === 80 ? "selected" : ""}>AI 范围：Top 80</option>
          </select>
          <button type="button" class="btn btn-ghost btn-sm" id="btn-dir-ai-config">AI 配置</button>
          <button type="button" class="btn btn-ai-accent btn-sm" id="btn-dir-ai-cluster" ${aiRunning ? "disabled" : ""}>
            ${aiRunning ? "AI 归纳分组中…" : "AI 一键归纳分组"}
          </button>
          <button type="button" class="btn btn-secondary btn-sm" id="btn-dir-goto-top10">
            前往 06 Top 10 →
          </button>
        </div>
      </div>
    </section>

    ${toastHtml}

    <div class="dir-split-layout">
      <!-- LEFT PANE (60%): Open-Air High-Potential Candidate Image Pool -->
      <section class="dir-pane-open dir-pane-left" aria-label="高潜力商品选图池">
        <div class="dir-pane-toolbar">
          <div class="dir-filter-row">
            <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
              <h2 class="panel-title">高潜力选图池</h2>
              <div class="segmented" role="tablist" aria-label="归组状态筛选">
                <button type="button" class="seg-btn ${filters.assigned === "unassigned" ? "is-active" : ""}" data-dir-assigned="unassigned">
                  未归组 (${formatInt(counts.unassigned_count || 0)})
                </button>
                <button type="button" class="seg-btn ${filters.assigned === "all" ? "is-active" : ""}" data-dir-assigned="all">
                  全部 (${formatInt(counts.top_n_count || 0)})
                </button>
                <button type="button" class="seg-btn ${filters.assigned === "assigned" ? "is-active" : ""}" data-dir-assigned="assigned">
                  已归组 (${formatInt(counts.assigned_count || 0)})
                </button>
              </div>
              ${activeCfg && weightShares
                ? `<button type="button" class="btn btn-ghost btn-sm" id="btn-dir-toggle-basis" aria-expanded="${basisOpen ? "true" : "false"}" aria-controls="dir-basis-drawer">
                    04: ${escapeHtml(activeCfg.preset_name || "默认方案")} ▾
                  </button>`
                : ""
              }
            </div>

            <label class="triage-select-all">
              <input type="checkbox" id="dir-select-all" ${allVisibleSelected ? "checked" : ""}>
              <span id="dir-select-all-lbl">${allVisibleSelected ? "取消全选" : "全选当前"}</span>
            </label>
          </div>

          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
            <input type="search" id="dir-search-input" class="input-control" style="height:30px;flex:1;min-width:150px;font-size:12px;"
              placeholder="搜标题 / ASIN…" value="${escapeHtml(filters.q || "")}">

            <select id="dir-cat-select" class="select-control" style="height:30px;font-size:12px;" aria-label="类目筛选">
              <option value="all" ${filters.category === "all" ? "selected" : ""}>全部类目</option>
              ${categories.filter((c) => c.is_configured).map((c) => `
                <option value="${escapeHtml(c.node_id)}" ${filters.category === c.node_id ? "selected" : ""}>${escapeHtml(c.short_name || c.name)}</option>
              `).join("")}
            </select>

            <select id="dir-topn-select" class="select-control" style="height:30px;font-size:12px;" aria-label="拉取评分前 N 名">
              <option value="50" ${Number(filters.top_n) === 50 ? "selected" : ""}>Top 50</option>
              <option value="80" ${Number(filters.top_n) === 80 ? "selected" : ""}>Top 80</option>
              <option value="120" ${Number(filters.top_n) === 120 ? "selected" : ""}>Top 120</option>
            </select>

            ${presets.length > 1
              ? `<select id="dir-preset-select" class="select-control" style="height:30px;font-size:12px;" aria-label="04 评分方案">
                  <option value="" ${!filters.preset_id ? "selected" : ""}>跟随 04 当前权重</option>
                  ${presets.map((p) => `
                    <option value="${escapeHtml(p.preset_id)}" ${filters.preset_id === p.preset_id ? "selected" : ""}>方案：${escapeHtml(p.name)}</option>
                  `).join("")}
                </select>`
              : ""
            }
          </div>

          ${activeCfg && weightShares
            ? `<div class="dir-basis-drawer" id="dir-basis-drawer" ${basisOpen ? "" : "hidden"}>
                <span class="mono">
                  销量 ${weightShares.sales}% · 机会 ${weightShares.opportunity}% · 星级 ${weightShares.rating}% · 新鲜度 ${weightShares.freshness}% · 价格 ${weightShares.price}%
                </span>
                <button type="button" class="btn btn-ghost btn-sm" id="btn-dir-adjust-weights" style="color:var(--accent-text);">
                  去 04 调整权重 →
                </button>
              </div>`
            : ""
          }
        </div>

        <!-- Floating Bottom Batch Grouping Bar (Visible when >= 1 selected) -->
        <div class="triage-batch-bar ${selectedCount > 0 ? "has-selection" : ""}" id="dir-batch-bar" aria-label="批量归组操作栏">
          <div class="triage-batch-left">
            <span class="badge badge-info" id="dir-selected-count">已选 ${selectedCount} 件</span>
            <button type="button" class="btn btn-ghost btn-sm" id="dir-clear-sel">清空</button>
          </div>
          <div class="triage-batch-right">
            <select id="dir-target-existing" class="select-control" style="height:30px;font-size:12px;max-width:170px;" ${dirs.length === 0 ? "disabled" : ""} aria-label="选择要归入的已有方向">
              ${dirs.length === 0
                ? `<option value="">暂无已有方向</option>`
                : dirs.map((d) => `<option value="${d.id}">${escapeHtml(d.name)} (${d.metrics?.asin_count || 0})</option>`).join("")
              }
            </select>
            <button type="button" class="btn btn-secondary btn-sm" id="btn-dir-assign-existing" ${(selectedCount === 0 || dirs.length === 0) ? "disabled" : ""}>
              归入已有方向
            </button>
            <button type="button" class="btn btn-primary btn-sm" id="btn-dir-create-from-selected" ${selectedCount === 0 ? "disabled" : ""}>
              + 基于所选新建方向
            </button>
          </div>
        </div>

        <!-- Candidate Image Grid -->
        ${candItems.length === 0
          ? `<div class="panel" style="text-align:center;padding:36px 20px;">
              <strong>${filters.assigned === "unassigned" ? "当前筛选下所有候选均已归组" : "当前筛选条件下无匹配商品"}</strong>
              <p style="margin:6px 0 0;font-size:13px;color:var(--ink-muted);">可切换至「全部」或扩大拉取范围（如 Top 120）。</p>
            </div>`
          : `<div class="dir-cand-grid" id="dir-cand-grid">
              ${candItems.map((item, idx) => {
                const checked = selectedSet.has(item.asin);
                const assignedDirs = item.assigned_directions || [];
                return `
                  <article class="dir-cand-card ${checked ? "is-selected" : ""}" data-cand-asin="${escapeHtml(item.asin)}" title="单击勾选 / 双击放大看印花细节">
                    <div class="dir-cand-media">
                      <label class="triage-check-wrap" title="勾选商品">
                        <input type="checkbox" class="triage-checkbox" data-cand-cb="${escapeHtml(item.asin)}" ${checked ? "checked" : ""}>
                      </label>
                      <span class="dir-score-chip" title="04 综合潜力分 #${item.rank}">★ ${formatDecimal(item.total_score, 1)}</span>
                      ${assignedDirs.length
                        ? `<div class="dir-assigned-chips">
                            ${assignedDirs.slice(0, 2).map((ad) => `<span class="dir-assigned-tag" title="已归入：${escapeHtml(ad.name)}">${escapeHtml(ad.name)}</span>`).join("")}
                          </div>`
                        : ""
                      }
                      <button type="button" class="dir-zoom-btn" data-zoom-asin="${escapeHtml(item.asin)}" title="放大高清核图" aria-label="放大预览">
                        <svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2">
                          <circle cx="9" cy="9" r="6"/><path d="M13.5 13.5L18 18M9 6.5v5M6.5 9h5" stroke-linecap="round"/>
                        </svg>
                      </button>
                      ${renderSafeImage(item.image_url, item.title || item.asin, "", idx < 12)}
                    </div>
                    <div class="dir-cand-body">
                      <div class="dir-cand-metrics">
                        <span style="font-size:12.5px;font-weight:700;color:var(--accent-text);">${formatInt(item.listing_sales_30d)}/月</span>
                        <span style="font-size:12px;font-weight:700;color:var(--ink-primary);">${formatPrice(item.price, item.price_valid)}</span>
                      </div>
                      <div class="tag-row">
                        ${renderCredibilityAnomalyBadges(item)}
                        ${(item.source_categories || []).slice(0, 2).map((s) => renderSourceCategoryBadge(s, { showBsr: false })).join("")}
                        ${item.collapsed_count > 0
                          ? `<span class="badge badge-both" title="已折叠同款变体：${escapeHtml((item.collapsed_sibling_asins || []).join(', '))}">合并${item.collapsed_count + 1}同款</span>`
                          : ""
                        }
                      </div>
                    </div>
                  </article>
                `;
              }).join("")}
            </div>`
        }
      </section>

      <!-- RIGHT PANE (40%): Open-Air Print Directions Management + Standalone Hits -->
      <section class="dir-pane-open dir-pane-right" aria-label="印花方向管理区">
        <div class="dir-pane-toolbar">
          <div class="dir-filter-row">
            <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
              <div class="segmented" role="tablist" aria-label="方向与独立爆款切换">
                <button type="button" class="seg-btn ${activeRightTab === "directions" ? "is-active" : ""}" data-dir-right-tab="directions">
                  印花方向 (${formatInt(dirs.length)})
                </button>
                <button type="button" class="seg-btn ${activeRightTab === "standalone" ? "is-active" : ""}" data-dir-right-tab="standalone" title="未进多商品趋势组，但 04 潜力分或月销 Top 20 的强单品">
                  独立爆款 (${formatInt(standaloneItems.length)})
                </button>
              </div>
              <button type="button" class="dir-shortlist-filter-btn ${shortlistedOnly ? "is-active" : ""} ${shortlistedCount > 10 ? "is-over" : ""}" id="btn-dir-filter-shortlisted" title="点击仅看已入选 Top 10 候选方向">
                Top 10 候选 ${formatInt(shortlistedCount)}/10${shortlistedCount > 10 ? " (超额)" : ""}
              </button>
            </div>
            <div style="display:flex;align-items:center;gap:6px;">
              ${activeRightTab === "directions"
                ? `<select id="dir-sort-select" class="select-control" style="height:30px;font-size:12px;" aria-label="方向排序">
                    <option value="direction_score" ${filters.sort === "direction_score" ? "selected" : ""}>按综合分</option>
                    <option value="asin_count" ${filters.sort === "asin_count" ? "selected" : ""}>按商品数</option>
                    <option value="created_at" ${filters.sort === "created_at" ? "selected" : ""}>按创建时间</option>
                  </select>`
                : ""
              }
              <button type="button" class="btn btn-primary btn-sm" id="btn-dir-new">+ 新建方向</button>
            </div>
          </div>
        </div>

        ${activeRightTab === "standalone"
          ? `<div style="display:flex;flex-direction:column;gap:10px;" id="dir-standalone-panel">
              ${standaloneItems.length === 0
                ? `<div class="panel" style="text-align:center;padding:36px 18px;">
                    <strong>当前无未归组的 Top 20 独立爆款单品</strong>
                    <p style="margin:6px 0 0;font-size:13px;color:var(--ink-muted);">Top 20 高分/高销商品均已归入趋势方向或已作为独立爆款入选。</p>
                  </div>`
                : `<div class="standalone-list" id="dir-standalone-list">
                    ${standaloneItems.map((it) => renderStandaloneCard(it)).join("")}
                  </div>`
              }
            </div>`
          : `${showStandaloneNotice && standaloneItems.length > 0
              ? `<div class="standalone-banner" id="dir-standalone-banner">
                  <span>发现 <strong>${formatInt(standaloneItems.length)}</strong> 件未归组 Top 20 孤品，适合作为独立爆款评估</span>
                  <div style="display:flex;align-items:center;gap:4px;">
                    <button type="button" class="btn btn-ghost btn-sm" data-dir-right-tab="standalone" style="color:var(--accent-text);font-weight:700;">查看独立爆款 →</button>
                    <button type="button" class="btn btn-ghost btn-sm" id="btn-dismiss-standalone-notice" aria-label="关闭提示">✕</button>
                  </div>
                </div>`
              : ""
            }
            ${visibleDirs.length === 0
              ? `<div class="panel" style="text-align:center;padding:36px 18px;">
                  <strong>${shortlistedOnly ? "暂无已入选 Top 10 的方向" : "尚未建立印花方向"}</strong>
                  <p style="margin:6px 0 0;font-size:13px;color:var(--ink-muted);">${shortlistedOnly ? "点击上方「Top 10 候选」按钮返回查看全部方向。" : "点击上方「AI 一键归纳分组」，或在左侧勾选同主题印花后新建方向。"}</p>
                </div>`
              : `<div class="dir-card-list" id="dir-card-list">
                  ${visibleDirs.map((d) => renderDirectionCard(d, selectedCount)).join("")}
                </div>`
            }`
        }
      </section>
    </div>
  `;

  // Restore scroll positions immediately and after layout paint so right-pane clicks never jump to top
  const restoreScroll = () => {
    const lp = container.querySelector(".dir-pane-left");
    const rp = container.querySelector(".dir-pane-right");
    const cl = container.querySelector("#dir-card-list");
    const sl = container.querySelector("#dir-standalone-list");
    const mc = document.getElementById("main-content");
    if (lp && prevScroll.leftPane) lp.scrollTop = prevScroll.leftPane;
    if (rp && prevScroll.rightPane) rp.scrollTop = prevScroll.rightPane;
    if (cl && prevScroll.cardList) cl.scrollTop = prevScroll.cardList;
    if (sl && prevScroll.standaloneList) sl.scrollTop = prevScroll.standaloneList;
    if (mc && prevScroll.mainTop) mc.scrollTop = prevScroll.mainTop;
    if (prevScroll.windowY) window.scrollTo(0, prevScroll.windowY);
  };
  restoreScroll();
  requestAnimationFrame(restoreScroll);

  bindChartInteractions(container);

  const candMap = new Map(candItems.map((i) => [i.asin, i]));
  const standaloneMap = new Map(standaloneItems.map((i) => [i.asin, i]));
  const dirMap = new Map(dirs.map((d) => [d.id, d]));

  // Keep open Direction Detail Modal live-synced after rating/shortlist/edit/unlink
  if (detailModalId) {
    const activeDir = dirMap.get(Number(detailModalId));
    if (activeDir) {
      openDirectionDetailModal(activeDir, handlers);
    } else {
      closeDirectionDetailModal();
      handlers.onCloseDirectionDetail?.();
    }
  }

  // Top AI Clustering & Config Controls
  document.getElementById("btn-dir-toggle-basis")?.addEventListener("click", () => {
    handlers.onToggleBasisDrawer?.();
  });
  document.getElementById("btn-dir-filter-shortlisted")?.addEventListener("click", () => {
    handlers.onToggleShortlistedOnly?.();
  });
  document.getElementById("btn-dir-goto-top10")?.addEventListener("click", () => {
    handlers.onJumpToTop10?.();
  });
  container.querySelectorAll("[data-quick-assign-dir]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const dirId = Number(btn.getAttribute("data-quick-assign-dir"));
      const asins = handlers.getSelectedAsins ? handlers.getSelectedAsins() : Array.from(selectedSet);
      if (dirId && asins.length) {
        handlers.onAddLinks(dirId, asins);
      }
    });
  });
  document.getElementById("btn-dir-adjust-weights")?.addEventListener("click", () => {
    handlers.onJumpToScoring?.();
  });
  document.getElementById("dir-ai-topn-select")?.addEventListener("change", (e) => {
    handlers.onChangeAiTopN?.(Number(e.target.value));
  });
  document.getElementById("btn-dir-ai-config")?.addEventListener("click", () => {
    handlers.onOpenAiConfig?.();
  });
  document.getElementById("btn-dir-ai-cluster")?.addEventListener("click", () => {
    handlers.onRunAiCluster?.();
  });

  // Right pane tabs (Directions vs Standalone Hits)
  container.querySelectorAll("[data-dir-right-tab]").forEach((btn) => {
    btn.addEventListener("click", () => {
      handlers.onSwitchRightTab?.(btn.getAttribute("data-dir-right-tab"));
    });
  });

  // Standalone Hits interactions
  container.querySelectorAll("[data-zoom-standalone]").forEach((el) => {
    el.addEventListener("click", () => {
      const item = standaloneMap.get(el.getAttribute("data-zoom-standalone"));
      if (item) openImageLightbox(item, {});
    });
  });
  container.querySelectorAll("[data-promote-standalone]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const asin = btn.getAttribute("data-promote-standalone");
      const item = standaloneMap.get(asin);
      if (!item) return;
      btn.disabled = true;
      btn.textContent = "入选处理中…";
      try {
        await handlers.onPromoteStandalone?.(item);
      } finally {
        btn.disabled = false;
      }
    });
  });

  // Toast dismiss
  document.getElementById("btn-dir-toast-dismiss")?.addEventListener("click", () => handlers.onDismissToast());

  // Left filters
  container.querySelectorAll("[data-dir-assigned]").forEach((btn) => {
    btn.addEventListener("click", () => {
      handlers.onFilterChange({ assigned: btn.getAttribute("data-dir-assigned") });
    });
  });
  document.getElementById("dir-topn-select")?.addEventListener("change", (e) => {
    handlers.onFilterChange({ top_n: Number(e.target.value) });
  });
  document.getElementById("dir-cat-select")?.addEventListener("change", (e) => {
    handlers.onFilterChange({ category: e.target.value });
  });
  document.getElementById("dir-preset-select")?.addEventListener("change", (e) => {
    handlers.onFilterChange({ preset_id: e.target.value });
  });
  let searchTimer = null;
  document.getElementById("dir-search-input")?.addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    const val = e.target.value;
    searchTimer = setTimeout(() => handlers.onFilterChange({ q: val }), 200);
  });

  // Left selection & batch grouping bar
  document.getElementById("dir-select-all")?.addEventListener("change", (e) => {
    handlers.onToggleSelectPage(candItems.map((i) => i.asin), e.target.checked);
  });
  document.getElementById("dir-clear-sel")?.addEventListener("click", () => handlers.onClearSelection());

  document.getElementById("btn-dir-assign-existing")?.addEventListener("click", () => {
    const dirId = Number(document.getElementById("dir-target-existing")?.value || 0);
    const asins = handlers.getSelectedAsins();
    if (dirId && asins.length) {
      handlers.onAddLinks(dirId, asins);
    }
  });

  document.getElementById("btn-dir-create-from-selected")?.addEventListener("click", () => {
    const asins = handlers.getSelectedAsins();
    openDirectionFormModal({
      mode: "create",
      initialAsins: asins,
      onSubmit: (payload) => handlers.onCreateDirection(payload),
    });
  });

  // Candidate card interactions: single-click select, double-click / zoom button -> HD Lightbox
  container.querySelectorAll("[data-cand-cb]").forEach((cb) => {
    cb.addEventListener("click", (e) => e.stopPropagation());
    cb.addEventListener("change", (e) => {
      e.stopPropagation();
      handlers.onToggleSelectAsin(cb.getAttribute("data-cand-cb"), cb.checked);
    });
  });
  container.querySelectorAll(".dir-cand-card .triage-check-wrap").forEach((lbl) => {
    lbl.addEventListener("click", (e) => e.stopPropagation());
  });
  container.querySelectorAll("[data-zoom-asin]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const asin = btn.getAttribute("data-zoom-asin");
      const prod = candMap.get(asin);
      if (!prod) return;
      openImageLightbox(prod, {
        isSelected: handlers.getSelectedAsins().includes(asin),
        onToggleSelect: (a, checked) => handlers.onToggleSelectAsin(a, checked),
      });
    });
  });
  container.querySelectorAll(".dir-cand-card[data-cand-asin]").forEach((card) => {
    const asin = card.getAttribute("data-cand-asin");
    card.addEventListener("click", (e) => {
      if (e.target.closest("button, a, input, label")) return;
      const cb = card.querySelector("[data-cand-cb]");
      handlers.onToggleSelectAsin(asin, cb ? !cb.checked : true);
    });
    card.addEventListener("dblclick", (e) => {
      if (e.target.closest("button, a, input, label")) return;
      e.preventDefault();
      const prod = candMap.get(asin);
      if (!prod) return;
      openImageLightbox(prod, {
        isSelected: handlers.getSelectedAsins().includes(asin),
        onToggleSelect: (a, checked) => handlers.onToggleSelectAsin(a, checked),
      });
    });
  });

  // Right pane: create, sort, stars, shortlist, edit, open detail popup, delete
  document.getElementById("btn-dismiss-standalone-notice")?.addEventListener("click", () => {
    handlers.onDismissStandaloneNotice?.();
  });
  document.getElementById("dir-sort-select")?.addEventListener("change", (e) => {
    handlers.onFilterChange({ sort: e.target.value });
  });
  document.getElementById("btn-dir-new")?.addEventListener("click", () => {
    openDirectionFormModal({
      mode: "create",
      initialAsins: handlers.getSelectedAsins(),
      onSubmit: (payload) => handlers.onCreateDirection(payload),
    });
  });

  container.querySelectorAll("[data-open-dir-detail]").forEach((el) => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      const dir = dirMap.get(Number(el.getAttribute("data-open-dir-detail")));
      if (!dir) return;
      handlers.onOpenDirectionDetail?.(dir.id);
      openDirectionDetailModal(dir, handlers);
    });
  });

  container.querySelectorAll("[data-rate-dir]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const [idStr, starStr] = btn.getAttribute("data-rate-dir").split(":");
      handlers.onUpdateDirection(Number(idStr), { feasibility_rating: Number(starStr) });
    });
  });

  container.querySelectorAll("[data-toggle-shortlist]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = Number(btn.getAttribute("data-toggle-shortlist"));
      const nextStatus = btn.getAttribute("data-next-status");
      handlers.onUpdateDirection(id, { status: nextStatus });
    });
  });

  container.querySelectorAll("[data-edit-dir]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const dir = dirMap.get(Number(btn.getAttribute("data-edit-dir")));
      if (!dir) return;
      openDirectionFormModal({
        mode: "edit",
        direction: dir,
        onSubmit: (payload) => handlers.onUpdateDirection(dir.id, payload),
      });
    });
  });

  container.querySelectorAll("[data-delete-dir]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const dir = dirMap.get(Number(btn.getAttribute("data-delete-dir")));
      if (!dir) return;
      openDeleteDirectionModal(dir, (id) => handlers.onDeleteDirection(id));
    });
  });
}

export function syncDirectionSelectionUi(selectedAsins, hasDirections) {
  const set = new Set(selectedAsins || []);
  const count = set.size;
  const bar = document.getElementById("dir-batch-bar");
  const badge = document.getElementById("dir-selected-count");
  const clearBtn = document.getElementById("dir-clear-sel");
  const selectAllCb = document.getElementById("dir-select-all");
  const selectAllLbl = document.getElementById("dir-select-all-lbl");
  const assignBtn = document.getElementById("btn-dir-assign-existing");
  const createBtn = document.getElementById("btn-dir-create-from-selected");

  if (bar) bar.classList.toggle("has-selection", count > 0);
  if (badge) {
    badge.textContent = `已选 ${count} 件`;
    badge.className = `badge ${count > 0 ? "badge-info" : "badge-neutral"}`;
  }
  if (clearBtn) clearBtn.style.display = count > 0 ? "" : "none";
  if (assignBtn) assignBtn.disabled = count === 0 || !hasDirections;
  if (createBtn) createBtn.disabled = count === 0;

  const cbs = Array.from(document.querySelectorAll("#dir-cand-grid [data-cand-cb]"));
  cbs.forEach((cb) => {
    const asin = cb.getAttribute("data-cand-cb");
    const checked = set.has(asin);
    cb.checked = checked;
    const card = cb.closest(".dir-cand-card");
    if (card) card.classList.toggle("is-selected", checked);
  });

  const allChecked = cbs.length > 0 && cbs.every((cb) => cb.checked);
  if (selectAllCb) selectAllCb.checked = allChecked;
  if (selectAllLbl) selectAllLbl.textContent = allChecked ? "取消全选" : "全选当前";

  // Live-update direct "+ 归入此方向 (N)" drop-in buttons on every right-pane direction card!
  document.querySelectorAll("[data-quick-assign-dir]").forEach((btn) => {
    btn.style.display = count > 0 ? "" : "none";
    btn.textContent = `+ 归入此方向 (${count} 件)`;
  });
}
