/**
 * Reusable DeepSeek / OpenAI-compatible AI Configuration Modal & Dry-Run Confirmation Modal.
 * Shared across Stage 02 (AI Visual Purification) and Stage 05 (AI One-Click Clustering).
 */

import { api } from "../api.js";
import { escapeHtml, formatInt } from "../format.js";

function closeModal(force = false) {
  const overlay = document.getElementById("review-modal-overlay");
  if (!overlay) return;
  if (!force && overlay.dataset.locked === "true") return;
  overlay.dataset.locked = "false";
  const closeX = document.getElementById("review-modal-close");
  if (closeX) closeX.disabled = false;
  overlay.classList.remove("is-open");
  overlay.setAttribute("aria-hidden", "true");
}

export async function openAiConfigModal({ onSaved } = {}) {
  const overlay = document.getElementById("review-modal-overlay");
  const body = document.getElementById("review-modal-body");
  const titleEl = document.getElementById("review-modal-title");
  const eyebrowEl = document.getElementById("review-modal-eyebrow");
  if (!overlay || !body || !titleEl) return;

  if (eyebrowEl) eyebrowEl.textContent = "DEEPSEEK VISION · AI CONFIG";
  titleEl.textContent = "DeepSeek 视觉模型配置";
  body.innerHTML = `<div class="loading-state" style="padding:20px;"><div class="spinner"></div><p>读取本地 AI 配置状态…</p></div>`;
  overlay.classList.add("is-open");
  overlay.setAttribute("aria-hidden", "false");

  let cfg;
  try {
    cfg = await api.getAiConfig();
  } catch (err) {
    body.innerHTML = `
      <div class="alert alert-danger">加载配置失败：${escapeHtml(err.message)}</div>
      <div style="display:flex;justify-content:flex-end;">
        <button type="button" class="btn btn-secondary" id="ai-cfg-close">关闭</button>
      </div>
    `;
    document.getElementById("ai-cfg-close")?.addEventListener("click", closeModal);
    return;
  }

  const renderForm = (cur) => {
    body.innerHTML = `
      <div class="alert alert-info" style="margin:0;font-size:12px;">
        默认对接 DeepSeek 官方多模态接口（<span class="mono">deepseek-flash</span>，原生支持图片 URL 看图，兼容任意 OpenAI 格式服务商）。
        API Key 仅保存在已被 <span class="mono">.gitignore</span> 忽略的本地文件 <span class="mono">data/ai_config.local</span>，严禁明文回传或写入日志。
      </div>

      <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;padding:9px 12px;border-radius:var(--radius-sm);background:var(--bg-elevated);border:1px solid var(--border);">
        <span style="font-size:12.5px;color:var(--ink-secondary);">当前密钥状态</span>
        <div style="display:flex;align-items:center;gap:8px;">
          ${cur.has_api_key
            ? `<span class="badge badge-ok">已配置</span><span class="mono" style="font-size:12px;font-weight:700;">${escapeHtml(cur.api_key_masked)}</span>`
            : `<span class="badge badge-warn">未配置 API Key</span>`
          }
        </div>
      </div>

      <label class="field">
        <span class="field-label">API Base URL</span>
        <input type="url" id="ai-cfg-base-url" class="input-control mono"
          value="${escapeHtml(cur.base_url || cur.default_base_url || "https://api.deepseek.com/v1")}"
          placeholder="https://api.deepseek.com/v1">
      </label>

      <label class="field">
        <span class="field-label">模型名称（默认 deepseek-flash）</span>
        <input type="text" id="ai-cfg-model" class="input-control mono"
          value="${escapeHtml(cur.model || cur.default_model || "deepseek-flash")}"
          placeholder="deepseek-flash">
      </label>

      <label class="field">
        <span class="field-label">API Key${cur.has_api_key ? "（留空则保持当前已保存密钥）" : " *"}</span>
        <input type="password" id="ai-cfg-api-key" class="input-control mono"
          autocomplete="off" spellcheck="false"
          placeholder="${cur.has_api_key ? "输入新 Key 以覆盖，留空保持不变" : "粘贴 DeepSeek API Key（sk-...）"}">
      </label>

      <div id="ai-cfg-feedback"></div>

      <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;padding-top:4px;">
        <div style="display:flex;gap:6px;">
          <button type="button" class="btn btn-secondary btn-sm" id="ai-cfg-test">测试连通性</button>
          ${cur.has_api_key
            ? `<button type="button" class="btn btn-ghost btn-sm" style="color:var(--danger);" id="ai-cfg-clear">清除 Key</button>`
            : ""
          }
        </div>
        <div style="display:flex;gap:8px;">
          <button type="button" class="btn btn-secondary" id="ai-cfg-cancel">关闭</button>
          <button type="button" class="btn btn-primary" id="ai-cfg-save">保存配置</button>
        </div>
      </div>
    `;

    const setFeedback = (type, msg) => {
      const box = document.getElementById("ai-cfg-feedback");
      if (!box) return;
      box.innerHTML = msg
        ? `<div class="alert ${type === "error" ? "alert-danger" : type === "ok" ? "alert-ok" : "alert-info"}" style="margin:0;">${escapeHtml(msg)}</div>`
        : "";
    };

    const readInputs = () => ({
      base_url: (document.getElementById("ai-cfg-base-url")?.value || "").trim(),
      model: (document.getElementById("ai-cfg-model")?.value || "").trim(),
      api_key: (document.getElementById("ai-cfg-api-key")?.value || "").trim(),
    });

    document.getElementById("ai-cfg-cancel")?.addEventListener("click", closeModal);

    document.getElementById("ai-cfg-save")?.addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      setFeedback("info", "正在保存配置…");
      try {
        const res = await api.saveAiConfig(readInputs());
        if (onSaved) onSaved(res.config);
        renderForm(res.config);
        setFeedback("ok", `配置已保存（模型：${res.config.model}，密钥状态：${res.config.has_api_key ? res.config.api_key_masked : "未配置"}）。`);
      } catch (err) {
        setFeedback("error", err.message);
      } finally {
        btn.disabled = false;
      }
    });

    document.getElementById("ai-cfg-test")?.addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      setFeedback("info", "正在发起最小 Token 连通性测试…");
      try {
        const res = await api.testAiConfig(readInputs());
        setFeedback("ok", res.message || "连通性测试通过。");
      } catch (err) {
        setFeedback("error", `连通失败：${err.message}`);
      } finally {
        btn.disabled = false;
      }
    });

    document.getElementById("ai-cfg-clear")?.addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      try {
        const res = await api.clearAiApiKey();
        if (onSaved) onSaved(res.config);
        renderForm(res.config);
        setFeedback("ok", "已清除本地保存的 API Key。");
      } catch (err) {
        setFeedback("error", err.message);
      } finally {
        btn.disabled = false;
      }
    });
  };

  renderForm(cfg);
}

export function estimateAiDurationText(callCount, { isCluster = false } = {}) {
  const n = Number(callCount || 0);
  if (n <= 0) {
    return isCluster
      ? "正在调用 DeepSeek 文本归纳聚类模型（预计约 5~12 秒，请勿刷新页面）…"
      : "正在刷新本地缓存统计（预计 1~2 秒）…";
  }
  if (n <= 15) {
    return `正在 8 线程并发调用 DeepSeek 视觉模型（共 ${formatInt(n)} 件，预计约 4~12 秒，请勿刷新页面）…`;
  }
  if (n <= 55) {
    return `正在 8 线程并发调用 DeepSeek 视觉模型（共 ${formatInt(n)} 件，预计约 10~25 秒，请勿刷新页面）…`;
  }
  if (n <= 120) {
    return `正在 8 线程并发调用 DeepSeek 视觉模型（共 ${formatInt(n)} 件，预计约 25~55 秒，请勿刷新页面）…`;
  }
  return `正在 8 线程并发调用 DeepSeek 视觉模型（共 ${formatInt(n)} 件，预计约 1~2 分钟，请勿刷新页面）…`;
}

export function buildAiDryRunSummaryHtml(preview, { mode = "purify", topN = 60, interactivePurify = false } = {}) {
  const newUncached = Number(preview.new_uncached_count ?? preview.uncached_count ?? preview.uncached_image_calls ?? 0);
  const upgradeV1 = Number(preview.upgrade_v1_count ?? 0);
  const cachedV2 = Number(preview.cached_count ?? 0);
  const pv = preview.prompt_version || "v2";
  const model = preview.model || "deepseek-flash";

  if (mode === "cluster") {
    const imgCalls = Number(preview.uncached_image_calls ?? (newUncached + upgradeV1));
    const reduceCalls = Number(preview.reduce_calls ?? 0);
    const total = Number(preview.candidate_count ?? 0);
    return `
      <div class="kv-grid" style="grid-template-columns:repeat(3, minmax(0, 1fr));">
        <div class="kv-box">
          <div class="kv-label">全新未缓存商品</div>
          <div class="kv-val" style="color:var(--accent);font-size:19px;">${formatInt(newUncached)} <span style="font-size:11.5px;font-weight:500;color:var(--ink-muted);">件</span></div>
        </div>
        <div class="kv-box">
          <div class="kv-label">旧版 v1 需升级重跑</div>
          <div class="kv-val" style="color:${upgradeV1 > 0 ? "var(--warn)" : "var(--ink-muted)"};font-size:19px;">${formatInt(upgradeV1)} <span style="font-size:11.5px;font-weight:500;color:var(--ink-muted);">件</span></div>
        </div>
        <div class="kv-box">
          <div class="kv-label">已命中 ${escapeHtml(pv)} 缓存</div>
          <div class="kv-val" style="color:var(--ok);font-size:19px;">${formatInt(cachedV2)} <span style="font-size:11.5px;font-weight:500;color:var(--ink-muted);">/ ${formatInt(total)}</span></div>
        </div>
      </div>
      <div style="font-size:12.5px;color:var(--ink-secondary);line-height:1.55;">
        Step 1 看图补齐共需调用 <strong>${formatInt(imgCalls)}</strong> 次（全新 ${formatInt(newUncached)} + v1 升级 ${formatInt(upgradeV1)}），Step 2 文本归纳聚类 <strong>${formatInt(reduceCalls)}</strong> 次。<br>
        模型：<span class="mono" style="font-weight:700;">${escapeHtml(model)}</span> · Prompt <span class="mono">${escapeHtml(pv)}</span>（新增季节/主体/迁移性标签，支持一图多组，严禁“其他/杂项”分组）。
        ${upgradeV1 > 0 ? `<br><strong style="color:var(--warn);">提示：含 ${formatInt(upgradeV1)} 件旧版 v1 缓存，确认后将增量升级至 ${escapeHtml(pv)} 并持久化。</strong>` : ""}
      </div>
    `;
  }

  const willCall = Number(preview.will_analyze_count ?? (newUncached + upgradeV1));
  const total = Number(preview.total_candidates ?? 0);
  const skippedNoImg = Number(preview.skipped_missing_image_count ?? 0);
  const skippedSuspected = Number(preview.skipped_suspected_non_print_count ?? 0);
  const availSuspected = Number(preview.available_suspected_non_print_count ?? skippedSuspected);
  const skipSuspectedChecked = preview.skip_suspected_non_print !== false;
  const curLimit = preview.limit === null || preview.limit === undefined ? "all" : String(preview.limit);
  const deferredByLimit = Number(preview.deferred_by_limit_count ?? 0);

  const estHint = willCall <= 0
    ? "0 秒（全部命中缓存）"
    : willCall <= 15
      ? "约 4~12 秒（8 线程并发）"
      : willCall <= 55
        ? "约 10~25 秒（8 线程并发）"
        : willCall <= 120
          ? "约 25~55 秒（8 线程并发）"
          : "约 1~2 分钟（8 线程并发）";

  const interactiveControlsHtml = interactivePurify
    ? `
      <div style="padding:10px 12px;border-radius:var(--radius-sm);background:var(--bg-elevated);border:1px solid var(--border);display:flex;flex-direction:column;gap:8px;">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;">
          <span style="font-size:12.5px;font-weight:700;color:var(--ink-primary);">单批看图上限（按销量 / 榜单排名热度优先截取）</span>
          <div class="segmented" role="group" aria-label="选择单次 AI 提纯件数上限">
            ${[
              { val: "50", label: "50 件" },
              { val: "100", label: "100 件" },
              { val: "200", label: "200 件" },
              { val: "all", label: "全部" },
            ]
              .map(
                (opt) => `
                  <button type="button" class="seg-btn ${curLimit === opt.val ? "is-active" : ""}" data-purify-limit="${opt.val}">${opt.label}</button>
                `
              )
              .join("")}
          </div>
        </div>
        <label style="display:flex;align-items:flex-start;gap:8px;font-size:12px;color:var(--ink-secondary);cursor:pointer;">
          <input type="checkbox" id="ai-purify-skip-suspected" style="margin-top:2px;" ${skipSuspectedChecked ? "checked" : ""}>
          <span>默认跳过已被文本规则标为「疑似非印花（纯色/胚衣）」的 <strong>${formatInt(availSuspected)}</strong> 件商品（推荐先在列表按品牌免费批量排除，省下看图 API 额度）</span>
        </label>
      </div>
    `
    : "";

  const skipNoticeParts = [];
  if (skippedNoImg > 0 || interactivePurify) {
    skipNoticeParts.push(`已自动跳过 <strong>${formatInt(skippedNoImg)}</strong> 件缺主图商品`);
  }
  if (skippedSuspected > 0) {
    skipNoticeParts.push(`已跳过 <strong>${formatInt(skippedSuspected)}</strong> 件疑似非印花商品`);
  }
  if (deferredByLimit > 0) {
    skipNoticeParts.push(`按热度优先截取前 <strong>${formatInt(willCall)}</strong> 件（其余 ${formatInt(deferredByLimit)} 件留待下批）`);
  }

  return `
    ${interactiveControlsHtml}
    <div class="kv-grid" style="grid-template-columns:repeat(3, minmax(0, 1fr));">
      <div class="kv-box">
        <div class="kv-label">本次实际待调用</div>
        <div class="kv-val" style="color:var(--accent);font-size:19px;">${formatInt(willCall)} <span style="font-size:11.5px;font-weight:500;color:var(--ink-muted);">件</span></div>
      </div>
      <div class="kv-box">
        <div class="kv-label">未缓存池（新 / v1）</div>
        <div class="kv-val" style="color:${upgradeV1 > 0 ? "var(--warn)" : "var(--ink-primary)"};font-size:19px;">${formatInt(newUncached)} <span style="font-size:11.5px;font-weight:500;color:var(--ink-muted);">/ ${formatInt(upgradeV1)}</span></div>
      </div>
      <div class="kv-box">
        <div class="kv-label">已命中 ${escapeHtml(pv)} 缓存</div>
        <div class="kv-val" style="color:var(--ok);font-size:19px;">${formatInt(cachedV2)} <span style="font-size:11.5px;font-weight:500;color:var(--ink-muted);">/ ${formatInt(total)}</span></div>
      </div>
    </div>
    ${skipNoticeParts.length
      ? `<div class="alert alert-info" style="margin:0;padding:8px 11px;font-size:12px;">防浪费过滤：${skipNoticeParts.join(" · ")}。</div>`
      : ""
    }
    <div style="font-size:12.5px;color:var(--ink-secondary);line-height:1.55;">
      本次共需调用看图 API <strong>${formatInt(willCall)}</strong> 次 · 预计耗时：<strong>${estHint}</strong>。<br>
      模型：<span class="mono" style="font-weight:700;">${escapeHtml(model)}</span> · Prompt <span class="mono">${escapeHtml(pv)}</span> · 分辨率：<span class="mono">detail: "low"</span>（单次看图产出 02 提纯判定 + 季节/主体/迁移性 v2 标签并永久缓存）。
      ${willCall === 0 ? `<br><strong style="color:var(--ok);">当前范围内所有商品均已命中本地 ${escapeHtml(pv)} 缓存，本次无需产生新的模型费用。</strong>` : ""}
      ${upgradeV1 > 0 ? `<br><strong style="color:var(--warn);">提示：含 ${formatInt(upgradeV1)} 件 v1 旧缓存商品，仅在点击下方确认后才会发起增量升级重跑。</strong>` : ""}
    </div>
  `;
}

export function openAiDryRunModal({
  eyebrowText = "DEEPSEEK VISION · DRY-RUN PREVIEW",
  title,
  summaryHtml,
  warningText = "",
  confirmLabel = "确认开始执行",
  runningBtnLabel = "AI 执行中…",
  progressText = "",
  estimatedCalls = 0,
  isCluster = false,
  canConfirm = true,
  missingKey = false,
  onOpenConfig,
  onAfterRender,
  onConfirm,
}) {
  const overlay = document.getElementById("review-modal-overlay");
  const body = document.getElementById("review-modal-body");
  const titleEl = document.getElementById("review-modal-title");
  const eyebrowEl = document.getElementById("review-modal-eyebrow");
  if (!overlay || !body || !titleEl) return;

  if (eyebrowEl) eyebrowEl.textContent = eyebrowText;
  titleEl.textContent = title;
  body.innerHTML = `
    ${summaryHtml}

    ${warningText
      ? `<div class="alert alert-warn" style="margin:0;font-size:12px;">${escapeHtml(warningText)}</div>`
      : ""
    }

    ${missingKey
      ? `<div class="alert alert-danger" style="margin:0;font-size:12.5px;">
          尚未配置 DeepSeek API Key，请先完成配置后再发起真实视觉调用。
          <div style="margin-top:8px;">
            <button type="button" class="btn btn-secondary btn-sm" id="ai-dryrun-open-cfg">立即前往 AI 配置</button>
          </div>
        </div>`
      : ""
    }

    <div id="ai-dryrun-progress"></div>

    <div style="display:flex;justify-content:flex-end;gap:8px;padding-top:4px;">
      <button type="button" class="btn btn-secondary" id="ai-dryrun-cancel">取消</button>
      <button type="button" class="btn btn-primary" id="ai-dryrun-confirm" ${(!canConfirm || missingKey) ? "disabled" : ""}>
        ${escapeHtml(confirmLabel)}
      </button>
    </div>
  `;

  overlay.classList.add("is-open");
  overlay.setAttribute("aria-hidden", "false");

  if (typeof onAfterRender === "function") {
    onAfterRender(body);
  }

  document.getElementById("ai-dryrun-cancel")?.addEventListener("click", closeModal);
  document.getElementById("ai-dryrun-open-cfg")?.addEventListener("click", () => {
    if (onOpenConfig) onOpenConfig();
  });

  document.getElementById("ai-dryrun-confirm")?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    const cancelBtn = document.getElementById("ai-dryrun-cancel");
    const closeX = document.getElementById("review-modal-close");
    const prog = document.getElementById("ai-dryrun-progress");
    const effectiveProgressText = progressText || estimateAiDurationText(estimatedCalls, { isCluster });
    overlay.dataset.locked = "true";
    btn.disabled = true;
    if (cancelBtn) cancelBtn.disabled = true;
    if (closeX) closeX.disabled = true;
    btn.textContent = runningBtnLabel;
    if (prog) {
      prog.innerHTML = `
        <div class="alert alert-info" style="margin:0;display:flex;align-items:center;gap:10px;">
          <div class="spinner" style="width:18px;height:18px;border-width:2px;flex-shrink:0;"></div>
          <span>${escapeHtml(effectiveProgressText)}</span>
        </div>
      `;
    }
    try {
      await onConfirm();
      overlay.dataset.locked = "false";
      if (closeX) closeX.disabled = false;
      closeModal(true);
    } catch (err) {
      overlay.dataset.locked = "false";
      if (closeX) closeX.disabled = false;
      if (prog) {
        prog.innerHTML = `<div class="alert alert-danger" style="margin:0;"><strong>执行失败：</strong>${escapeHtml(err.message || "未知错误")}</div>`;
      }
      btn.disabled = false;
      btn.textContent = "重试执行";
      if (cancelBtn) cancelBtn.disabled = false;
    }
  });
}
