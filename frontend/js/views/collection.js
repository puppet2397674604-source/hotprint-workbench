/**
 * Stage 01: Cloud Data Sync & Team State Hub (Supabase Read-Only Sync + select_* State Sync).
 */
import { escapeHtml, formatBeijingTime, formatInt } from "../format.js";

const ICON_CLOUD_SYNC = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 13v8"/><path d="m8 17 4 4 4-4"/><path d="M20.39 18.39A5 5 0 0 0 18 9h-1.26A8 8 0 1 0 3 16.3"/></svg>`;
const ICON_REFRESH = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16"/><path d="M16 16h5v5"/></svg>`;
const ICON_SETTINGS = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>`;
const ICON_ARROW_RIGHT = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14"/><path d="m12 5 7 7-7 7"/></svg>`;

function renderTeamStatusBadge(hpStatus, hasApiKey) {
  if (!hpStatus) {
    return `<span class="badge badge-neutral" id="hp-status-badge">状态检测中…</span>`;
  }
  if (hpStatus.initialized) {
    return `<span class="badge badge-ok" id="hp-status-badge">已就绪</span>`;
  }
  if (!hasApiKey) {
    return `<span class="badge badge-neutral" id="hp-status-badge">待配置密钥</span>`;
  }
  if (hpStatus.check_error) {
    return `<span class="badge badge-warn" id="hp-status-badge" title="${escapeHtml(hpStatus.check_error)}">连接待核实</span>`;
  }
  return `<span class="badge badge-warn" id="hp-status-badge">待初始化云端表</span>`;
}

function getBatchCategoryLabel(batch, catMap) {
  const cat = catMap.get(String(batch.node_id || ""));
  if (cat) return { name: cat.name, color: cat.color || "var(--accent)" };
  if (batch.category_name) return { name: batch.category_name, color: "var(--ink-muted)" };
  if (batch.source === "supabase") return { name: "全量云端同步", color: "var(--accent)" };
  return { name: batch.node_id || "未知类目", color: "var(--ink-muted)" };
}

export function renderCollectionView(
  container,
  { config, batchesResp, hpStatus, form, settingsOpen, batchesExpanded, submitting, handlers }
) {
  if (!config) {
    container.innerHTML = `<div class="loading-state"><div class="spinner"></div><p>正在加载云端同步状态…</p></div>`;
    return;
  }

  const sb = config.supabase || {};
  const catStates = config.category_states || [];
  const catMap = new Map(catStates.map((c) => [String(c.node_id || ""), c]));
  const totalCats = catStates.length || 6;
  const syncedTodayCount = catStates.filter((c) => c.collected_today).length;

  const batches = (batchesResp && batchesResp.batches) || [];
  const visibleBatches = batchesExpanded ? batches : batches.slice(0, 5);
  const hiddenBatchCount = Math.max(0, batches.length - 5);

  const sq = config.snapshot_quality || null;
  const uniqueAsins = sq?.unique_asin_count ?? 0;
  const cleanAsins = sq?.clean_asin_count ?? 0;
  const warningIssues = (sq?.checks || [])
    .filter((chk) => chk.severity === "warning")
    .reduce((sum, chk) => sum + (Number(chk.count) || 0), 0);

  const cloudRows = hpStatus?.total_cloud_rows ?? 0;
  const localRows = hpStatus?.total_local_rows ?? 0;
  const isSettingsExpanded = settingsOpen === null ? !sb.has_api_key : Boolean(settingsOpen);
  const targetDate = form?.observed_date || config.beijing_today || "";

  let teamKpiVal = "检测中";
  let teamKpiSub = "正在校验云端协作表";
  if (hpStatus) {
    if (hpStatus.initialized) {
      teamKpiVal = "已就绪";
      teamKpiSub = `云端 ${formatInt(cloudRows)} 条 · 本地 ${formatInt(localRows)} 条`;
    } else if (!sb.has_api_key) {
      teamKpiVal = "待配置";
      teamKpiSub = `本地已有 ${formatInt(localRows)} 条状态`;
    } else {
      teamKpiVal = "待建表";
      teamKpiSub = `本地 ${formatInt(localRows)} 条 · 需初始化`;
    }
  }

  container.innerHTML = `
    <!-- 1. Compact 3-Card Executive KPI Strip -->
    <section class="col-kpi-grid" aria-label="云端同步核心指标">
      <article class="kpi-card">
        <div class="kpi-top">
          <span class="kpi-label">今日类目覆盖</span>
        </div>
        <div class="kpi-value">${syncedTodayCount} <span style="font-size:15px;font-weight:500;color:var(--ink-muted);">/ ${totalCats} 类目</span></div>
        <div class="kpi-sub">目标日期 ${escapeHtml(config.beijing_today)}</div>
      </article>

      <article class="kpi-card">
        <div class="kpi-top">
          <span class="kpi-label">最近快照商品规模</span>
        </div>
        <div class="kpi-value">${formatInt(uniqueAsins)} <span style="font-size:15px;font-weight:500;color:var(--ink-muted);">件</span></div>
        <div class="kpi-sub">来源：云端只读同步（0 费用）</div>
      </article>

      <article class="kpi-card">
        <div class="kpi-top">
          <span class="kpi-label">团队协作状态</span>
        </div>
        <div class="kpi-value" style="font-size:24px;line-height:1.28;">${teamKpiVal}</div>
        <div class="kpi-sub">${teamKpiSub}</div>
      </article>
    </section>

    <!-- 2. Primary Hero Panel: Cloud Data Sync + Team State + Collapsible Settings -->
    <section class="panel col-sync-hero" id="supabase-sync-panel" aria-label="云端数据同步与团队状态">
      <div class="col-sync-main">
        <div class="col-sync-copy">
          <h2 class="panel-title">云端数据同步</h2>
          <p class="panel-subtitle">从 Supabase 云端只读拉取最新商品快照到本地工作库，并自动刷新 02 数据预处理（不触发任何外部 AI 调用）。</p>
        </div>

        <div class="col-sync-actions">
          <div class="col-date-field">
            <label for="supabase-sync-date">观察日期</label>
            <input type="date" id="supabase-sync-date" value="${escapeHtml(targetDate)}">
          </div>
          <button type="button" class="btn btn-primary btn-sync-primary" id="btn-supabase-sync" ${submitting || !sb.has_api_key ? "disabled" : ""}>
            ${ICON_CLOUD_SYNC}
            <span>同步云端数据（先预检）</span>
          </button>
          <button type="button" class="btn btn-secondary btn-sync-primary" id="btn-jump-prep-top">
            <span>前往 02 预处理</span>
            ${ICON_ARROW_RIGHT}
          </button>
        </div>
      </div>

      <!-- Team Cloud State Bar (select_* tables) -->
      <div class="col-team-bar" id="supabase-hp-bar">
        <div class="col-team-meta">
          <span class="col-team-title">云端业务表（select_*）</span>
          ${renderTeamStatusBadge(hpStatus, sb.has_api_key)}
          <span>${hpStatus?.initialized ? `云端 ${formatInt(cloudRows)} 条 · 本地 ${formatInt(localRows)} 条` : `本地 ${formatInt(localRows)} 条审阅与方向记录`}</span>
        </div>

        <div class="col-team-actions">
          <button type="button" class="btn btn-ghost btn-sm" id="btn-hp-refresh" aria-label="刷新团队云端状态" title="重新检测云端业务表（select_*）状态">
            ${ICON_REFRESH}
          </button>
          <button type="button" class="btn btn-secondary btn-sm" id="btn-hp-push" ${hpStatus?.initialized ? "" : "disabled"}>
            推送本地状态
          </button>
          <button type="button" class="btn btn-secondary btn-sm" id="btn-hp-pull" ${hpStatus?.initialized ? "" : "disabled"}>
            拉取团队最新
          </button>
          <button type="button" class="btn btn-ghost btn-sm" id="btn-toggle-sb-settings" aria-expanded="${isSettingsExpanded ? "true" : "false"}" aria-controls="supabase-settings-drawer">
            ${ICON_SETTINGS}
            <span>${isSettingsExpanded ? "收起数据源设置" : "数据源设置"}</span>
          </button>
        </div>
      </div>

      <!-- Collapsible One-Time Infrastructure Configuration -->
      <div class="col-settings-drawer" id="supabase-settings-drawer" ${isSettingsExpanded ? "" : "hidden"}>
        <div class="col-settings-grid">
          <div class="col-settings-field">
            <label for="supabase-url-input">云端服务地址（Project URL）</label>
            <input type="url" id="supabase-url-input" class="input-control mono"
              value="${escapeHtml(sb.url || sb.default_url || "https://oyckbvstitrmpvnvityb.supabase.co")}"
              placeholder="https://your-project.supabase.co" autocomplete="off" spellcheck="false">
          </div>

          <div class="col-settings-field">
            <label for="supabase-key-input">访问密钥（${sb.has_api_key ? `已保存 ${escapeHtml(sb.api_key_masked || "****")}` : "未配置"}）</label>
            <input type="password" id="supabase-key-input" class="input-control mono"
              placeholder="${sb.has_api_key ? "留空保持现有密钥，输入新值可覆盖" : "粘贴 Supabase API Key（仅存本地）"}"
              autocomplete="off" spellcheck="false">
          </div>

          <div class="col-settings-btns">
            <button type="button" class="btn btn-secondary" id="btn-supabase-save">保存配置</button>
            <button type="button" class="btn btn-secondary" id="btn-supabase-test">测试连接</button>
            ${sb.can_clear_local_key
              ? `<button type="button" class="btn btn-danger-ghost" id="btn-supabase-clear" data-confirm-armed="0">清除 Key</button>`
              : ""
            }
          </div>
        </div>

        <div class="col-settings-footer">
          <span>密钥仅保存在本机配置文件，不写入代码库；上游主数据表严格只读。</span>
          <button type="button" class="btn btn-ghost btn-sm" id="btn-hp-copy-sql">复制云端业务表建表 SQL</button>
        </div>
      </div>

      <div id="supabase-feedback" class="col-feedback" role="status" aria-live="polite"></div>
    </section>

    <!-- 3. Single-Line Snapshot Health Summary Strip -->
    ${sq
      ? `<section class="panel col-health-strip" aria-label="最近快照健康度">
          <div class="col-health-metrics">
            <span>最近快照 <strong>${escapeHtml(sq.selected_date || "—")}</strong></span>
            <span class="col-health-sep" aria-hidden="true">|</span>
            <span>去重商品 <strong>${formatInt(uniqueAsins)}</strong> 件</span>
            <span class="col-health-sep" aria-hidden="true">|</span>
            <span>字段完备 <strong>${formatInt(cleanAsins)}</strong> 件</span>
            <span class="col-health-sep" aria-hidden="true">|</span>
            <span>待核实异常项 <strong>${formatInt(warningIssues)}</strong> 处</span>
          </div>
          <button type="button" class="btn btn-ghost btn-sm" id="btn-jump-prep-health">
            <span>前往 02 核验明细</span>
            ${ICON_ARROW_RIGHT}
          </button>
        </section>`
      : ""
    }

    <!-- 4. Single Full-Width Core 6-Category Coverage Table -->
    <section class="panel" aria-label="核心类目同步覆盖">
      <div class="panel-header">
        <div>
          <h2 class="panel-title">核心 6 大服饰类目覆盖</h2>
          <p class="panel-subtitle">云端同步后自动归类至 6 个主选品类目，供后续预处理与候选评分使用。</p>
        </div>
      </div>

      <div class="table-scroll">
        <table class="data-table col-table">
          <thead>
            <tr>
              <th>目标类目</th>
              <th>款式 / 性别</th>
              <th>今日状态（${escapeHtml(config.beijing_today)}）</th>
              <th class="num-cell">今日样本数</th>
              <th>最近同步时间</th>
            </tr>
          </thead>
          <tbody>
            ${catStates.map((cat) => `
              <tr>
                <td>
                  <div class="col-cat-cell">
                    <span class="cat-dot" style="background:${escapeHtml(cat.color || "var(--accent)")}"></span>
                    <span>${escapeHtml(cat.name)}</span>
                  </div>
                </td>
                <td style="color:var(--ink-secondary);">${escapeHtml(cat.garment_label || cat.garment_type || "—")} · ${escapeHtml(cat.gender_label || cat.gender || "—")}</td>
                <td>
                  <span class="col-status-dot ${cat.collected_today ? "is-ok" : "is-muted"}">
                    ${cat.collected_today ? "今日已同步" : "今日未同步"}
                  </span>
                </td>
                <td class="num-cell">${cat.today_product_count ? formatInt(cat.today_product_count) : "—"}</td>
                <td style="color:var(--ink-secondary);">${formatBeijingTime(cat.last_fetched_at)}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
    </section>

    <!-- 5. Streamlined Historical Sync Batches (Latest 5 + Expand) -->
    <section class="panel" aria-label="历史同步批次">
      <div class="panel-header">
        <div>
          <h2 class="panel-title">历史同步批次</h2>
          <p class="panel-subtitle">共 ${formatInt(batches.length)} 批记录${!batchesExpanded && hiddenBatchCount > 0 ? `，当前仅展示最近 5 批` : ""}。</p>
        </div>
        ${hiddenBatchCount > 0
          ? `<button type="button" class="btn btn-ghost btn-sm" id="btn-toggle-all-batches">
              ${batchesExpanded ? "收起至最近 5 批" : `展开全部 (${formatInt(batches.length)} 批)`}
            </button>`
          : ""
        }
      </div>

      ${batches.length === 0
        ? `<p style="margin:0;font-size:13px;color:var(--ink-muted);">暂无历史同步记录，点击上方「同步云端数据」即可拉取首份快照。</p>`
        : `<div class="table-scroll">
            <table class="data-table col-table">
              <thead>
                <tr>
                  <th>观察日期</th>
                  <th>同步时间 (UTC+8)</th>
                  <th>覆盖类目</th>
                  <th class="num-cell">有效商品数</th>
                  <th>结果</th>
                </tr>
              </thead>
              <tbody>
                ${visibleBatches.map((b) => {
                  const catInfo = getBatchCategoryLabel(b, catMap);
                  const isOk = b.status === "succeeded";
                  return `
                    <tr>
                      <td class="mono">${escapeHtml(b.observed_date)}</td>
                      <td style="color:var(--ink-secondary);">${formatBeijingTime(b.fetched_at)}</td>
                      <td>
                        <div class="col-cat-cell">
                          <span class="cat-dot" style="background:${escapeHtml(catInfo.color)}"></span>
                          <span>${escapeHtml(catInfo.name)}</span>
                        </div>
                      </td>
                      <td class="num-cell"><strong>${formatInt(b.product_count)}</strong></td>
                      <td>
                        <span class="col-status-dot ${isOk ? "is-ok" : "is-warn"}">
                          ${isOk ? "已完成" : "异常"}
                        </span>
                      </td>
                    </tr>
                  `;
                }).join("")}
              </tbody>
            </table>
          </div>`
      }
    </section>
  `;

  // Feedback helper
  const sbFeedback = container.querySelector("#supabase-feedback");
  const setFeedback = (msg, tone = "") => {
    if (!sbFeedback) return;
    sbFeedback.className = `col-feedback${tone ? ` is-${tone}` : ""}`;
    sbFeedback.textContent = msg;
  };

  // Explicit observation date selector
  const dateInput = container.querySelector("#supabase-sync-date");
  if (dateInput) {
    dateInput.addEventListener("change", () => {
      handlers.onSyncDateChange(dateInput.value);
    });
  }

  // Primary CTA: Supabase Read-Only Cloud Sync (dry-run preview first)
  const btnSync = container.querySelector("#btn-supabase-sync");
  if (btnSync) {
    btnSync.addEventListener("click", async () => {
      const observedDate = dateInput?.value || config.beijing_today;
      btnSync.disabled = true;
      setFeedback("正在拉取云端数据预检统计（严格只读，零 AI 调用）…");
      try {
        await handlers.onSyncSupabase({ observedDate });
      } catch (err) {
        setFeedback(`云端同步预检失败：${err.message}`, "danger");
      } finally {
        btnSync.disabled = !sb.has_api_key;
      }
    });
  }

  // Quick jumps to Stage 02 Preprocessing
  const jumpToPrep = () => {
    handlers.onJumpAfterCollection("preprocessing", sq?.selected_date || dateInput?.value || config.beijing_today);
  };
  container.querySelector("#btn-jump-prep-top")?.addEventListener("click", jumpToPrep);
  container.querySelector("#btn-jump-prep-health")?.addEventListener("click", jumpToPrep);

  // Collapsible settings drawer toggle
  container.querySelector("#btn-toggle-sb-settings")?.addEventListener("click", () => {
    handlers.onToggleSupabaseSettings();
  });

  // Expand / collapse history batches
  container.querySelector("#btn-toggle-all-batches")?.addEventListener("click", () => {
    handlers.onToggleBatchesExpanded();
  });

  // Save Supabase config (also bound to Enter on API key input)
  const urlInput = container.querySelector("#supabase-url-input");
  const keyInput = container.querySelector("#supabase-key-input");
  const btnSave = container.querySelector("#btn-supabase-save");
  const handleSaveConfig = async () => {
    const url = (urlInput?.value || "").trim();
    const apiKey = (keyInput?.value || "").trim();
    if (!url && !apiKey) {
      setFeedback("请输入云端服务地址或访问密钥。", "warn");
      return;
    }
    if (btnSave) btnSave.disabled = true;
    try {
      await handlers.onSaveSupabaseConfig({ url, api_key: apiKey });
    } catch (err) {
      setFeedback(`保存配置失败：${err.message}`, "danger");
    } finally {
      if (btnSave) btnSave.disabled = false;
    }
  };
  btnSave?.addEventListener("click", handleSaveConfig);
  keyInput?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      handleSaveConfig();
    }
  });

  // Test Supabase connection
  const btnTest = container.querySelector("#btn-supabase-test");
  btnTest?.addEventListener("click", async () => {
    const url = (urlInput?.value || "").trim();
    const apiKey = (keyInput?.value || "").trim();
    btnTest.disabled = true;
    setFeedback("正在测试云端只读连接…");
    try {
      const res = await handlers.onTestSupabaseConfig({ url, api_key: apiKey });
      setFeedback(`连接正常：${res.message || "云端服务可读"}`, "ok");
    } catch (err) {
      setFeedback(`连接失败：${err.message}`, "danger");
    } finally {
      btnTest.disabled = false;
    }
  });

  // Two-step inline confirmation for clearing Supabase API Key
  const btnClear = container.querySelector("#btn-supabase-clear");
  if (btnClear) {
    let resetTimer = null;
    btnClear.addEventListener("click", async () => {
      if (btnClear.dataset.confirmArmed !== "1") {
        btnClear.dataset.confirmArmed = "1";
        btnClear.textContent = "确认清除？";
        btnClear.classList.remove("btn-danger-ghost");
        btnClear.classList.add("btn-danger-solid");
        setFeedback("再次点击「确认清除？」以移除本地保存的访问密钥。", "warn");
        resetTimer = window.setTimeout(() => {
          btnClear.dataset.confirmArmed = "0";
          btnClear.textContent = "清除 Key";
          btnClear.classList.remove("btn-danger-solid");
          btnClear.classList.add("btn-danger-ghost");
        }, 4000);
        return;
      }
      if (resetTimer) window.clearTimeout(resetTimer);
      btnClear.disabled = true;
      try {
        await handlers.onClearSupabaseKey();
      } catch (err) {
        setFeedback(`清除密钥失败：${err.message}`, "danger");
        btnClear.disabled = false;
      }
    });
  }

  // Team cloud state actions: refresh, copy SQL, push, pull
  const btnHpRefresh = container.querySelector("#btn-hp-refresh");
  btnHpRefresh?.addEventListener("click", async () => {
    btnHpRefresh.disabled = true;
    setFeedback("正在检测团队云端协作表状态…");
    try {
      const st = await handlers.onRefreshHpStatus();
      setFeedback(
        st.initialized
          ? `协作表已就绪：云端 ${formatInt(st.total_cloud_rows || 0)} 条，本地 ${formatInt(st.total_local_rows || 0)} 条。`
          : "云端协作表尚未初始化，可在「数据源设置」中复制建表 SQL 执行。",
        st.initialized ? "ok" : "warn"
      );
    } catch (err) {
      setFeedback(`状态检测失败：${err.message}`, "danger");
    } finally {
      btnHpRefresh.disabled = false;
    }
  });

  const btnHpCopySql = container.querySelector("#btn-hp-copy-sql");
  btnHpCopySql?.addEventListener("click", async () => {
    let sqlText = hpStatus?.migration_sql || "";
    if (!sqlText) {
      btnHpCopySql.disabled = true;
      try {
        const st = await handlers.onRefreshHpStatus();
        sqlText = st?.migration_sql || "";
      } catch (err) {
        setFeedback(`获取建表 SQL 失败：${err.message}`, "danger");
        btnHpCopySql.disabled = false;
        return;
      }
      btnHpCopySql.disabled = false;
    }
    if (!sqlText) {
      setFeedback("未读取到建表 SQL，请检查后端迁移脚本。", "danger");
      return;
    }
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(sqlText);
        setFeedback("已复制建表 SQL，请到 Supabase SQL Editor 粘贴执行后刷新状态。", "ok");
      } else {
        window.prompt("请复制以下建表 SQL 到 Supabase SQL Editor 执行：", sqlText);
      }
    } catch (_) {
      window.prompt("请复制以下建表 SQL 到 Supabase SQL Editor 执行：", sqlText);
    }
  });

  const btnHpPush = container.querySelector("#btn-hp-push");
  btnHpPush?.addEventListener("click", async () => {
    btnHpPush.disabled = true;
    setFeedback("正在推送本地审阅结论、AI 缓存与印花方向到云端…");
    try {
      const res = await handlers.onPushHpState();
      setFeedback(`推送完成：共同步 ${formatInt(res.total_pushed || 0)} 条团队记录。`, "ok");
    } catch (err) {
      setFeedback(`推送失败：${err.message}`, "danger");
      btnHpPush.disabled = false;
    }
  });

  const btnHpPull = container.querySelector("#btn-hp-pull");
  btnHpPull?.addEventListener("click", async () => {
    btnHpPull.disabled = true;
    setFeedback("正在从云端拉取团队最新状态并合并到本地…");
    try {
      const res = await handlers.onPullHpState();
      setFeedback(`拉取完成：合并 ${formatInt(res.total_pulled || 0)} 条团队记录，各页面已同步更新。`, "ok");
    } catch (err) {
      setFeedback(`拉取失败：${err.message}`, "danger");
      btnHpPull.disabled = false;
    }
  });
}
