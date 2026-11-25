/**
 * Stage 02: Data Preprocessing View — Streamlined Visual Triage & Safe Batch Review.
 *
 * Strictly preserves Plan A:
 * - Automatic rules & AI vision ONLY tag and prioritize sorting.
 * - Automatic rules NEVER auto-exclude products; all exclusions require human confirmation.
 * - Supports Gallery Triage (default) + List Table toggle, interactive KPI queue switching,
 *   floating multi-select batch bar with exclude confirmation, and Brand / Parent ASIN dry-run batch exclude.
 */

import { api } from "../api.js";
import {
  escapeHtml,
  formatBeijingTime,
  formatInt,
  formatPrice,
  formatRatingHtml,
  renderAiV2Tags,
  renderCredibilityAnomalyBadges,
  renderSafeImage,
  renderSourceCategoryBadge,
} from "../format.js";

const ICON_SPARKLE = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/></svg>`;
const ICON_ARROW_RIGHT = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14"/><path d="m12 5 7 7-7 7"/></svg>`;
const ICON_SLIDERS = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/></svg>`;

const REVIEW_STATUS_LABELS = {
  unreviewed: "未审核",
  confirmed_keep: "已确认保留",
  confirmed_exclude: "已确认排除",
};

let activeDrawerKeyHandler = null;

function detachDrawerKeyHandler() {
  if (activeDrawerKeyHandler) {
    document.removeEventListener("keydown", activeDrawerKeyHandler);
    activeDrawerKeyHandler = null;
  }
}

function makeRequestId(prefix) {
  return `${prefix}_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

function formatStatusLabel(status) {
  return REVIEW_STATUS_LABELS[status] || status || "—";
}

function renderAutoBadge(item) {
  const map = {
    possible_print: ["badge-info", "有印花线索"],
    suspected_non_print: ["badge-warn", "疑似非印花"],
    conflicting: ["badge-both", "规则冲突"],
    uncertain: ["badge-neutral", "信息不足"],
  };
  const [cls, label] = map[item.automatic_classification] || ["badge-neutral", item.automatic_classification_label];
  const tip = (item.automatic_reasons || []).map((r) => `${r.field}：${r.label}`).join("；");
  return `<span class="badge ${cls}" title="${escapeHtml(tip)}">${label}</span>`;
}

function renderAiVisionBadge(item) {
  const ai = item && item.ai_analysis;
  if (!ai) return "";
  const pct = Math.round((Number(ai.confidence) || 0) * 100);
  const label = ai.pattern_label || ai.pattern_type || "已分析";
  let cls = "ai-pill-neutral";
  if (ai.suggest_exclude) {
    cls = "ai-pill-exclude";
  } else if (ai.is_graphic_print) {
    cls = "ai-pill-print";
  } else {
    cls = "ai-pill-warn";
  }
  const tipParts = [
    `AI 视觉判定：${label}（置信度 ${pct}% · ${ai.model || "deepseek-flash"}）`,
    ai.filter_reason ? `理由：${ai.filter_reason}` : "",
    ai.print_subject ? `印花主体：${ai.print_subject}` : "",
    ai.slogan_text ? `衣服文案：${ai.slogan_text}` : "",
  ].filter(Boolean);
  return `<span class="badge ai-vision-pill ${cls}" title="${escapeHtml(tipParts.join("\n"))}">AI: ${escapeHtml(label)} ${pct}%</span>`;
}

function renderManualBadge(item) {
  const map = {
    unreviewed: ["badge-neutral", "未审核"],
    confirmed_keep: ["badge-ok", "已确认保留"],
    confirmed_exclude: ["badge-danger", "已确认排除"],
  };
  const [cls, label] = map[item.manual_review_status] || ["badge-neutral", item.manual_review_status_label];
  const tip = item.manual_review_reason
    ? `${item.manual_review_reason}${item.manual_review_note ? ` · ${item.manual_review_note}` : ""}`
    : "尚未进行人工确认";
  const staleHtml = item.review_stale
    ? ` <span class="badge badge-warn" title="商品关键信息已变化，原人工结论需复核">需复核</span>`
    : "";
  return `<span class="badge ${cls}" title="${escapeHtml(tip)}">${label}</span>${staleHtml}`;
}

function renderEligibilityBadge(item) {
  const map = {
    ready: ["badge-ok", "可评分"],
    needs_review: ["badge-warn", "待核实"],
    excluded: ["badge-danger", "已排除"],
    blocked: ["badge-danger", "阻断"],
  };
  const [cls, label] = map[item.eligibility] || ["badge-neutral", item.eligibility_label];
  return `<span class="badge ${cls}" title="${escapeHtml(item.eligibility_reason || "")}">${label}</span>`;
}

function renderQualityBadges(item) {
  const credHtml = renderCredibilityAnomalyBadges(item);
  const list = (item.data_quality_issue_labels || []).filter(
    (iss) => iss.code !== "price_anomaly" && iss.code !== "sales_suspect"
  );
  if (!credHtml && !list.length) {
    return `<span class="badge badge-ok">完备</span>`;
  }
  const show = list.slice(0, 2);
  const rest = list.length - show.length;
  const html = show
    .map((iss) => {
      const cls = ["invalid_asin", "title_missing", "sales_missing"].includes(iss.code)
        ? "badge-danger"
        : ["price_zero", "parent_asin_same"].includes(iss.code)
          ? "badge-warn"
          : "badge-neutral";
      return `<span class="badge ${cls}">${escapeHtml(iss.label)}</span>`;
    })
    .join(" ");
  const more = rest > 0
    ? ` <span class="badge badge-neutral" title="${escapeHtml(list.slice(2).map((x) => x.label).join("、"))}">+${rest}</span>`
    : "";
  return [credHtml, html + more].filter(Boolean).join(" ");
}

export function closeReviewModal(force = false) {
  const overlay = document.getElementById("review-modal-overlay");
  if (!overlay) return;
  if (!force && overlay.dataset.locked === "true") return;
  overlay.dataset.locked = "false";
  overlay.classList.remove("is-open");
  overlay.setAttribute("aria-hidden", "true");
}

function openExcludeModal(item, excludeReasons, onSubmit) {
  const overlay = document.getElementById("review-modal-overlay");
  const body = document.getElementById("review-modal-body");
  const titleEl = document.getElementById("review-modal-title");
  const eyebrowEl = document.getElementById("review-modal-eyebrow");
  if (!overlay || !body) return;

  if (eyebrowEl) eyebrowEl.textContent = "人工确认";
  titleEl.textContent = "确认排除商品";
  const reasonEntries = Object.entries(excludeReasons || {});

  body.innerHTML = `
    <div style="display:flex;gap:12px;align-items:center;padding:10px 12px;background:var(--bg-elevated);border-radius:var(--radius-sm);">
      <div class="thumb-box" style="width:48px;height:48px;">${renderSafeImage(item.image_url, item.asin, "", true)}</div>
      <div style="min-width:0;flex:1;">
        <div style="font-weight:700;font-size:13px;line-height:1.35;">${escapeHtml(item.title || "（无标题）")}</div>
        <div class="mono" style="font-size:12px;color:var(--ink-muted);margin-top:2px;">${escapeHtml(item.asin)} · 自动判断：${escapeHtml(item.automatic_classification_label)}</div>
      </div>
    </div>

    <div class="field">
      <label class="field-label" for="modal-exclude-reason">排除原因</label>
      <select id="modal-exclude-reason" class="select-control">
        ${reasonEntries.map(([code, label]) => `<option value="${escapeHtml(code)}">${escapeHtml(label)}</option>`).join("")}
      </select>
    </div>

    <div class="field">
      <label class="field-label" for="modal-exclude-note">核验备注（选填）</label>
      <textarea id="modal-exclude-note" class="textarea-control" placeholder="例如：主图确认纯色基础款，无印花或文案设计"></textarea>
    </div>

    <div style="font-size:12px;color:var(--ink-muted);">
      原始数据不会被删除；确认排除后仅移出下一阶段评分输入，随时可点击「恢复」撤销。
    </div>

    <div style="display:flex;justify-content:flex-end;gap:8px;">
      <button type="button" class="btn btn-secondary" id="modal-cancel-btn">取消</button>
      <button type="button" class="btn btn-primary" style="background:var(--danger);" id="modal-confirm-exclude-btn">确认排除</button>
    </div>
  `;

  overlay.classList.add("is-open");
  overlay.setAttribute("aria-hidden", "false");
  document.getElementById("modal-exclude-reason")?.focus();

  document.getElementById("modal-cancel-btn")?.addEventListener("click", () => closeReviewModal());
  document.getElementById("modal-confirm-exclude-btn")?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    const reasonCode = document.getElementById("modal-exclude-reason")?.value || "other";
    const reasonNote = document.getElementById("modal-exclude-note")?.value || "";
    try {
      await onSubmit({
        asin: item.asin,
        action: "confirm_exclude",
        reason_code: reasonCode,
        reason_note: reasonNote,
        request_id: makeRequestId("excl"),
      });
      closeReviewModal();
    } finally {
      btn.disabled = false;
    }
  });
}

function openMultiBatchExcludeModal({ asins, defaultReason, defaultNote, excludeReasons, onConfirm }) {
  const overlay = document.getElementById("review-modal-overlay");
  const body = document.getElementById("review-modal-body");
  const titleEl = document.getElementById("review-modal-title");
  const eyebrowEl = document.getElementById("review-modal-eyebrow");
  if (!overlay || !body || !asins.length) return;

  if (eyebrowEl) eyebrowEl.textContent = "批量人工确认";
  titleEl.textContent = `确认批量排除 ${formatInt(asins.length)} 件商品`;
  const reasonEntries = Object.entries(excludeReasons || {});
  let chosenReason = defaultReason || (reasonEntries[0] ? reasonEntries[0][0] : "solid_non_print");

  body.innerHTML = `
    <div style="font-size:13px;color:var(--ink-secondary);line-height:1.5;">
      已勾选 <strong>${formatInt(asins.length)}</strong> 件商品，确认后将统一移出 04 候选评分池；操作后可通过顶部提示条一键撤销。
    </div>

    <div class="field">
      <span class="field-label">快捷选择排除原因</span>
      <div class="reason-chip-group" id="modal-reason-chips">
        ${reasonEntries.map(([code, label]) => `
          <button type="button" class="reason-chip ${code === chosenReason ? "is-active" : ""}" data-reason-code="${escapeHtml(code)}">
            ${escapeHtml(label)}
          </button>
        `).join("")}
      </div>
    </div>

    <div class="field">
      <label class="field-label" for="modal-multi-batch-note">批量备注（选填）</label>
      <input type="text" id="modal-multi-batch-note" class="input-control" value="${escapeHtml(defaultNote || "")}" placeholder="填写本次批量核验备注…">
    </div>

    <div style="display:flex;justify-content:flex-end;gap:8px;">
      <button type="button" class="btn btn-secondary" id="modal-cancel-btn">取消</button>
      <button type="button" class="btn btn-primary" style="background:var(--danger);" id="modal-confirm-multi-batch-btn">
        确认排除 ${formatInt(asins.length)} 件
      </button>
    </div>
  `;

  overlay.classList.add("is-open");
  overlay.setAttribute("aria-hidden", "false");

  body.querySelectorAll("[data-reason-code]").forEach((chip) => {
    chip.addEventListener("click", () => {
      chosenReason = chip.getAttribute("data-reason-code") || "solid_non_print";
      body.querySelectorAll("[data-reason-code]").forEach((c) => c.classList.toggle("is-active", c === chip));
    });
  });

  const confirmBtn = document.getElementById("modal-confirm-multi-batch-btn");
  confirmBtn?.focus();
  document.getElementById("modal-cancel-btn")?.addEventListener("click", () => closeReviewModal());

  const submitNow = async () => {
    if (!confirmBtn || confirmBtn.disabled) return;
    confirmBtn.disabled = true;
    const note = document.getElementById("modal-multi-batch-note")?.value || "";
    try {
      await onConfirm({ reason_code: chosenReason, reason_note: note });
      closeReviewModal();
    } finally {
      confirmBtn.disabled = false;
    }
  };

  confirmBtn?.addEventListener("click", submitNow);
  document.getElementById("modal-multi-batch-note")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      submitNow();
    }
  });
}

async function openGroupBatchExcludeModal({
  groupType,
  groupValue,
  estimatedCount,
  observedDate,
  excludeReasons,
  onConfirmBatch,
}) {
  const overlay = document.getElementById("review-modal-overlay");
  const body = document.getElementById("review-modal-body");
  const titleEl = document.getElementById("review-modal-title");
  const eyebrowEl = document.getElementById("review-modal-eyebrow");
  if (!overlay || !body) return;

  const isBrand = groupType === "brand";
  if (eyebrowEl) eyebrowEl.textContent = "按组批量排除";
  titleEl.textContent = isBrand ? "一键排除同品牌全部商品" : "一键排除同父体全部商品";
  const reasonEntries = Object.entries(excludeReasons || {});

  body.innerHTML = `<div class="loading-state" style="padding:20px;"><div class="spinner"></div><p>正在统计影响范围…</p></div>`;
  overlay.classList.add("is-open");
  overlay.setAttribute("aria-hidden", "false");

  let matchedCount = estimatedCount || 0;
  let sampleTitles = [];
  try {
    const preview = await api.submitPreprocessingBatchReview({
      action: "confirm_exclude",
      [isBrand ? "brand" : "parent_asin"]: groupValue,
      observed_date: observedDate,
      dry_run: true,
    });
    matchedCount = preview.matched_count || 0;
    sampleTitles = preview.sample_titles || [];
  } catch (err) {
    body.innerHTML = `
      <div class="alert alert-danger">统计受影响商品失败：${escapeHtml(err.message)}</div>
      <div style="display:flex;justify-content:flex-end;">
        <button type="button" class="btn btn-secondary" id="modal-cancel-btn">关闭</button>
      </div>
    `;
    document.getElementById("modal-cancel-btn")?.addEventListener("click", () => closeReviewModal());
    return;
  }

  body.innerHTML = `
    <div class="alert alert-warn" style="margin:0;">
      <strong>确认批量排除 ${formatInt(matchedCount)} 件商品？</strong>
      <div style="margin-top:4px;font-size:12px;">
        ${isBrand ? "品牌" : "父体 Parent ASIN"}：<span class="mono" style="font-weight:700;">${escapeHtml(groupValue)}</span>
        （当前快照共匹配 <strong>${formatInt(matchedCount)}</strong> 件，将统一标记为「已确认排除」，随时可撤销恢复）。
      </div>
    </div>

    ${sampleTitles.length
      ? `<div style="padding:8px 10px;border-radius:8px;background:var(--bg-elevated);font-size:12px;color:var(--ink-secondary);">
          <div style="font-weight:600;margin-bottom:4px;color:var(--ink-primary);">受影响商品示例：</div>
          <ul style="margin:0;padding-left:16px;display:flex;flex-direction:column;gap:3px;">
            ${sampleTitles.map((t) => `<li>${escapeHtml((t || "（无标题）").slice(0, 72))}</li>`).join("")}
          </ul>
        </div>`
      : ""
    }

    <div class="field">
      <label class="field-label" for="modal-batch-reason">统一排除原因</label>
      <select id="modal-batch-reason" class="select-control">
        ${reasonEntries.map(([code, label]) => `<option value="${escapeHtml(code)}">${escapeHtml(label)}</option>`).join("")}
      </select>
    </div>

    <div class="field">
      <label class="field-label" for="modal-batch-note">批量备注（选填）</label>
      <input type="text" id="modal-batch-note" class="input-control"
        value="${escapeHtml(isBrand ? `按品牌 ${groupValue} 批量排除` : `按父体 ${groupValue} 批量排除`)}"
        placeholder="填写批量操作原因…">
    </div>

    <div style="display:flex;justify-content:flex-end;gap:8px;">
      <button type="button" class="btn btn-secondary" id="modal-cancel-btn">取消</button>
      <button type="button" class="btn btn-primary" style="background:var(--danger);" id="modal-confirm-batch-btn">
        确认排除全部 ${formatInt(matchedCount)} 件
      </button>
    </div>
  `;

  document.getElementById("modal-cancel-btn")?.addEventListener("click", () => closeReviewModal());
  document.getElementById("modal-confirm-batch-btn")?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    const reasonCode = document.getElementById("modal-batch-reason")?.value || "solid_non_print";
    const reasonNote = document.getElementById("modal-batch-note")?.value || "";
    try {
      await onConfirmBatch({
        action: "confirm_exclude",
        [isBrand ? "brand" : "parent_asin"]: groupValue,
        reason_code: reasonCode,
        reason_note: reasonNote,
        operator_source: isBrand ? "workbench_batch_brand" : "workbench_batch_parent",
        request_id: makeRequestId(isBrand ? "bbrand" : "bparent"),
      });
      closeReviewModal();
    } finally {
      btn.disabled = false;
    }
  });
}

export async function openPreprocessingDrawer(
  asin,
  observedDate,
  { excludeReasons, keepReasons, pageAsins = [], onNavigateAsin, onReviewDone, onBatchReview }
) {
  const drawer = document.getElementById("detail-drawer");
  const overlay = document.getElementById("drawer-overlay");
  const body = document.getElementById("drawer-body");
  const titleEl = document.getElementById("drawer-title");
  const eyebrowEl = document.getElementById("drawer-eyebrow");
  if (!drawer || !overlay || !body) return;

  detachDrawerKeyHandler();
  eyebrowEl.textContent = `商品核验 · ${asin}`;
  titleEl.textContent = `加载 ${asin}…`;
  body.innerHTML = `<div class="loading-state"><div class="spinner"></div><p>正在加载商品核验详情…</p></div>`;

  drawer.classList.add("is-open");
  overlay.classList.add("is-open");
  drawer.setAttribute("aria-hidden", "false");
  overlay.setAttribute("aria-hidden", "false");
  document.getElementById("drawer-close")?.focus();

  try {
    const res = await api.getPreprocessingProductDetail(asin, observedDate);
    const p = res.item;
    titleEl.textContent = p.asin;
    renderPreprocessingDrawerBody(body, p, {
      observedDate,
      excludeReasons,
      keepReasons,
      pageAsins,
      onNavigateAsin,
      onReviewDone,
      onBatchReview,
    });
  } catch (err) {
    body.innerHTML = `<div class="alert alert-danger">加载详情失败：${escapeHtml(err.message)}</div>`;
  }
}

function renderPreprocessingDrawerBody(
  body,
  p,
  { observedDate, excludeReasons, keepReasons, pageAsins = [], onNavigateAsin, onReviewDone, onBatchReview }
) {
  detachDrawerKeyHandler();
  const photos = p.photos && p.photos.length ? p.photos : (p.image_url ? [p.image_url] : []);
  const activeImg = photos[0] || "";
  const currentIdx = pageAsins.indexOf(p.asin);
  const prevAsin = currentIdx > 0 ? pageAsins[currentIdx - 1] : null;
  const nextAsin = currentIdx >= 0 && currentIdx < pageAsins.length - 1 ? pageAsins[currentIdx + 1] : null;

  const thumbsHtml = photos.length > 1
    ? `<div class="drawer-thumbs">
        ${photos.slice(0, 8).map((url, idx) => `
          <button type="button" class="drawer-thumb-btn ${idx === 0 ? "is-active" : ""}" data-thumb-url="${escapeHtml(url)}" aria-label="图 ${idx + 1}">
            <img src="${escapeHtml(url)}" alt="图 ${idx + 1}" loading="lazy" referrerpolicy="no-referrer" onerror="this.style.opacity='0.25'">
          </button>
        `).join("")}
      </div>`
    : "";

  const sourcesHtml = (p.source_categories || []).map((s) => `
    <tr>
      <td>${renderSourceCategoryBadge(s, { useFullName: true })}</td>
      <td class="mono">${escapeHtml(s.node_id)}</td>
      <td class="num-cell">#${formatInt(s.result_position)}</td>
      <td class="num-cell">${s.source_category_bsr !== null ? `#${formatInt(s.source_category_bsr)}` : '<span class="badge badge-neutral">缺失</span>'}</td>
    </tr>
  `).join("");

  const reasonsHtml = (p.automatic_reasons || []).map((r) => `
    <div style="padding:8px 10px;border-radius:8px;background:var(--bg-elevated);font-size:12px;">
      <div style="font-weight:600;color:var(--ink-primary);">${escapeHtml(r.label)} <span class="badge badge-neutral" style="margin-left:4px;">${escapeHtml(r.field)}</span></div>
      ${r.quote ? `<div class="mono" style="font-size:12px;color:var(--ink-muted);margin-top:3px;">命中原文：“${escapeHtml(r.quote)}”</div>` : ""}
    </div>
  `).join("");

  const historyHtml = (p.review_history || []).length
    ? `<div class="timeline-list">
        ${p.review_history.map((h) => `
          <div class="timeline-item">
            <div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;">
              <strong>${h.action === "confirm_exclude" ? "人工确认排除" : h.action === "confirm_keep" ? "人工确认保留" : "撤销并恢复"}</strong>
              <span style="color:var(--ink-muted);">${formatBeijingTime(h.created_at)}</span>
            </div>
            <div style="color:var(--ink-secondary);margin-top:2px;">
              状态：${escapeHtml(formatStatusLabel(h.prev_status))} → ${escapeHtml(formatStatusLabel(h.new_status))}
              ${h.reason_label ? ` · 原因：${escapeHtml(h.reason_label)}` : ""}
              ${h.reason_note ? ` · 备注：${escapeHtml(h.reason_note)}` : ""}
            </div>
          </div>
        `).join("")}
      </div>`
    : `<div style="font-size:12px;color:var(--ink-muted);">暂无人工审核历史记录。</div>`;

  body.innerHTML = `
    <div class="drawer-gallery-main" id="drawer-main-photo">
      ${renderSafeImage(activeImg, p.title || p.asin, "", true)}
    </div>
    ${thumbsHtml}

    <div>
      <div class="tag-row" style="margin-bottom:8px;">
        ${renderAutoBadge(p)}
        ${renderAiVisionBadge(p)}
        ${renderManualBadge(p)}
        ${renderEligibilityBadge(p)}
        ${renderCredibilityAnomalyBadges(p, { compact: false })}
      </div>
      <h3 style="margin:0 0 6px;font-size:14.5px;line-height:1.45;">${escapeHtml(p.title || "（无标题）")}</h3>
      <div style="font-size:12px;color:var(--ink-muted);display:flex;gap:10px;flex-wrap:wrap;align-items:center;">
        <span class="mono">ASIN: ${escapeHtml(p.asin)}</span>
        <span class="mono">Parent: ${escapeHtml(p.parent_asin || "缺失")}</span>
        <span>品牌: <strong>${escapeHtml(p.brand || "未知")}</strong></span>
        ${(p.amazon_url || p.asin)
          ? `<a href="${escapeHtml(p.amazon_url || `https://www.amazon.com/dp/${encodeURIComponent(p.asin)}`)}" target="_blank" rel="noopener noreferrer">打开 Amazon 前台 ↗</a>`
          : ""
        }
      </div>
    </div>

    <div class="kv-grid">
      <div class="kv-box">
        <div class="kv-label">30 天估算销量</div>
        <div class="kv-val">${formatInt(p.listing_sales_30d)}</div>
      </div>
      <div class="kv-box">
        <div class="kv-label">售价</div>
        <div class="kv-val">${formatPrice(p.raw_price_usd, p.price_valid)}</div>
      </div>
      <div class="kv-box">
        <div class="kv-label">星级 / 评分数</div>
        <div class="kv-val">${formatRatingHtml(p.rating_stars, p.ratings_count, p.rating_stars !== null ? "valid" : "missing")}</div>
      </div>
      <div class="kv-box">
        <div class="kv-label">上架日期</div>
        <div class="kv-val" style="font-size:12.5px;">${escapeHtml(p.online_date || "—")}</div>
      </div>
    </div>

    <!-- Human Review Controls inside Drawer -->
    <div style="padding:14px;border-radius:var(--radius-md);background:var(--bg-elevated);border:1px solid var(--border);display:flex;flex-direction:column;gap:10px;">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;">
        <strong style="font-size:13px;">人工审核操作（优先于自动规则）</strong>
        ${(prevAsin || nextAsin)
          ? `<div style="display:flex;gap:6px;">
              <button type="button" class="btn btn-ghost btn-sm" id="drawer-btn-prev" ${prevAsin ? "" : "disabled"}>← 上一件</button>
              <button type="button" class="btn btn-ghost btn-sm" id="drawer-btn-next" ${nextAsin ? "" : "disabled"}>下一件 →</button>
            </div>`
          : ""
        }
      </div>
      <div class="form-grid">
        <label class="field">
          <span class="field-label">排除原因</span>
          <select id="drawer-exclude-reason" class="select-control">
            ${Object.entries(excludeReasons || {}).map(([k, v]) => `<option value="${escapeHtml(k)}" ${p.manual_review_reason_code === k ? "selected" : ""}>${escapeHtml(v)}</option>`).join("")}
          </select>
        </label>
        <label class="field">
          <span class="field-label">保留依据</span>
          <select id="drawer-keep-reason" class="select-control">
            ${Object.entries(keepReasons || {}).map(([k, v]) => `<option value="${escapeHtml(k)}" ${p.manual_review_reason_code === k ? "selected" : ""}>${escapeHtml(v)}</option>`).join("")}
          </select>
        </label>
      </div>
      <label class="field">
        <span class="field-label">审核备注（选填）</span>
        <input type="text" id="drawer-review-note" class="input-control" value="${escapeHtml(p.manual_review_note || "")}" placeholder="填写核图结论或补充说明…">
      </label>
      <div style="display:flex;gap:8px;flex-wrap:wrap;">
        <button type="button" class="btn btn-ok-soft" id="drawer-btn-keep">确认保留</button>
        <button type="button" class="btn btn-danger-soft" id="drawer-btn-exclude">确认排除</button>
        ${p.manual_review_status !== "unreviewed"
          ? `<button type="button" class="btn btn-secondary" id="drawer-btn-restore">恢复至自动状态</button>`
          : ""
        }
      </div>
      <div class="drawer-kbd-hint">
        <span>快捷键：</span>
        <span><kbd>K</kbd> 保留</span>
        <span><kbd>E</kbd> 排除</span>
        <span><kbd>←</kbd><kbd>→</kbd> 切换商品</span>
        <span><kbd>Esc</kbd> 关闭</span>
      </div>
      ${((p.brand && (p.brand_count || 0) >= 2) || (p.parent_asin && (p.parent_count || 0) >= 2))
        ? `<div style="display:flex;gap:8px;flex-wrap:wrap;padding-top:8px;border-top:1px dashed var(--border);">
            ${p.brand && (p.brand_count || 0) >= 2
              ? `<button type="button" class="btn btn-ghost btn-sm" id="drawer-btn-exclude-brand">排除同品牌全部（${escapeHtml(p.brand)} · ${formatInt(p.brand_count)}件）</button>`
              : ""
            }
            ${p.parent_asin && (p.parent_count || 0) >= 2
              ? `<button type="button" class="btn btn-ghost btn-sm" id="drawer-btn-exclude-parent">排除同父体全部（${escapeHtml(p.parent_asin)} · ${formatInt(p.parent_count)}件）</button>`
              : ""
            }
          </div>`
        : ""
      }
    </div>

    ${p.ai_analysis
      ? `<div style="padding:12px 14px;border-radius:var(--radius-md);background:${p.ai_analysis.suggest_exclude ? "var(--danger-soft)" : "var(--info-soft)"};border:1px solid ${p.ai_analysis.suggest_exclude ? "var(--danger-border)" : "var(--info-border)"};display:flex;flex-direction:column;gap:6px;">
          <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;">
            <strong style="font-size:13px;">AI 视觉看图结论（仅建议，不自动排除）</strong>
            <span class="mono" style="font-size:12px;color:var(--ink-muted);">置信度 ${Math.round((p.ai_analysis.confidence || 0) * 100)}%</span>
          </div>
          <div style="font-size:12.5px;color:var(--ink-primary);">
            判定：<strong>${escapeHtml(p.ai_analysis.pattern_label || p.ai_analysis.pattern_type)}</strong>
            ${p.ai_analysis.filter_reason ? ` — ${escapeHtml(p.ai_analysis.filter_reason)}` : ""}
          </div>
          ${renderAiV2Tags(p.ai_analysis, { compact: false })
            ? `<div class="tag-row" data-ai-v2-row="drawer">${renderAiV2Tags(p.ai_analysis, { compact: false })}</div>`
            : ""
          }
          ${p.ai_analysis.print_subject
            ? `<div style="font-size:12px;color:var(--ink-secondary);">印花主体：${escapeHtml(p.ai_analysis.print_subject)}${p.ai_analysis.visual_style ? ` · 风格：${escapeHtml(p.ai_analysis.visual_style)}` : ""}</div>`
            : ""
          }
          ${p.ai_analysis.slogan_text
            ? `<div style="font-size:12px;color:var(--ink-secondary);">文案 OCR：<span class="mono">${escapeHtml(p.ai_analysis.slogan_text)}</span></div>`
            : ""
          }
          ${(p.ai_analysis.theme_tags || []).length
            ? `<div class="tag-row">${p.ai_analysis.theme_tags.map((t) => `<span class="badge badge-info">${escapeHtml(t)}</span>`).join("")}</div>`
            : ""
          }
        </div>`
      : ""
    }

    <div>
      <h4 style="margin:0 0 8px;font-size:13px;">自动规则命中证据</h4>
      <div style="display:flex;flex-direction:column;gap:6px;">${reasonsHtml}</div>
    </div>

    <div>
      <h4 style="margin:0 0 8px;font-size:13px;">数据质量问题</h4>
      <div class="tag-row">${renderQualityBadges(p)}</div>
    </div>

    <div>
      <h4 style="margin:0 0 8px;font-size:13px;">来源类目与独立 BSR</h4>
      <div class="table-scroll">
        <table class="data-table" style="min-width:400px;">
          <thead><tr><th>来源类目</th><th>Node ID</th><th class="num-cell">位次</th><th class="num-cell">源类目 BSR</th></tr></thead>
          <tbody>${sourcesHtml}</tbody>
        </table>
      </div>
    </div>

    <div>
      <h4 style="margin:0 0 8px;font-size:13px;">人工审核历史记录</h4>
      ${historyHtml}
    </div>
  `;

  body.querySelectorAll("[data-thumb-url]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const url = btn.getAttribute("data-thumb-url");
      const mainBox = document.getElementById("drawer-main-photo");
      if (mainBox) mainBox.innerHTML = renderSafeImage(url, p.title || p.asin, "", true);
      body.querySelectorAll(".drawer-thumb-btn").forEach((b) => b.classList.remove("is-active"));
      btn.classList.add("is-active");
    });
  });

  const runAction = async (action) => {
    const note = document.getElementById("drawer-review-note")?.value || "";
    const reasonCode = action === "confirm_exclude"
      ? document.getElementById("drawer-exclude-reason")?.value
      : action === "confirm_keep"
        ? document.getElementById("drawer-keep-reason")?.value
        : null;
    const res = await onReviewDone({
      asin: p.asin,
      action,
      reason_code: reasonCode,
      reason_note: note,
      request_id: makeRequestId("drw"),
    });
    if (res && res.item) {
      renderPreprocessingDrawerBody(body, res.item, {
        observedDate,
        excludeReasons,
        keepReasons,
        pageAsins,
        onNavigateAsin,
        onReviewDone,
        onBatchReview,
      });
    }
  };

  document.getElementById("drawer-btn-keep")?.addEventListener("click", () => runAction("confirm_keep"));
  document.getElementById("drawer-btn-exclude")?.addEventListener("click", () => runAction("confirm_exclude"));
  document.getElementById("drawer-btn-restore")?.addEventListener("click", () => runAction("restore"));
  document.getElementById("drawer-btn-prev")?.addEventListener("click", () => {
    if (prevAsin && onNavigateAsin) onNavigateAsin(prevAsin);
  });
  document.getElementById("drawer-btn-next")?.addEventListener("click", () => {
    if (nextAsin && onNavigateAsin) onNavigateAsin(nextAsin);
  });

  // Keyboard shortcuts while drawer is open (ignored when typing in inputs or when modal is open)
  activeDrawerKeyHandler = (e) => {
    const drawerEl = document.getElementById("detail-drawer");
    if (!drawerEl || !drawerEl.classList.contains("is-open")) {
      detachDrawerKeyHandler();
      return;
    }
    const modalEl = document.getElementById("review-modal-overlay");
    if (modalEl && modalEl.classList.contains("is-open")) return;
    const tag = (e.target && e.target.tagName) ? e.target.tagName.toLowerCase() : "";
    if (tag === "input" || tag === "textarea" || tag === "select") return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;

    const key = e.key.toLowerCase();
    if (key === "k") {
      e.preventDefault();
      runAction("confirm_keep");
    } else if (key === "e") {
      e.preventDefault();
      runAction("confirm_exclude");
    } else if (e.key === "ArrowLeft" && prevAsin && onNavigateAsin) {
      e.preventDefault();
      onNavigateAsin(prevAsin);
    } else if (e.key === "ArrowRight" && nextAsin && onNavigateAsin) {
      e.preventDefault();
      onNavigateAsin(nextAsin);
    }
  };
  document.addEventListener("keydown", activeDrawerKeyHandler);

  document.getElementById("drawer-btn-exclude-brand")?.addEventListener("click", () => {
    openGroupBatchExcludeModal({
      groupType: "brand",
      groupValue: p.brand,
      estimatedCount: p.brand_count,
      observedDate,
      excludeReasons,
      onConfirmBatch: async (payload) => {
        if (onBatchReview) await onBatchReview(payload);
        const fresh = await api.getPreprocessingProductDetail(p.asin, observedDate);
        if (fresh && fresh.item) {
          renderPreprocessingDrawerBody(body, fresh.item, {
            observedDate,
            excludeReasons,
            keepReasons,
            pageAsins,
            onNavigateAsin,
            onReviewDone,
            onBatchReview,
          });
        }
      },
    });
  });

  document.getElementById("drawer-btn-exclude-parent")?.addEventListener("click", () => {
    openGroupBatchExcludeModal({
      groupType: "parent_asin",
      groupValue: p.parent_asin,
      estimatedCount: p.parent_count,
      observedDate,
      excludeReasons,
      onConfirmBatch: async (payload) => {
        if (onBatchReview) await onBatchReview(payload);
        const fresh = await api.getPreprocessingProductDetail(p.asin, observedDate);
        if (fresh && fresh.item) {
          renderPreprocessingDrawerBody(body, fresh.item, {
            observedDate,
            excludeReasons,
            keepReasons,
            pageAsins,
            onNavigateAsin,
            onReviewDone,
            onBatchReview,
          });
        }
      },
    });
  });
}

export function renderPreprocessingView(
  container,
  {
    overview,
    pageData,
    filters,
    viewMode = "gallery",
    rulesOpen = false,
    moreFiltersOpen = false,
    selectedAsins = [],
    batchReason = "solid_non_print",
    batchNote = "",
    running,
    aiRunning = false,
    toast,
    handlers,
  }
) {
  if (!overview) {
    container.innerHTML = `<div class="loading-state"><div class="spinner"></div><p>正在加载数据预处理工作台…</p></div>`;
    return;
  }

  if (!overview.ready) {
    const st = overview.db_status || {};
    container.innerHTML = `
      <div class="empty-state">
        <span class="badge badge-warn">${escapeHtml(st.state || "empty")}</span>
        <h2 style="margin:6px 0 0;font-size:17px;color:var(--ink-primary);">暂无可预处理的原始快照数据</h2>
        <p style="max-width:520px;margin:0;font-size:13px;">${escapeHtml(st.message || "请先在 01 数据采集同步云端商品快照。")}</p>
        <button type="button" class="btn btn-primary" id="btn-run-preprocessing-empty" ${running ? "disabled" : ""}>尝试执行预处理</button>
      </div>
    `;
    document.getElementById("btn-run-preprocessing-empty")?.addEventListener("click", () => handlers.onRunPreprocessing());
    return;
  }

  const c = overview.counts;
  const r = overview.rules_summary;
  const aiCounts = overview.ai_counts || {
    cached_count: 0,
    suggest_exclude_count: 0,
    suggest_exclude_unreviewed_count: 0,
    graphic_print_count: 0,
  };
  const latestRun = overview.latest_run;
  const hasBlocked = c.blocked > 0;
  const selectedSet = new Set(selectedAsins || []);
  const pageItems = (pageData && pageData.items) || [];
  const allPageSelected = pageItems.length > 0 && pageItems.every((i) => selectedSet.has(i.asin));
  const selectedCount = selectedSet.size;
  const isAiExcludeTab = (filters.ai_filter || "all") === "suggest_exclude";

  const activeAdvancedCount = [
    filters.auto_class && filters.auto_class !== "all",
    filters.review_status && filters.review_status !== "all",
    filters.issue && filters.issue !== "all",
  ].filter(Boolean).length;
  const isMoreOpen = moreFiltersOpen || activeAdvancedCount > 0;

  const qualityAnomalyTotal =
    (Number(r.quality_check.price_anomaly) || 0) + (Number(r.quality_check.sales_suspect) || 0);

  const toastHtml = toast
    ? `<div class="prep-toast ${toast.type === "error" ? "alert-danger" : "alert-ok"}" id="prep-toast" role="status" aria-live="polite">
        <span>${escapeHtml(toast.message)}</span>
        <div style="display:flex;gap:8px;align-items:center;">
          ${toast.undoAsin ? `<button type="button" class="btn btn-secondary btn-sm" id="btn-toast-undo">撤销</button>` : ""}
          ${toast.undoAsins && toast.undoAsins.length ? `<button type="button" class="btn btn-secondary btn-sm" id="btn-toast-undo-batch">撤销本次批量 (${toast.undoAsins.length})</button>` : ""}
          <button type="button" class="btn btn-ghost btn-sm" id="btn-toast-dismiss">关闭</button>
        </div>
      </div>`
    : "";

  container.innerHTML = `
    <!-- 1. Compact Single-Row Top Execution Bar + Collapsible Rules Diagnostic Drawer -->
    <section class="panel" style="padding:12px 18px;" aria-label="预处理执行与规则诊断">
      <div class="prep-header-bar">
        <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;font-size:12.5px;color:var(--ink-secondary);">
          <span>快照 <strong class="mono" style="color:var(--ink-primary);">${escapeHtml(overview.selected_date || "—")}</strong></span>
          ${latestRun ? `<span>最近处理 ${formatBeijingTime(latestRun.finished_at)}</span>` : ""}
          ${overview.stale_review_count > 0
            ? `<span class="badge badge-warn">${formatInt(overview.stale_review_count)} 条结论需复核</span>`
            : ""
          }
          <button type="button" class="btn btn-ghost btn-sm" id="btn-prep-toggle-rules" aria-expanded="${rulesOpen ? "true" : "false"}" aria-controls="prep-rules-drawer">
            ${rulesOpen ? "收起规则诊断" : `规则与质量诊断（异常 ${formatInt(qualityAnomalyTotal)}）`}
          </button>
        </div>

        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
          <button type="button" class="btn btn-ghost btn-sm" id="btn-prep-ai-config" title="配置 DeepSeek 视觉模型与 API Key">AI 配置</button>
          <button type="button" class="btn btn-ghost btn-sm" id="btn-run-preprocessing" ${running ? "disabled" : ""}>
            ${running ? "预处理执行中…" : "重新运行预处理"}
          </button>
          <button type="button" class="btn btn-ai-accent btn-sm" id="btn-prep-ai-purify" ${aiRunning ? "disabled" : ""} title="使用 DeepSeek 视觉识别纯色/条纹/波点并标记建议，绝不自动排除">
            ${ICON_SPARKLE}
            <span>${aiRunning ? "AI 看图提纯中…" : `AI 视觉提纯${aiCounts.cached_count ? ` (${formatInt(aiCounts.cached_count)})` : ""}`}</span>
          </button>
          <button type="button" class="btn ${c.needs_review === 0 ? "btn-primary" : "btn-secondary"} btn-sm" id="btn-goto-scoring">
            <span>前往 04 候选评分</span>
            ${ICON_ARROW_RIGHT}
          </button>
        </div>
      </div>

      <div class="prep-rules-drawer" id="prep-rules-drawer" ${rulesOpen ? "" : "hidden"}>
        <div class="prep-rule-col">
          <div class="prep-rule-col-title">
            <span>商品去重（按站点 + ASIN）</span>
          </div>
          <div class="prep-rule-metrics">
            <span>原始观察 <strong>${formatInt(r.deduplication.total_observations)}</strong></span>
            <span>跨类目合并 <strong>${formatInt(r.deduplication.cross_category_merged)}</strong></span>
            <span>父子同号 <strong>${formatInt(r.deduplication.parent_same_flagged)}</strong></span>
          </div>
        </div>

        <div class="prep-rule-col">
          <div class="prep-rule-col-title">
            <span>数据质量检查（不补值 · 不默认排除）</span>
          </div>
          <div class="prep-rule-metrics">
            <span>$0 售价 <strong>${formatInt(r.quality_check.price_zero)}</strong></span>
            <span>价格异常 <strong>${formatInt(r.quality_check.price_anomaly || 0)}</strong></span>
            <span>销量存疑 <strong>${formatInt(r.quality_check.sales_suspect || 0)}</strong></span>
            <span>缺星级 <strong>${formatInt(r.quality_check.rating_missing)}</strong></span>
            <span>缺主图 <strong>${formatInt(r.quality_check.image_missing || 0)}</strong></span>
          </div>
        </div>

        <div class="prep-rule-col">
          <div class="prep-rule-col-title">
            <span>印花相关性初筛（规则仅标记）</span>
          </div>
          <div class="prep-rule-metrics">
            <span>有线索 <strong>${formatInt(r.print_screening.possible_print)}</strong></span>
            <span>疑似非印花 <strong>${formatInt(r.print_screening.suspected_non_print)}</strong></span>
            <span>规则冲突 <strong>${formatInt(r.print_screening.conflicting)}</strong></span>
            <span>信息不足 <strong>${formatInt(r.print_screening.uncertain)}</strong></span>
          </div>
        </div>
      </div>
    </section>

    ${toastHtml}

    <!-- 2. Interactive 4-Card KPI Queue Switcher (Merges KPI + Status Tabs) -->
    <section class="prep-kpi-grid ${hasBlocked ? "has-blocked" : ""}" aria-label="预处理核心状态切换">
      <button type="button" class="kpi-card prep-kpi-btn ${!isAiExcludeTab && filters.eligibility === "all" ? "is-active" : ""}" data-prep-elig="all">
        <div class="kpi-top">
          <span class="kpi-label">去重商品总数</span>
        </div>
        <div class="kpi-value">${formatInt(c.total)}</div>
        <div class="kpi-sub">跨类目重合 ${formatInt(overview.cross_category_asin_count)} · 原始 ${formatInt(overview.total_observations)} 条</div>
      </button>

      <button type="button" class="kpi-card prep-kpi-btn ${!isAiExcludeTab && filters.eligibility === "ready" ? "is-active" : ""}" data-prep-elig="ready">
        <div class="kpi-top">
          <span class="kpi-label">可进入评分</span>
        </div>
        <div class="kpi-value" style="color:var(--ok)">${formatInt(c.ready)}</div>
        <div class="kpi-sub">人工保留 ${formatInt(overview.manual_counts.confirmed_keep)} · 自动候选可评分</div>
      </button>

      <button type="button" class="kpi-card prep-kpi-btn ${!isAiExcludeTab && filters.eligibility === "needs_review" ? "is-active" : ""}" data-prep-elig="needs_review">
        <div class="kpi-top">
          <span class="kpi-label">待人工核实</span>
        </div>
        <div class="kpi-value" style="color:var(--warn)">${formatInt(c.needs_review)}</div>
        <div class="kpi-sub">含未审疑似非印花、冲突及需复核项</div>
      </button>

      <button type="button" class="kpi-card prep-kpi-btn ${!isAiExcludeTab && filters.eligibility === "excluded" ? "is-active" : ""}" data-prep-elig="excluded">
        <div class="kpi-top">
          <span class="kpi-label">已人工排除</span>
        </div>
        <div class="kpi-value" style="color:var(--danger)">${formatInt(c.excluded)}</div>
        <div class="kpi-sub">人工确认排除 · 随时可一键恢复</div>
      </button>

      ${hasBlocked
        ? `<button type="button" class="kpi-card prep-kpi-btn ${!isAiExcludeTab && filters.eligibility === "blocked" ? "is-active" : ""}" data-prep-elig="blocked">
            <div class="kpi-top">
              <span class="kpi-label">关键数据阻断</span>
            </div>
            <div class="kpi-value">${formatInt(c.blocked)}</div>
            <div class="kpi-sub">缺少身份或核心评分输入</div>
          </button>`
        : ""
      }
    </section>

    <!-- 3. Borderless Visual Triage Workspace (Eliminates Nested Cards) -->
    <section class="prep-workspace" id="prep-table-section" aria-label="商品人工快审区">
      <div class="prep-toolbar-bar">
        <div class="prep-toolbar-left">
          <label class="triage-select-all">
            <input type="checkbox" id="prep-select-all-page" ${allPageSelected ? "checked" : ""}>
            <span id="prep-select-all-label">${allPageSelected ? "取消全选本页" : "全选本页"}</span>
          </label>

          <button type="button" class="prep-tab prep-tab-ai ${isAiExcludeTab ? "is-active" : ""}" data-prep-ai="suggest_exclude" title="筛选 AI 视觉判定为非印花且置信度 ≥ 85% 的纯色/条纹/波点商品">
            ${ICON_SPARKLE}
            <span>AI 建议排除 (${formatInt(aiCounts.suggest_exclude_count)})</span>
          </button>

          <input type="search" id="prep-search" class="input-control search-input"
            placeholder="搜索 ASIN / 标题 / 品牌…" value="${escapeHtml(filters.q)}" aria-label="搜索预处理商品">

          <select id="prep-category-filter" class="select-control" aria-label="来源类目筛选">
            <option value="all" ${(filters.category || "all") === "all" ? "selected" : ""}>全部类目 (${(overview.categories || []).filter((cat) => cat.is_configured).length}/${(overview.categories || []).length || 6})</option>
            ${(overview.categories || []).map((cat) => {
              const val = cat.node_id || cat.key;
              const selected = filters.category === val || filters.category === cat.key;
              return `<option value="${escapeHtml(val)}" ${selected ? "selected" : ""}>${escapeHtml(cat.name)}${cat.is_configured ? "" : "（待配置）"}</option>`;
            }).join("")}
          </select>

          <select id="prep-sort" class="select-control" aria-label="排序方式">
            <option value="needs_review_first" ${filters.sort === "needs_review_first" ? "selected" : ""}>待核实优先</option>
            <option value="listing_sales_30d" ${filters.sort === "listing_sales_30d" ? "selected" : ""}>30 天估算销量</option>
            <option value="price" ${filters.sort === "price" ? "selected" : ""}>有效售价</option>
            <option value="rating_stars" ${filters.sort === "rating_stars" ? "selected" : ""}>星级评分</option>
            <option value="main_category_bsr" ${filters.sort === "main_category_bsr" ? "selected" : ""}>大类 BSR</option>
          </select>

          <button type="button" class="btn btn-ghost btn-sm" id="btn-prep-more-filters" aria-expanded="${isMoreOpen ? "true" : "false"}" aria-controls="prep-more-filters-drawer">
            ${ICON_SLIDERS}
            <span>更多筛选${activeAdvancedCount > 0 ? ` (${activeAdvancedCount})` : ""}</span>
          </button>

          <button type="button" class="btn btn-ghost btn-sm" id="prep-reset-btn">重置</button>
        </div>

        <div class="prep-toolbar-right">
          <span style="font-size:12.5px;color:var(--ink-muted);font-weight:600;" id="prep-table-count">${pageData ? formatInt(pageData.total) : "—"} 件</span>
          <div class="segmented" role="group" aria-label="视图模式切换">
            <button type="button" class="seg-btn ${viewMode === "gallery" ? "is-active" : ""}" data-prep-view="gallery">大图快审</button>
            <button type="button" class="seg-btn ${viewMode === "table" ? "is-active" : ""}" data-prep-view="table">列表视图</button>
          </div>
        </div>

        <div class="prep-more-filters" id="prep-more-filters-drawer" ${isMoreOpen ? "" : "hidden"}>
          <select id="prep-auto-filter" class="select-control" aria-label="自动判断筛选">
            <option value="all" ${filters.auto_class === "all" ? "selected" : ""}>全部自动判断</option>
            <option value="suspected_non_print" ${filters.auto_class === "suspected_non_print" ? "selected" : ""}>疑似非印花</option>
            <option value="conflicting" ${filters.auto_class === "conflicting" ? "selected" : ""}>规则冲突</option>
            <option value="uncertain" ${filters.auto_class === "uncertain" ? "selected" : ""}>信息不足</option>
            <option value="possible_print" ${filters.auto_class === "possible_print" ? "selected" : ""}>有印花线索</option>
          </select>

          <select id="prep-review-filter" class="select-control" aria-label="人工审核状态筛选">
            <option value="all" ${filters.review_status === "all" ? "selected" : ""}>全部人工状态</option>
            <option value="unreviewed" ${filters.review_status === "unreviewed" ? "selected" : ""}>未审核</option>
            <option value="confirmed_keep" ${filters.review_status === "confirmed_keep" ? "selected" : ""}>已确认保留</option>
            <option value="confirmed_exclude" ${filters.review_status === "confirmed_exclude" ? "selected" : ""}>已确认排除</option>
            <option value="stale" ${filters.review_status === "stale" ? "selected" : ""}>需复核（信息已变更）</option>
          </select>

          <select id="prep-issue-filter" class="select-control" aria-label="数据问题筛选">
            <option value="all" ${filters.issue === "all" ? "selected" : ""}>全部质量状态</option>
            <option value="any" ${filters.issue === "any" ? "selected" : ""}>含任意数据问题</option>
            <option value="price_anomaly" ${filters.issue === "price_anomaly" ? "selected" : ""}>价格异常 (&lt;$5)</option>
            <option value="sales_suspect" ${filters.issue === "sales_suspect" ? "selected" : ""}>销量存疑 (BSR背离)</option>
            <option value="parent_asin_same" ${filters.issue === "parent_asin_same" ? "selected" : ""}>父子编号相同</option>
            <option value="price_zero" ${filters.issue === "price_zero" ? "selected" : ""}>售价为 $0</option>
            <option value="rating_missing" ${filters.issue === "rating_missing" ? "selected" : ""}>星级缺失</option>
            <option value="bsr_missing" ${filters.issue === "bsr_missing" ? "selected" : ""}>源类目 BSR 缺失</option>
            <option value="clean" ${filters.issue === "clean" ? "selected" : ""}>字段完备</option>
          </select>
        </div>
      </div>

      <div id="prep-table-wrap">
        ${viewMode === "table"
          ? renderPrepTableHtml(pageData, selectedSet)
          : renderPrepGalleryHtml(pageData, selectedSet)
        }
      </div>

      <!-- Floating Bottom Multi-Select Batch Action Bar (Only visible when >= 1 item selected) -->
      <div class="triage-batch-bar ${selectedCount > 0 ? "has-selection" : ""}" id="prep-batch-bar" aria-label="批量审核操作栏">
        <div class="triage-batch-left">
          <span class="badge badge-info" id="prep-selected-count">已选 ${selectedCount} 件</span>
          <button type="button" class="btn btn-ghost btn-sm" id="prep-clear-selection">清空勾选</button>
        </div>
        <div class="triage-batch-right">
          <button type="button" class="btn btn-ok-soft btn-sm" id="btn-batch-keep" ${selectedCount === 0 ? "disabled" : ""}>
            批量确认保留
          </button>
          <button type="button" class="btn btn-danger-soft btn-sm" id="btn-batch-exclude" ${selectedCount === 0 ? "disabled" : ""}>
            批量确认排除
          </button>
          <button type="button" class="btn btn-secondary btn-sm" id="btn-batch-restore" ${selectedCount === 0 ? "disabled" : ""}>
            批量重置待审
          </button>
        </div>
      </div>
    </section>
  `;

  // Bind top buttons
  document.getElementById("btn-prep-ai-config")?.addEventListener("click", () => handlers.onOpenAiConfig?.());
  document.getElementById("btn-prep-ai-purify")?.addEventListener("click", () => handlers.onRunAiPurify?.());
  document.getElementById("btn-run-preprocessing")?.addEventListener("click", () => handlers.onRunPreprocessing());
  document.getElementById("btn-goto-scoring")?.addEventListener("click", () => handlers.onJumpToScoring?.());
  document.getElementById("btn-prep-toggle-rules")?.addEventListener("click", () => handlers.onToggleRulesDrawer?.());
  document.getElementById("btn-prep-more-filters")?.addEventListener("click", () => handlers.onToggleMoreFilters?.());

  document.getElementById("btn-toast-dismiss")?.addEventListener("click", () => handlers.onDismissToast());
  document.getElementById("btn-toast-undo")?.addEventListener("click", () => {
    if (toast && toast.undoAsin) {
      handlers.onSubmitReview({
        asin: toast.undoAsin,
        action: "restore",
        request_id: makeRequestId("undo"),
      });
    }
  });
  document.getElementById("btn-toast-undo-batch")?.addEventListener("click", () => {
    if (toast && toast.undoAsins && toast.undoAsins.length) {
      handlers.onSubmitBatchReview({
        action: "restore",
        asins: toast.undoAsins,
        reason_note: "撤销上一次批量审核",
        operator_source: "workbench_batch_undo",
        request_id: makeRequestId("undob"),
      });
    }
  });

  // View mode switch
  container.querySelectorAll("[data-prep-view]").forEach((btn) => {
    btn.addEventListener("click", () => {
      handlers.onToggleViewMode(btn.getAttribute("data-prep-view"));
    });
  });

  // KPI queue switchers & AI suggest-exclude toggle
  container.querySelectorAll("[data-prep-elig]").forEach((btn) => {
    btn.addEventListener("click", () => {
      handlers.onFilterChange({ eligibility: btn.getAttribute("data-prep-elig"), ai_filter: "all", page: 1 });
    });
  });
  container.querySelectorAll("[data-prep-ai]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const target = btn.getAttribute("data-prep-ai");
      const next = (filters.ai_filter || "all") === target ? "all" : target;
      handlers.onFilterChange({ ai_filter: next, eligibility: "all", page: 1 });
    });
  });

  let timer = null;
  document.getElementById("prep-search")?.addEventListener("input", (e) => {
    clearTimeout(timer);
    const val = e.target.value;
    timer = setTimeout(() => handlers.onFilterChange({ q: val, page: 1 }), 200);
  });
  document.getElementById("prep-category-filter")?.addEventListener("change", (e) => {
    handlers.onFilterChange({ category: e.target.value, page: 1 });
  });
  document.getElementById("prep-auto-filter")?.addEventListener("change", (e) => {
    handlers.onFilterChange({ auto_class: e.target.value, page: 1 });
  });
  document.getElementById("prep-review-filter")?.addEventListener("change", (e) => {
    handlers.onFilterChange({ review_status: e.target.value, page: 1 });
  });
  document.getElementById("prep-issue-filter")?.addEventListener("change", (e) => {
    handlers.onFilterChange({ issue: e.target.value, page: 1 });
  });
  document.getElementById("prep-sort")?.addEventListener("change", (e) => {
    const field = e.target.value;
    handlers.onFilterChange({ sort: field, order: field === "main_category_bsr" ? "asc" : "desc", page: 1 });
  });
  document.getElementById("prep-reset-btn")?.addEventListener("click", () => handlers.onResetFilters());

  // Batch bar events
  document.getElementById("prep-select-all-page")?.addEventListener("change", (e) => {
    const pageAsins = pageItems.map((i) => i.asin);
    handlers.onToggleSelectPage(pageAsins, e.target.checked);
  });
  document.getElementById("prep-clear-selection")?.addEventListener("click", () => handlers.onClearSelection());

  const getActiveAsins = () => (handlers.getSelectedAsins ? handlers.getSelectedAsins() : Array.from(selectedSet));

  document.getElementById("btn-batch-keep")?.addEventListener("click", () => {
    const asins = getActiveAsins();
    if (!asins.length) return;
    handlers.onSubmitBatchReview({
      action: "confirm_keep",
      asins,
      reason_code: "confirmed_print_visual",
      reason_note: "",
      operator_source: "workbench_batch_select",
      request_id: makeRequestId("bsel"),
    });
  });

  document.getElementById("btn-batch-exclude")?.addEventListener("click", () => {
    const asins = getActiveAsins();
    if (!asins.length) return;
    openMultiBatchExcludeModal({
      asins,
      defaultReason: batchReason,
      defaultNote: batchNote,
      excludeReasons: overview.exclude_reasons,
      onConfirm: ({ reason_code, reason_note }) => {
        handlers.onBatchInputChange?.({ batchReason: reason_code, batchNote: reason_note });
        return handlers.onSubmitBatchReview({
          action: "confirm_exclude",
          asins,
          reason_code,
          reason_note,
          operator_source: "workbench_batch_select",
          request_id: makeRequestId("bsel"),
        });
      },
    });
  });

  document.getElementById("btn-batch-restore")?.addEventListener("click", () => {
    const asins = getActiveAsins();
    if (!asins.length) return;
    handlers.onSubmitBatchReview({
      action: "restore",
      asins,
      reason_code: null,
      reason_note: "",
      operator_source: "workbench_batch_select",
      request_id: makeRequestId("bsel"),
    });
  });

  bindPrepItemsEvents(container, { overview, pageData, handlers });
}

export function renderPrepGalleryHtml(pageData, selectedSet = new Set()) {
  if (!pageData) {
    return `<div class="loading-state"><div class="spinner"></div><p>加载主图瀑布流…</p></div>`;
  }
  if (!pageData.items || pageData.items.length === 0) {
    return `<div class="panel" style="text-align:center;padding:32px;"><strong style="font-size:14px;">当前筛选条件下无匹配商品</strong><p style="margin:6px 0 0;font-size:13px;color:var(--ink-muted);">请点击上方其他状态卡片或点击「重置」查看全部商品。</p></div>`;
  }

  const cardsHtml = pageData.items.map((p, idx) => {
    const checked = selectedSet.has(p.asin);
    const isExcluded = p.manual_review_status === "confirmed_exclude";
    const showBrandBulk = Boolean(p.brand && p.brand.trim() && (p.brand_count || 0) >= 2);
    const showParentBulk = Boolean(p.parent_asin && p.parent_asin.trim() && (p.parent_count || 0) >= 2);
    return `
      <article class="triage-card ${checked ? "is-selected" : ""} ${isExcluded ? "is-excluded" : ""}" data-prep-asin="${escapeHtml(p.asin)}">
        <div class="triage-media" data-act-detail="${escapeHtml(p.asin)}" title="点击查看多图与核验详情">
          <label class="triage-check-wrap" title="勾选以批量操作">
            <input type="checkbox" class="triage-checkbox" data-select-asin="${escapeHtml(p.asin)}" ${checked ? "checked" : ""} aria-label="勾选 ${escapeHtml(p.asin)}">
          </label>
          <span class="triage-status-pill">${renderManualBadge(p)}</span>
          ${renderSafeImage(p.image_url, p.title || p.asin, "", idx < 10)}
        </div>
        <div class="triage-body">
          <div class="triage-badges">
            ${renderAutoBadge(p)}
            ${renderAiVisionBadge(p)}
            ${renderCredibilityAnomalyBadges(p)}
          </div>
          <h2 class="triage-title" data-act-detail="${escapeHtml(p.asin)}" title="${escapeHtml(p.title || "")}">
            ${escapeHtml(p.title || "（无标题）")}
          </h2>
          <div class="triage-metrics">
            <span class="triage-price">${formatPrice(p.raw_price_usd, p.price_valid)}</span>
            <span class="triage-sales" title="30天估算销量">${formatInt(p.listing_sales_30d)}/月</span>
          </div>
          <div class="triage-actions">
            ${p.manual_review_status !== "confirmed_keep"
              ? `<button type="button" class="btn btn-ok-soft btn-sm" data-act-keep="${escapeHtml(p.asin)}">保留</button>`
              : ""
            }
            ${p.manual_review_status !== "confirmed_exclude"
              ? `<button type="button" class="btn btn-danger-soft btn-sm" data-act-exclude="${escapeHtml(p.asin)}">排除</button>`
              : ""
            }
            ${p.manual_review_status !== "unreviewed"
              ? `<button type="button" class="btn btn-secondary btn-sm" data-act-restore="${escapeHtml(p.asin)}">恢复</button>`
              : ""
            }
          </div>
          ${(showBrandBulk || showParentBulk)
            ? `<div class="triage-bulk-links">
                ${showBrandBulk
                  ? `<button type="button" class="triage-link-btn" data-exclude-brand="${escapeHtml(p.brand)}" data-est-count="${p.brand_count}" title="一键排除品牌 ${escapeHtml(p.brand)} 全部 ${p.brand_count} 件商品">排同品牌(${p.brand_count})</button>`
                  : ""
                }
                ${showParentBulk
                  ? `<button type="button" class="triage-link-btn" data-exclude-parent="${escapeHtml(p.parent_asin)}" data-est-count="${p.parent_count}" title="一键排除父体 ${escapeHtml(p.parent_asin)} 全部 ${p.parent_count} 件商品">排同父体(${p.parent_count})</button>`
                  : ""
                }
              </div>`
            : ""
          }
        </div>
      </article>
    `;
  }).join("");

  return `
    <div class="triage-grid" id="prep-gallery-grid">
      ${cardsHtml}
    </div>
    <div class="table-footer">
      <div>共 <strong>${formatInt(pageData.total)}</strong> 件 · 第 <strong>${pageData.page}</strong> / ${Math.max(1, pageData.total_pages)} 页</div>
      <div class="pagination">
        <button type="button" class="btn btn-secondary btn-sm" id="prep-page-prev" ${pageData.page <= 1 ? "disabled" : ""}>上一页</button>
        <button type="button" class="btn btn-secondary btn-sm" id="prep-page-next" ${pageData.page >= pageData.total_pages ? "disabled" : ""}>下一页</button>
      </div>
    </div>
  `;
}

export function renderPrepTableHtml(pageData, selectedSet = new Set()) {
  if (!pageData) {
    return `<div class="loading-state"><div class="spinner"></div><p>加载核验列表…</p></div>`;
  }
  if (!pageData.items || pageData.items.length === 0) {
    return `<div class="panel" style="text-align:center;padding:32px;"><strong style="font-size:14px;">当前筛选条件下无匹配商品</strong><p style="margin:6px 0 0;font-size:13px;color:var(--ink-muted);">请切换上方状态卡片或点击「重置」。</p></div>`;
  }

  const rowsHtml = pageData.items.map((p) => {
    const checked = selectedSet.has(p.asin);
    return `
      <tr data-prep-asin="${escapeHtml(p.asin)}" class="${checked ? "is-selected" : ""}">
        <td style="width:36px;text-align:center;">
          <input type="checkbox" class="triage-checkbox" data-select-asin="${escapeHtml(p.asin)}" ${checked ? "checked" : ""} aria-label="勾选 ${escapeHtml(p.asin)}">
        </td>
        <td>
          <div class="cell-product" data-act-detail="${escapeHtml(p.asin)}" style="cursor:pointer;">
            <div class="thumb-box">${renderSafeImage(p.image_url, p.asin)}</div>
            <div style="min-width:0;">
              <div class="cell-title" title="${escapeHtml(p.title)}">${escapeHtml(p.title || "（无标题）")}</div>
              <div class="cell-sub">
                <span class="mono">${escapeHtml(p.asin)}</span>
                <span>·</span>
                <span>${escapeHtml(p.brand || "未知")}</span>
              </div>
              <div class="tag-row" style="margin-top:4px;">
                ${(p.source_categories || []).map((s) => renderSourceCategoryBadge(s, { showBsr: false })).join(" ")}
              </div>
            </div>
          </div>
        </td>
        <td class="num-cell">
          <div style="font-weight:700;color:var(--accent-text);">${formatInt(p.listing_sales_30d)}</div>
          <div style="font-size:12px;color:var(--ink-muted);margin-top:2px;">${formatPrice(p.raw_price_usd, p.price_valid)}</div>
        </td>
        <td class="num-cell" style="white-space:nowrap;">${formatRatingHtml(p.rating_stars, p.ratings_count, p.rating_stars !== null ? "valid" : "missing")}</td>
        <td><div class="tag-row">${renderAutoBadge(p)} ${renderAiVisionBadge(p)}</div></td>
        <td><div class="tag-row">${renderQualityBadges(p)}</div></td>
        <td><div class="tag-row">${renderManualBadge(p)}</div></td>
        <td>${renderEligibilityBadge(p)}</td>
        <td>
          <div class="prep-actions">
            ${p.manual_review_status !== "confirmed_keep"
              ? `<button type="button" class="btn btn-ok-soft btn-sm" data-act-keep="${escapeHtml(p.asin)}">保留</button>`
              : ""
            }
            ${p.manual_review_status !== "confirmed_exclude"
              ? `<button type="button" class="btn btn-danger-soft btn-sm" data-act-exclude="${escapeHtml(p.asin)}">排除</button>`
              : ""
            }
            ${p.manual_review_status !== "unreviewed"
              ? `<button type="button" class="btn btn-secondary btn-sm" data-act-restore="${escapeHtml(p.asin)}">恢复</button>`
              : ""
            }
            ${p.brand && (p.brand_count || 0) >= 2
              ? `<button type="button" class="btn btn-ghost btn-sm" data-exclude-brand="${escapeHtml(p.brand)}" data-est-count="${p.brand_count}" title="排除同品牌全部 ${p.brand_count} 件">排品牌(${p.brand_count})</button>`
              : ""
            }
            ${p.parent_asin && (p.parent_count || 0) >= 2
              ? `<button type="button" class="btn btn-ghost btn-sm" data-exclude-parent="${escapeHtml(p.parent_asin)}" data-est-count="${p.parent_count}" title="排除同父体全部 ${p.parent_count} 件">排父体(${p.parent_count})</button>`
              : ""
            }
          </div>
        </td>
      </tr>
    `;
  }).join("");

  return `
    <div class="panel" style="padding:0;overflow:hidden;">
      <div class="table-scroll" style="border:none;border-radius:0;">
        <table class="data-table" style="min-width:1040px;">
          <thead>
            <tr>
              <th style="width:36px;text-align:center;">选</th>
              <th>商品 / ASIN</th>
              <th class="num-cell">30天销量 / 售价</th>
              <th class="num-cell">评分</th>
              <th>自动判断</th>
              <th>数据问题</th>
              <th>人工审核</th>
              <th>评分准入</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>${rowsHtml}</tbody>
        </table>
      </div>
    </div>
    <div class="table-footer">
      <div>共 <strong>${formatInt(pageData.total)}</strong> 件 · 第 <strong>${pageData.page}</strong> / ${Math.max(1, pageData.total_pages)} 页</div>
      <div class="pagination">
        <button type="button" class="btn btn-secondary btn-sm" id="prep-page-prev" ${pageData.page <= 1 ? "disabled" : ""}>上一页</button>
        <button type="button" class="btn btn-secondary btn-sm" id="prep-page-next" ${pageData.page >= pageData.total_pages ? "disabled" : ""}>下一页</button>
      </div>
    </div>
  `;
}

export function bindPrepItemsEvents(root, { overview, pageData, handlers }) {
  const byAsin = new Map(((pageData && pageData.items) || []).map((i) => [i.asin, i]));

  root.querySelectorAll("[data-select-asin]").forEach((cb) => {
    cb.addEventListener("click", (e) => e.stopPropagation());
    cb.addEventListener("change", (e) => {
      e.stopPropagation();
      handlers.onToggleSelectAsin(cb.getAttribute("data-select-asin"), cb.checked);
    });
  });

  root.querySelectorAll(".triage-check-wrap").forEach((lbl) => {
    lbl.addEventListener("click", (e) => e.stopPropagation());
  });

  root.querySelectorAll("[data-act-keep]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const asin = btn.getAttribute("data-act-keep");
      handlers.onSubmitReview({
        asin,
        action: "confirm_keep",
        reason_code: "keep_for_scoring",
        request_id: makeRequestId("keep"),
      });
    });
  });

  root.querySelectorAll("[data-act-exclude]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const asin = btn.getAttribute("data-act-exclude");
      const item = byAsin.get(asin);
      if (!item) return;
      openExcludeModal(item, overview.exclude_reasons, (payload) => handlers.onSubmitReview(payload));
    });
  });

  root.querySelectorAll("[data-act-restore]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const asin = btn.getAttribute("data-act-restore");
      handlers.onSubmitReview({
        asin,
        action: "restore",
        request_id: makeRequestId("rest"),
      });
    });
  });

  root.querySelectorAll("[data-exclude-brand]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      openGroupBatchExcludeModal({
        groupType: "brand",
        groupValue: btn.getAttribute("data-exclude-brand"),
        estimatedCount: Number(btn.getAttribute("data-est-count") || 1),
        observedDate: overview.selected_date,
        excludeReasons: overview.exclude_reasons,
        onConfirmBatch: (payload) => handlers.onSubmitBatchReview(payload),
      });
    });
  });

  root.querySelectorAll("[data-exclude-parent]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      openGroupBatchExcludeModal({
        groupType: "parent_asin",
        groupValue: btn.getAttribute("data-exclude-parent"),
        estimatedCount: Number(btn.getAttribute("data-est-count") || 1),
        observedDate: overview.selected_date,
        excludeReasons: overview.exclude_reasons,
        onConfirmBatch: (payload) => handlers.onSubmitBatchReview(payload),
      });
    });
  });

  root.querySelectorAll("[data-act-detail]").forEach((el) => {
    el.addEventListener("click", (e) => {
      if (e.target.closest("input, label, button")) return;
      e.stopPropagation();
      handlers.onOpenDetail(el.getAttribute("data-act-detail"));
    });
  });

  document.getElementById("prep-page-prev")?.addEventListener("click", () => handlers.onChangePage(-1));
  document.getElementById("prep-page-next")?.addEventListener("click", () => handlers.onChangePage(1));
}

export function syncPrepSelectionUi(selectedAsins) {
  const set = new Set(selectedAsins || []);
  const count = set.size;
  const bar = document.getElementById("prep-batch-bar");
  const badge = document.getElementById("prep-selected-count");
  const selectAllCb = document.getElementById("prep-select-all-page");
  const selectAllLbl = document.getElementById("prep-select-all-label");

  if (bar) bar.classList.toggle("has-selection", count > 0);
  if (badge) {
    badge.textContent = `已选 ${count} 件`;
  }

  ["btn-batch-keep", "btn-batch-exclude", "btn-batch-restore"].forEach((id) => {
    const b = document.getElementById(id);
    if (b) b.disabled = count === 0;
  });

  const cbs = Array.from(document.querySelectorAll("#prep-table-wrap [data-select-asin]"));
  cbs.forEach((cb) => {
    const asin = cb.getAttribute("data-select-asin");
    const checked = set.has(asin);
    cb.checked = checked;
    const host = cb.closest(".triage-card, tr[data-prep-asin]");
    if (host) host.classList.toggle("is-selected", checked);
  });

  const allChecked = cbs.length > 0 && cbs.every((cb) => cb.checked);
  if (selectAllCb) selectAllCb.checked = allChecked;
  if (selectAllLbl) selectAllLbl.textContent = allChecked ? "取消全选本页" : "全选本页";
}
