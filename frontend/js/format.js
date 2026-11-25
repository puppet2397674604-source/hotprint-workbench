/**
 * Formatting, escaping, and semantic badge helpers.
 * Never fabricates values for $0 prices, missing ratings, or unverified ASINs.
 */

export function escapeHtml(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export function formatInt(value, fallback = "—") {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return fallback;
  }
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(Number(value));
}

export function formatDecimal(value, digits = 1, fallback = "—") {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return fallback;
  }
  return new Intl.NumberFormat("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(Number(value));
}

export function formatPrice(price, priceValid) {
  if (priceValid && price !== null && price !== undefined && Number(price) > 0) {
    return `$${Number(price).toFixed(2)}`;
  }
  if (price === 0) {
    return `<span class="badge badge-warn" title="源数据返回 0 价格，已从价格统计隔离">$0 异常</span>`;
  }
  return `<span class="badge badge-neutral">售价缺失</span>`;
}

export function formatPriceText(price, priceValid) {
  if (priceValid && price !== null && price !== undefined && Number(price) > 0) {
    return `$${Number(price).toFixed(2)}`;
  }
  if (price === 0) return "$0（异常）";
  return "缺失";
}

export function formatRevenue(rev) {
  if (rev === null || rev === undefined) return "—";
  if (Number(rev) <= 0) return "$0（异常）";
  return `$${new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(Number(rev))}`;
}

export function formatRatingHtml(stars, count, status) {
  if (status !== "valid" || stars === null || stars === undefined) {
    return `<span class="badge badge-warn" title="接口返回 0 或空值，未补值">暂无评分</span>`;
  }
  const cnt = count !== null && count !== undefined ? ` <span class="num" style="color:var(--ink-muted)">(${formatInt(count)})</span>` : "";
  return `<strong class="num">${Number(stars).toFixed(1)}★</strong>${cnt}`;
}

export function formatBeijingTime(isoString) {
  if (!isoString) return "—";
  const dt = new Date(isoString);
  if (Number.isNaN(dt.getTime())) return escapeHtml(isoString);
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(dt) + " (UTC+8)";
  } catch {
    return dt.toISOString();
  }
}

export function renderSourceCategoryBadge(src, { showBsr = false, useFullName = false } = {}) {
  const label = useFullName
    ? (src.source_category || src.name || src.node_id || "未知类目")
    : (src.short_name || src.source_category || src.name || src.node_id || "未知");
  const color = src.color || (src.gender === "women" ? "#D14D72" : src.gender === "men" ? "#0D7C8C" : "#475569");
  const soft = src.soft_color || (src.gender === "women" ? "#FDF2F5" : src.gender === "men" ? "#EEF8FA" : "#F1F5F9");
  const bsrText = showBsr
    ? (src.source_category_bsr !== null && src.source_category_bsr !== undefined
        ? ` #${formatInt(src.source_category_bsr)}`
        : " BSR缺")
    : "";
  const titleParts = [
    `${src.source_category || src.name || ""}${src.node_id ? `（节点 ${src.node_id}）` : ""}`,
  ];
  if (src.result_position !== undefined && src.result_position !== null) {
    titleParts.push(`第 ${src.result_position} 位`);
  }
  if (showBsr) {
    titleParts.push(`源类目 BSR ${src.source_category_bsr ?? "缺失"}（节点内独立排名）`);
  }
  return `<span class="badge badge-cat-chip" style="background:${escapeHtml(soft)};border-color:${escapeHtml(color)}38;" title="${escapeHtml(titleParts.join(" · "))}"><span class="cat-dot" style="background:${escapeHtml(color)};"></span><span>${escapeHtml(label)}${bsrText}</span></span>`;
}

export function renderCategoryBadges(product) {
  const sources = product.source_categories || [];
  const pills = sources.map((src) => renderSourceCategoryBadge(src, { showBsr: true, useFullName: false }));
  if (product.is_cross_category) {
    const cnt = product.cross_category_count || sources.length || 2;
    const label = cnt === 2 ? "双类目" : `跨 ${cnt} 类目`;
    pills.push(`<span class="badge badge-both" title="同一 ASIN 同时出现在 ${cnt} 个目标类目，已去重未累加销量">${label}</span>`);
  }
  return pills.join(" ");
}

export function renderAnomalyBadges(product) {
  const tags = [];
  if ((product.duplicate_cluster_size || 1) > 1) {
    const label = (product.collapsed_duplicate_count || 0) > 0
      ? `合并 ${product.duplicate_cluster_size} 同款`
      : `同款簇(${product.duplicate_cluster_size})`;
    tags.push(`<span class="badge badge-info" title="${escapeHtml(product.duplicate_match_reason || "同品牌+同主图+标题高度一致，未累加销量")}">${label}</span>`);
  }
  if (product.needs_parent_verification) {
    tags.push(`<span class="badge badge-warn" title="ParentAsin 与 Asin 相同，待人工核验">父子同号</span>`);
  }
  if (!product.price_valid) {
    tags.push(`<span class="badge badge-warn">价格异常</span>`);
  }
  if (product.rating_status !== "valid") {
    tags.push(`<span class="badge badge-neutral">无评分</span>`);
  }
  if ((product.anomalies || []).includes("missing_source_bsr")) {
    tags.push(`<span class="badge badge-neutral">缺类目BSR</span>`);
  }
  if (!tags.length) {
    return `<span class="badge badge-ok">字段完整</span>`;
  }
  return tags.join(" ");
}

export function renderCredibilityAnomalyBadges(item, { compact = true } = {}) {
  if (!item) return "";
  const badges = [];
  const cls = compact ? " is-compact" : "";
  if (item.price_anomaly) {
    const pText = item.price !== null && item.price !== undefined
      ? `$${Number(item.price).toFixed(2)}`
      : (item.raw_price_usd !== null && item.raw_price_usd !== undefined ? `$${Number(item.raw_price_usd).toFixed(2)}` : "");
    const tip = `售价异常${pText ? `（${pText} < $5）` : ""}：04 价格维度按缺失重归一化，可信度 -20 分，方向分去极值时不主导峰值（绝不自动排除）`;
    badges.push(
      `<span class="badge badge-cred-anomaly badge-price-anomaly${cls}" data-anomaly-type="price_anomaly" title="${escapeHtml(tip)}">⚠ 价格异常</span>`
    );
  }
  if (item.sales_suspect) {
    const details = Array.isArray(item.sales_suspect_details) ? item.sales_suspect_details : [];
    const detailText = details.length
      ? details
          .slice(0, 2)
          .map((d) => `${d.category_name || d.node_id}: BSR #${formatInt(d.bsr)} / 月销 ${formatInt(d.listing_sales_30d)} (${d.z_score >= 0 ? "+" : ""}${d.z_score}σ)`)
          .join("；")
      : "同源类目 BSR-销量回归残差超过 2σ";
    const tip = `销量存疑（${detailText}）：04 可信度 -15 分，方向分去极值时不主导峰值（绝不自动排除）`;
    badges.push(
      `<span class="badge badge-cred-anomaly badge-sales-suspect${cls}" data-anomaly-type="sales_suspect" title="${escapeHtml(tip)}">⚠ 销量存疑</span>`
    );
  }
  return badges.join(" ");
}

export function renderDirectionAnomalyBadge(dir, { compact = true } = {}) {
  if (!dir) return "";
  const m = dir.metrics || dir;
  const cls = compact ? " is-compact" : "";
  const asins = Array.isArray(m.anomalous_asins) ? m.anomalous_asins : [];
  if (m.all_anomalous) {
    const tip = `方向内全部商品均存在价格异常或销量存疑（${asins.join(", ") || "—"}），方向分已兜底回退取原始最高分，请人工重点核验`;
    return `<span class="badge badge-cred-anomaly badge-all-anomalous${cls}" data-anomaly-type="all_anomalous" title="${escapeHtml(tip)}">⚠ 全员异常兜底</span>`;
  }
  const excluded = Number(m.excluded_anomaly_count || 0);
  if (excluded > 0) {
    const rawPeak = m.raw_peak_product_score !== null && m.raw_peak_product_score !== undefined
      ? formatDecimal(m.raw_peak_product_score, 1)
      : "—";
    const cleanPeak = m.peak_product_score !== null && m.peak_product_score !== undefined
      ? formatDecimal(m.peak_product_score, 1)
      : "—";
    const tip = `计算方向峰值分时已剔除 ${excluded} 个异常商品（${asins.join(", ") || "—"}），峰值由 ${rawPeak} 去极值修正为 ${cleanPeak}`;
    return `<span class="badge badge-cred-trimmed${cls}" data-anomaly-type="outlier_trimmed" title="${escapeHtml(tip)}">已去极值 (-${excluded}异常)</span>`;
  }
  return "";
}

export function renderAiV2Tags(ai, { compact = true } = {}) {
  if (!ai) return "";
  const cls = compact ? " is-compact" : "";
  const pills = [];
  if (ai.season_tag) {
    const label = ai.season_label || ai.season_tag;
    pills.push(
      `<span class="badge badge-v2-season${cls}" data-ai-v2-tag="season" data-ai-v2-value="${escapeHtml(ai.season_tag)}" title="季节标签 (season_tag: ${escapeHtml(ai.season_tag)})">季节: ${escapeHtml(label)}</span>`
    );
  }
  if (ai.motif) {
    const label = ai.motif_label || ai.motif;
    pills.push(
      `<span class="badge badge-v2-motif${cls}" data-ai-v2-tag="motif" data-ai-v2-value="${escapeHtml(ai.motif)}" title="受控图案主体 (motif: ${escapeHtml(ai.motif)})">主体: ${escapeHtml(label)}</span>`
    );
  }
  if (ai.transferability) {
    const label = ai.transferability_label || ai.transferability;
    const trCls = ai.transferability === "form_dependent" ? "badge-v2-transfer-warn" : "badge-v2-transfer";
    pills.push(
      `<span class="badge ${trCls}${cls}" data-ai-v2-tag="transferability" data-ai-v2-value="${escapeHtml(ai.transferability)}" title="跨品类迁移性 (transferability: ${escapeHtml(ai.transferability)})">${escapeHtml(label)}</span>`
    );
  }
  if (!pills.length && (ai.needs_v2_upgrade || ai.prompt_version === "v1")) {
    pills.push(
      `<span class="badge badge-neutral${cls}" data-ai-v2-tag="legacy_v1" title="旧版 v1 缓存记录，下次 AI 预检确认后可增量升级至 v2 标签">v1 缓存（待升级 v2）</span>`
    );
  }
  return pills.join(" ");
}

export function renderSafeImage(url, alt, className = "", eager = false) {
  if (!url) {
    return `<div class="img-fallback"><span>无主图</span></div>`;
  }
  const loading = eager ? "eager" : "lazy";
  return `<img src="${escapeHtml(url)}" alt="${escapeHtml(alt)}" class="${className}" loading="${loading}" referrerpolicy="no-referrer" onerror="this.outerHTML='<div class=&quot;img-fallback&quot;><span>图片加载失败</span></div>'">`;
}
