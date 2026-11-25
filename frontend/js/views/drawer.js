/**
 * Product Evidence Slide-over Drawer and Methodology Modal.
 */

import { api } from "../api.js";
import {
  escapeHtml,
  formatBeijingTime,
  formatDecimal,
  formatInt,
  formatPrice,
  formatRatingHtml,
  formatRevenue,
  renderSafeImage,
  renderSourceCategoryBadge,
} from "../format.js";

let lastFocusEl = null;

export function closeDrawer() {
  const drawer = document.getElementById("detail-drawer");
  const overlay = document.getElementById("drawer-overlay");
  if (!drawer || !overlay) return;
  drawer.classList.remove("is-open");
  overlay.classList.remove("is-open");
  drawer.setAttribute("aria-hidden", "true");
  overlay.setAttribute("aria-hidden", "true");
  if (lastFocusEl && typeof lastFocusEl.focus === "function") {
    lastFocusEl.focus();
  }
}

export async function openProductDrawer(asin, observedDate = "") {
  const drawer = document.getElementById("detail-drawer");
  const overlay = document.getElementById("drawer-overlay");
  const body = document.getElementById("drawer-body");
  const titleEl = document.getElementById("drawer-title");
  const eyebrowEl = document.getElementById("drawer-eyebrow");
  if (!drawer || !overlay || !body) return;

  lastFocusEl = document.activeElement;
  eyebrowEl.textContent = `PRODUCT EVIDENCE · ${asin}`;
  titleEl.textContent = `加载 ${asin}…`;
  body.innerHTML = `<div class="loading-state"><div class="spinner"></div><p>正在读取商品原始观察记录…</p></div>`;

  drawer.classList.add("is-open");
  overlay.classList.add("is-open");
  drawer.setAttribute("aria-hidden", "false");
  overlay.setAttribute("aria-hidden", "false");
  document.getElementById("drawer-close")?.focus();

  try {
    const res = await api.getProductDetail(asin, observedDate);
    const p = res.item;
    titleEl.textContent = p.asin;
    renderProductDetailBody(body, p);
  } catch (err) {
    body.innerHTML = `<div class="alert alert-danger">加载商品详情失败：${escapeHtml(err.message)}</div>`;
  }
}

function renderProductDetailBody(body, p) {
  const photos = p.photos && p.photos.length ? p.photos : (p.main_image_url ? [p.main_image_url] : []);
  const activeImg = photos[0] || "";

  const thumbsHtml = photos.length > 1
    ? `<div class="drawer-thumbs" role="list" aria-label="商品图库缩略图">
        ${photos.slice(0, 8).map((url, idx) => `
          <button type="button" class="drawer-thumb-btn ${idx === 0 ? "is-active" : ""}" data-thumb-url="${escapeHtml(url)}" aria-label="查看第 ${idx + 1} 张图">
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
        <td class="mono">#${s.batch_id}</td>
      </tr>
    `).join("");

  const dupMembers = p.duplicate_members || [];
  const dupClusterHtml = dupMembers.length > 1
    ? `<div>
        <h4 style="margin:0 0 6px;font-size:13px;">同款 / 孤立子体合并明细（共 ${dupMembers.length} 个 ASIN · 绝不累加销量）</h4>
        <div class="alert alert-info" style="margin-bottom:8px;padding:8px 10px;font-size:12px;">
          <strong>判定依据：</strong>${escapeHtml(p.duplicate_match_reason || "同品牌 + 同主图 + 标题高度一致")}
        </div>
        <div class="table-scroll">
          <table class="data-table" style="min-width:460px;">
            <thead>
              <tr>
                <th>ASIN</th>
                <th>角色 / 身份</th>
                <th class="num-cell">30天估算销量</th>
                <th class="num-cell">售价</th>
                <th class="num-cell">大类 BSR</th>
              </tr>
            </thead>
            <tbody>
              ${dupMembers.map((m) => `
                <tr data-sibling-asin="${escapeHtml(m.asin)}" style="${m.asin === p.asin ? "background:var(--accent-soft);" : ""}">
                  <td>
                    <div style="display:flex;align-items:center;gap:8px;">
                      <div class="thumb-box" style="width:32px;height:32px;">${renderSafeImage(m.main_image_url, m.asin)}</div>
                      <strong class="mono">${escapeHtml(m.asin)}</strong>
                    </div>
                  </td>
                  <td>
                    ${m.is_representative ? `<span class="badge badge-ok">代表款</span>` : `<span class="badge badge-neutral">已折叠</span>`}
                    ${m.needs_parent_verification ? `<span class="badge badge-warn">父子同号</span>` : ""}
                  </td>
                  <td class="num-cell">${formatInt(m.listing_sales_30d)}</td>
                  <td class="num-cell">${formatPrice(m.sales_price_usd, m.price_valid)}</td>
                  <td class="num-cell">${m.main_category_bsr !== null ? `#${formatInt(m.main_category_bsr)}` : "—"}</td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        </div>
      </div>`
    : "";

  const checksHtml = [
    {
      label: "父子 ASIN 身份",
      ok: !p.needs_parent_verification,
      text: p.asin_status + (p.parent_asin ? `（Parent: ${p.parent_asin}）` : ""),
    },
    {
      label: "售价有效性",
      ok: p.price_valid,
      text: p.price_valid ? `有效售价 $${formatDecimal(p.sales_price_usd, 2)}` : "源数据售价为 $0 或缺失，已隔离出价格均值/散点统计",
    },
    {
      label: "评分数据",
      ok: p.rating_status === "valid",
      text: p.rating_status === "valid" ? `${formatDecimal(p.rating_stars, 1)}★（${formatInt(p.ratings_count)} 评分）` : "接口返回 0 或空值，未补值",
    },
    {
      label: "跨类目去重状态",
      ok: true,
      text: p.is_cross_category
        ? `同时出现在 ${p.cross_category_count || (p.source_categories || []).length} 个目标类目；全量看板已按 ASIN 去重，未重复累加销量`
        : "仅出现在单一目标类目",
    },
  ].map((c) => `
    <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:10px;padding:8px 0;border-bottom:1px solid var(--border);">
      <span style="font-size:12px;color:var(--ink-secondary);font-weight:600;flex-shrink:0">${escapeHtml(c.label)}</span>
      <span style="font-size:12px;text-align:right;color:${c.ok ? "var(--ink-primary)" : "var(--warn)"}">${escapeHtml(c.text)}</span>
    </div>
  `).join("");

  body.innerHTML = `
    <div class="drawer-gallery-main" id="drawer-main-photo">
      ${renderSafeImage(activeImg, p.title || p.asin, "", true)}
    </div>
    ${thumbsHtml}

    <div>
      <div class="tag-row" style="margin-bottom:8px;">
        <span class="badge badge-neutral mono">ASIN: ${escapeHtml(p.asin)}</span>
        ${p.parent_asin ? `<span class="badge badge-neutral mono">Parent: ${escapeHtml(p.parent_asin)}</span>` : ""}
        ${p.needs_parent_verification ? `<span class="badge badge-warn">父子编号相同，需核对</span>` : ""}
        ${p.is_cross_category ? `<span class="badge badge-both">跨类目重合</span>` : ""}
        ${dupMembers.length > 1 ? `<span class="badge badge-info">同款簇 · ${dupMembers.length} 个 ASIN</span>` : ""}
      </div>
      <h3 style="margin:0 0 6px;font-size:14.5px;line-height:1.45;">${escapeHtml(p.title || "（无标题）")}</h3>
      <div style="font-size:12px;color:var(--ink-muted);display:flex;gap:12px;flex-wrap:wrap;align-items:center;">
        <span>品牌：<strong>${escapeHtml(p.brand || "未知")}</strong></span>
        <span>上架日期：<strong>${escapeHtml(p.online_date || "—")}</strong>${p.online_days !== null ? ` (${formatInt(p.online_days)} 天)` : ""}</span>
        ${(p.amazon_url || p.asin)
          ? `<a href="${escapeHtml(p.amazon_url || `https://www.amazon.com/dp/${encodeURIComponent(p.asin)}`)}" target="_blank" rel="noopener noreferrer">打开 Amazon 前台 ↗</a>`
          : ""
        }
      </div>
    </div>

    <div class="kv-grid">
      <div class="kv-box">
        <div class="kv-label">Listing 近 30 天估算销量</div>
        <div class="kv-val">${formatInt(p.listing_sales_30d)} <span style="font-size:11px;font-weight:500;color:var(--ink-muted)">件 (估算)</span></div>
      </div>
      <div class="kv-box">
        <div class="kv-label">近 30 天估算销售额</div>
        <div class="kv-val">${formatRevenue(p.listing_revenue_30d_usd)}</div>
      </div>
      <div class="kv-box">
        <div class="kv-label">券后售价 / 优惠券</div>
        <div class="kv-val">${formatPrice(p.sales_price_usd, p.price_valid)}${p.coupon_usd ? ` <span style="font-size:11px;color:var(--ok)">券 -$${p.coupon_usd}</span>` : ""}</div>
      </div>
      <div class="kv-box">
        <div class="kv-label">星级 / 评分数</div>
        <div class="kv-val">${formatRatingHtml(p.rating_stars, p.ratings_count, p.rating_status)}</div>
      </div>
      <div class="kv-box">
        <div class="kv-label">大类 BSR (Clothing)</div>
        <div class="kv-val">${p.main_category_bsr !== null ? `#${formatInt(p.main_category_bsr)}` : "—"}</div>
      </div>
      <div class="kv-box">
        <div class="kv-label">Listing 变体数 / 配送</div>
        <div class="kv-val">${p.variation_asin_count !== null ? `${formatInt(p.variation_asin_count)} 变体` : "—"}${p.is_fba === true ? " · FBA" : p.is_fba === false ? " · 非FBA" : ""}</div>
      </div>
    </div>

    <div class="tag-row">
      ${p.has_aplus === true ? `<span class="badge badge-info">含 A+ (${p.ebc_photo_count} 图)</span>` : ""}
      ${p.has_brand_store === true ? `<span class="badge badge-neutral">品牌旗舰店</span>` : ""}
      ${p.has_video === true ? `<span class="badge badge-neutral">含视频</span>` : ""}
      ${p.buybox_seller_address ? `<span class="badge badge-neutral">卖家归属：${escapeHtml(p.buybox_seller_address)}</span>` : ""}
      <span class="badge badge-neutral">采集时间：${formatBeijingTime(p.fetched_at)}</span>
    </div>

    <div>
      <h4 style="margin:0 0 8px;font-size:13px;">来源类目与独立 BSR 观察（不跨类目混排）</h4>
      <div class="table-scroll">
        <table class="data-table" style="min-width:440px;">
          <thead>
            <tr>
              <th>来源类目</th>
              <th>Node ID</th>
              <th class="num-cell">接口位次</th>
              <th class="num-cell">源类目 BSR</th>
              <th>批次</th>
            </tr>
          </thead>
          <tbody>${sourcesHtml}</tbody>
        </table>
      </div>
    </div>

    ${dupClusterHtml}

    ${p.description
      ? `<div>
          <h4 style="margin:0 0 6px;font-size:13px;">商品介绍原文（Description）</h4>
          <div style="font-size:12px;color:var(--ink-secondary);background:var(--bg-elevated);border:1px solid var(--border);border-radius:var(--radius-sm);padding:10px 12px;line-height:1.55;max-height:140px;overflow-y:auto;">${escapeHtml(p.description)}</div>
        </div>`
      : ""
    }

    <div>
      <h4 style="margin:0 0 6px;font-size:13px;">数据质量与口径核验</h4>
      <div>${checksHtml}</div>
    </div>

    <div class="alert alert-info">
      <strong>预处理与印花核图槽位（后续阶段预留）</strong><br>
      当前状态：<span class="mono">${escapeHtml(p.preprocessing_placeholder?.visual_print_status || "pending_manual_review")}</span> — ${escapeHtml(p.preprocessing_placeholder?.label || "待人工核图")}。<br>
      注：第一阶段未执行自动视觉识别、评论正文抓取或版权核验；估算销量属于整个 Listing，不等同于单个印花图案销量。
    </div>
  `;

  body.querySelectorAll("[data-thumb-url]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const url = btn.getAttribute("data-thumb-url");
      const mainBox = document.getElementById("drawer-main-photo");
      if (mainBox) {
        mainBox.innerHTML = renderSafeImage(url, p.title || p.asin, "", true);
      }
      body.querySelectorAll(".drawer-thumb-btn").forEach((b) => b.classList.remove("is-active"));
      btn.classList.add("is-active");
    });
  });

  body.querySelectorAll("[data-sibling-asin]").forEach((tr) => {
    tr.addEventListener("click", () => {
      const targetAsin = tr.getAttribute("data-sibling-asin");
      if (targetAsin && targetAsin !== p.asin) {
        openProductDrawer(targetAsin, p.observed_date || "");
      }
    });
  });
}

export function openMethodologyModal(notes = []) {
  const overlay = document.getElementById("modal-overlay");
  const body = document.getElementById("modal-body");
  if (!overlay || !body) return;

  body.innerHTML = `
    <div class="alert alert-warn">
      <strong>重要声明：</strong>本工作台 V0.1 展示的数据均来自本地只读 SQLite 快照（Sorftime CategoryProducts 美国站首页）。所有指标均附带有效样本分母与口径边界，不虚构商品、不外推全美市场规模、不将单日快照解释为销量增长趋势。
    </div>
    ${notes.map((n, idx) => `
      <div class="panel" style="padding:13px 15px;">
        <div style="font-weight:700;font-size:13.5px;margin-bottom:4px;">${idx + 1}. ${escapeHtml(n.title)}</div>
        <div style="font-size:12.5px;color:var(--ink-secondary);line-height:1.55;">${escapeHtml(n.summary)}</div>
      </div>
    `).join("")}
  `;

  overlay.classList.add("is-open");
  overlay.setAttribute("aria-hidden", "false");
  document.getElementById("modal-close")?.focus();
}

export function closeMethodologyModal() {
  const overlay = document.getElementById("modal-overlay");
  if (!overlay) return;
  overlay.classList.remove("is-open");
  overlay.setAttribute("aria-hidden", "true");
}
