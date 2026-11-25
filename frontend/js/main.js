/**
 * Application entry point: routing, global scope sync, keyboard support, and view orchestration.
 */

import { api } from "./api.js";
import { hideTooltip, stopWordCloudPhysics } from "./charts.js";
import { escapeHtml, formatInt } from "./format.js";
import { ROUTE_META, state } from "./state.js";
import { buildAiDryRunSummaryHtml, openAiConfigModal, openAiDryRunModal } from "./views/ai_modal.js";
import { renderCollectionView } from "./views/collection.js";
import { bindTableRowEvents, renderDashboardView, renderProductTableHtml } from "./views/dashboard.js";
import { closeDrawer, openProductDrawer } from "./views/drawer.js";
import {
  closeReviewModal,
  openPreprocessingDrawer,
  renderPreprocessingView,
  syncPrepSelectionUi,
} from "./views/preprocessing.js";
import {
  bindScoringTableEvents,
  renderScoringTableHtml,
  renderScoringView,
} from "./views/scoring.js";
import {
  closeDirectionDetailModal,
  closeImageLightbox,
  renderDirectionsView,
  syncDirectionSelectionUi,
} from "./views/directions.js";
import { renderTop10View } from "./views/top10.js";

const mainEl = () => document.getElementById("main-content");

function closeSidebarMobile() {
  document.getElementById("app-shell")?.classList.remove("sidebar-open");
}

function renderCategorySwitch() {
  const wrap = document.getElementById("category-switch");
  if (!wrap) return;
  const cats = state.categories || [];
  const configured = cats.filter((c) => c.is_configured && c.node_id);
  const pending = cats.filter((c) => !c.is_configured);

  const buttons = [
    `<button type="button" class="seg-btn ${state.category === "all" ? "is-active" : ""}" data-category="all" role="tab" aria-selected="${state.category === "all" ? "true" : "false"}">全部</button>`,
    ...configured.map((cat) => {
      const active = state.category === cat.node_id;
      const label = configured.length > 3 ? (cat.short_name || cat.name) : cat.name;
      return `
        <button type="button" class="seg-btn ${active ? "is-active" : ""}" data-category="${escapeHtml(cat.node_id)}" role="tab" aria-selected="${active ? "true" : "false"}" title="${escapeHtml(cat.name)}（${escapeHtml(cat.en_name)} · ${escapeHtml(cat.node_id)}）">
          <span class="cat-dot" style="background:${escapeHtml(cat.color)}"></span>${escapeHtml(label)}
        </button>
      `;
    }),
  ];

  if (pending.length > 0) {
    const pendingTitle = `待配置 Node ID（${pending.length} 项）：` + pending.map((c) => `${c.name} (${c.en_name})`).join("、") + "。点击前往采集配置查看。";
    buttons.push(
      `<button type="button" class="seg-btn" data-jump-collection="true" title="${escapeHtml(pendingTitle)}" style="color:var(--ink-faint);">+${pending.length} 待配置</button>`
    );
  }

  wrap.innerHTML = buttons.join("");

  wrap.querySelectorAll(".seg-btn[data-category]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const cat = btn.getAttribute("data-category");
      if (cat === state.category) return;
      await dashboardHandlers.onSelectCategory(cat);
    });
  });

  wrap.querySelectorAll(".seg-btn[data-jump-collection]").forEach((btn) => {
    btn.addEventListener("click", () => navigateTo("collection"));
  });
}

function updateTopbarAndNav() {
  const meta = ROUTE_META[state.route] || ROUTE_META.dashboard;
  const title = document.getElementById("page-title");
  const scopeEl = document.getElementById("scope-controls");

  if (title) title.textContent = meta.title;
  if (scopeEl) scopeEl.classList.toggle("is-hidden", !meta.showScope);

  document.querySelectorAll(".nav-item[data-route]").forEach((btn) => {
    const active = btn.getAttribute("data-route") === state.route;
    btn.classList.toggle("is-active", active);
    if (active) btn.setAttribute("aria-current", "page");
    else btn.removeAttribute("aria-current");
  });

  document.querySelectorAll("#category-switch .seg-btn[data-category]").forEach((btn) => {
    const active = btn.getAttribute("data-category") === state.category;
    btn.classList.toggle("is-active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
}

function renderSidebarHealth() {
  const h = state.health;
  const dot = document.getElementById("db-status-dot");
  const label = document.getElementById("db-status-label");
  const meta = document.getElementById("db-status-meta");
  const safety = document.getElementById("safety-tag");
  const dateSelect = document.getElementById("date-select");

  if (!h) return;
  if (Array.isArray(h.categories) && h.categories.length) {
    state.categories = h.categories;
    renderCategorySwitch();
  }
  const db = h.db_status || {};
  if (dot) {
    dot.className = "status-dot " + (db.state === "ready" ? "is-ready" : "is-error");
  }
  if (label) {
    label.textContent = db.state === "ready" ? "SQLite 快照就绪" : `数据源：${db.state}`;
  }
  if (meta) {
    meta.textContent = db.state === "ready"
      ? `${db.batch_count} 批 · ${db.observation_count} 观察`
      : db.message || "未加载有效数据库";
  }
  if (safety) {
    safety.textContent = h.allow_live_collection
      ? "真实付费采集已解锁"
      : "安全模式 · 禁止付费请求";
  }

  state.availableDates = h.available_dates || [];
  if (!state.selectedDate && state.availableDates.length) {
    state.selectedDate = state.availableDates[0];
  }
  if (dateSelect) {
    if (!state.availableDates.length) {
      dateSelect.innerHTML = `<option value="">无快照</option>`;
      dateSelect.disabled = true;
    } else {
      dateSelect.disabled = false;
      dateSelect.innerHTML = state.availableDates
        .map((d) => `<option value="${escapeHtml(d)}" ${d === state.selectedDate ? "selected" : ""}>${escapeHtml(d)}</option>`)
        .join("");
    }
  }
}

async function loadProductsOnly() {
  const wrap = document.getElementById("product-table-body-wrap");
  const countBadge = document.getElementById("table-count-badge");
  if (wrap) {
    wrap.innerHTML = `<div class="loading-state" style="padding:24px;"><div class="spinner"></div><p>刷新商品明细…</p></div>`;
  }
  try {
    state.productsPage = await api.getProducts({
      date: state.selectedDate,
      category: state.category,
      dedup: state.dedupMode,
      prep_scope: state.prepScope,
      ...state.table,
    });
    if (wrap) {
      wrap.innerHTML = renderProductTableHtml(state.productsPage);
      bindTableRowEvents(wrap, dashboardHandlers);
    }
    if (countBadge) {
      countBadge.textContent = `${state.productsPage.total} 件`;
    }
  } catch (err) {
    if (wrap) {
      wrap.innerHTML = `<div class="alert alert-danger">加载商品明细失败：${escapeHtml(err.message)}</div>`;
    }
  }
}

const dashboardHandlers = {
  onOpenAsin(asin) {
    openProductDrawer(asin, state.selectedDate);
  },
  onJumpToCollection() {
    navigateTo("collection");
  },
  onJumpToPreprocessing() {
    navigateTo("preprocessing");
  },
  async onChangePrepScope(mode) {
    state.prepScope = ["printable", "hide_excluded", "all"].includes(mode) ? mode : "printable";
    state.table.page = 1;
    await renderDashboard();
  },
  onToggleScatterScale(mode) {
    state.scatterScaleMode = mode === "linear" ? "linear" : "focused";
    renderDashboardFromCache();
  },
  onToggleScatterCategory(catGroup) {
    const base = Array.isArray(state.scatterSelectedCategories)
      ? state.scatterSelectedCategories
      : getDefaultScatterCategories(state.overview);
    const set = new Set(base);
    if (set.has(catGroup)) set.delete(catGroup);
    else set.add(catGroup);
    state.scatterSelectedCategories = Array.from(set);
    renderDashboardFromCache();
  },
  onSetScatterCategories(catGroups) {
    state.scatterSelectedCategories = Array.isArray(catGroups) ? [...catGroups] : [];
    renderDashboardFromCache();
  },
  onToggleDashboardTable(forceVal) {
    state.dashboardTableExpanded = typeof forceVal === "boolean" ? forceVal : !state.dashboardTableExpanded;
    renderDashboardFromCache();
  },
  onToggleWordCloudMotion() {
    state.wordCloudPaused = !state.wordCloudPaused;
    renderDashboardFromCache();
  },
  async onSelectCategory(nodeId) {
    state.category = nodeId;
    if (nodeId === "all" && state.table.sort === "source_category_bsr") {
      state.table.sort = "listing_sales_30d";
      state.table.order = "desc";
    }
    state.table.page = 1;
    updateTopbarAndNav();
    await renderCurrentRoute();
  },
  async onTogglePriceBand(bandKey) {
    const next = state.table.price_band === bandKey ? "all" : bandKey;
    state.table.price_band = next;
    state.table.page = 1;
    if (next !== "all") state.dashboardTableExpanded = true;
    await renderDashboard();
    if (next !== "all") {
      document.getElementById("product-table-section")?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  },
  async onSelectAnomaly(val) {
    state.table.anomaly = val;
    state.table.page = 1;
    if (val && val !== "all") state.dashboardTableExpanded = true;
    await renderDashboard();
  },
  async onSelectBrand(brand) {
    const next = state.table.brand === brand ? "" : brand;
    state.table.brand = next;
    state.table.page = 1;
    if (next) state.dashboardTableExpanded = true;
    await renderDashboard();
    if (next) {
      document.getElementById("product-table-section")?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  },
  async onSelectKeyword(keyword) {
    const next = (state.table.q || "").toLowerCase() === (keyword || "").toLowerCase() ? "" : keyword;
    state.table.q = next;
    state.table.page = 1;
    if (next) state.dashboardTableExpanded = true;
    await renderDashboard();
    if (next) {
      document.getElementById("product-table-section")?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  },
  async onTableFilterChange(patch) {
    Object.assign(state.table, patch);
    await loadProductsOnly();
  },
  async onResetTableFilters() {
    state.table = {
      q: "",
      anomaly: "all",
      price_band: "all",
      brand: "",
      sort: "listing_sales_30d",
      order: "desc",
      page: 1,
      page_size: 15,
    };
    await renderDashboard();
  },
  async onChangePage(delta) {
    const next = state.table.page + delta;
    if (!state.productsPage) return;
    if (next < 1 || next > state.productsPage.total_pages) return;
    state.table.page = next;
    await loadProductsOnly();
  },
};

const collectionHandlers = {
  onFormChange(nextForm) {
    state.collectionForm = { ...state.collectionForm, ...nextForm };
    if (state.collectionPlan) {
      state.collectionPlan = null;
      renderCollection();
    }
  },
  async onCreatePlan(formPayload) {
    state.collectionForm = { ...state.collectionForm, ...formPayload };
    state.collectionSubmitting = true;
    try {
      state.collectionPlan = await api.planCollection(state.collectionForm);
    } finally {
      state.collectionSubmitting = false;
    }
    renderCollection();
  },
  async onExecuteRun(token) {
    if (state.collectionSubmitting) return;
    state.collectionSubmitting = true;
    renderCollection();
    try {
      const result = await api.runCollection({
        confirmation_token: token,
        confirmed: true,
      });
      state.collectionLastRun = result;
      state.collectionPlan = null;
      const [cfg, batches, health] = await Promise.all([
        api.getCollectionConfig(),
        api.getCollectionBatches(),
        api.getHealth(),
      ]);
      state.collectionConfig = cfg;
      state.collectionBatches = batches;
      state.health = health;
      if (result.database_modified && result.observed_date) {
        state.selectedDate = result.observed_date;
      }
      renderSidebarHealth();
    } finally {
      state.collectionSubmitting = false;
      renderCollection();
    }
  },
  onClearPlan() {
    state.collectionPlan = null;
    renderCollection();
  },
  async onSaveApiKey(apiKey) {
    await api.saveCollectionApiKey(apiKey);
    state.collectionConfig = await api.getCollectionConfig();
    state.collectionPlan = null;
    renderCollection();
  },
  async onClearApiKey() {
    await api.clearCollectionApiKey();
    state.collectionConfig = await api.getCollectionConfig();
    state.collectionPlan = null;
    renderCollection();
  },
  onToggleSupabaseSettings(forceValue) {
    const sb = state.collectionConfig?.supabase || {};
    const current = state.supabaseSettingsOpen === null ? !sb.has_api_key : Boolean(state.supabaseSettingsOpen);
    state.supabaseSettingsOpen = typeof forceValue === "boolean" ? forceValue : !current;
    renderCollection(false, { skipHpAutoRefresh: true });
  },
  onToggleBatchesExpanded() {
    state.collectionBatchesExpanded = !state.collectionBatchesExpanded;
    renderCollection(false, { skipHpAutoRefresh: true });
  },
  onSyncDateChange(observedDate) {
    state.collectionForm = { ...state.collectionForm, observed_date: observedDate };
  },
  async onSaveSupabaseConfig(payload) {
    const res = await api.saveSupabaseConfig(payload);
    state.collectionConfig = await api.getCollectionConfig();
    renderCollection();
    refreshSupabaseHpStatusAsync();
    return res;
  },
  async onClearSupabaseKey() {
    const res = await api.clearSupabaseApiKey();
    state.collectionConfig = await api.getCollectionConfig();
    state.supabaseHpStatus = null;
    state.supabaseSettingsOpen = true;
    renderCollection();
    refreshSupabaseHpStatusAsync();
    return res;
  },
  async onTestSupabaseConfig(payload) {
    return api.testSupabaseConfig(payload);
  },
  async onRefreshHpStatus() {
    const st = await api.getSupabaseHpStatus();
    state.supabaseHpStatus = st;
    if (state.route === "collection") renderCollection();
    return st;
  },
  async onPushHpState() {
    const res = await api.pushSupabaseHpState({});
    state.supabaseHpStatus = await api.getSupabaseHpStatus().catch(() => state.supabaseHpStatus);
    if (state.route === "collection") renderCollection();
    return res;
  },
  async onPullHpState() {
    const res = await api.pullSupabaseHpState({});
    // Invalidate local view caches so 02/04/05/06 reflect freshly pulled team state
    state.preprocessingOverview = null;
    state.scoringData = null;
    state.scoringInitialized = false;
    state.supabaseHpStatus = await api.getSupabaseHpStatus().catch(() => state.supabaseHpStatus);
    if (state.route === "collection") renderCollection();
    return res;
  },
  async onSyncSupabase({ observedDate }) {
    const preview = await api.syncSupabaseData({
      observed_date: observedDate,
      dry_run: true,
    });
    const s = preview.summary || {};
    const cats = (preview.category_breakdown || []).filter((c) => c.matched_count > 0);
    const summaryHtml = `
      <div class="kv-grid" style="grid-template-columns:repeat(3, minmax(0, 1fr));">
        <div class="kv-box">
          <div class="kv-label">云端 Snapshots / Discoveries</div>
          <div class="kv-val" style="font-size:18px;">${formatInt(s.snapshot_records_total || 0)} <span style="font-size:11.5px;font-weight:500;color:var(--ink-muted);">/ ${formatInt(s.discovery_records_total || 0)}</span></div>
        </div>
        <div class="kv-box">
          <div class="kv-label">命中 6 大类目商品</div>
          <div class="kv-val" style="color:var(--accent);font-size:18px;">${formatInt(s.matched_asin_count || 0)} <span style="font-size:11.5px;font-weight:500;color:var(--ink-muted);">ASIN (${formatInt(s.matched_observation_count || 0)} 条观察)</span></div>
        </div>
        <div class="kv-box">
          <div class="kv-label">未命中目标类目（已隔离）</div>
          <div class="kv-val" style="color:var(--ink-muted);font-size:18px;">${formatInt(s.unmatched_asin_count || 0)} <span style="font-size:11.5px;font-weight:500;">ASIN</span></div>
        </div>
      </div>
      <div style="padding:10px 12px;border-radius:var(--radius-sm);background:var(--bg-elevated);border:1px solid var(--border);font-size:12.5px;line-height:1.6;">
        <div><strong>目标类目覆盖（${formatInt(s.matched_category_count || 0)} / 6 类目）：</strong>
          ${cats.length
            ? cats.map((c) => `<span class="badge badge-neutral" style="margin-right:4px;">${escapeHtml(c.short_name || c.name)}: ${formatInt(c.matched_count)} 条</span>`).join(" ")
            : "无匹配类目"
          }
        </div>
        <div style="margin-top:6px;">
          <strong>合并后核心字段覆盖率${s.merged_multi_snapshot_asin_count ? `（已互补合并 ${formatInt(s.merged_multi_snapshot_asin_count)} 个多快照 ASIN）` : ""}：</strong>
          <div style="margin-top:2px;">
            · 有效 30 天销量覆盖率：<strong>${s.valid_sales_pct ?? 0}%</strong>（${formatInt(s.valid_sales_count || 0)} / ${formatInt(s.matched_asin_count || 0)} 件；其余 ${formatInt(s.missing_sales_count || 0)} 件保留为 NULL，04 评分自动启用「超越同类占比」）<br>
            · 有效主图覆盖率：<strong>${s.valid_image_pct ?? 0}%</strong>（${formatInt(s.valid_image_count || 0)} / ${formatInt(s.matched_asin_count || 0)} 件${s.missing_image_count ? `，缺主图 ${formatInt(s.missing_image_count)} 件` : ""}）<br>
            · 有效售价覆盖率：<strong>${s.valid_price_pct ?? 0}%</strong>（${formatInt(s.valid_price_count || 0)} / ${formatInt(s.matched_asin_count || 0)} 件${s.missing_price_count ? `，缺失售价 ${formatInt(s.missing_price_count)} 件` : ""}）
          </div>
        </div>
      </div>
    `;
    return new Promise((resolve) => {
      openAiDryRunModal({
        eyebrowText: "SUPABASE SYNC · DRY-RUN PREVIEW",
        title: "Supabase 云端数据源（只读同步）· 预检确认",
        summaryHtml,
        warningText: `严格只读拉取（仅 HTTP GET，绝对零外部 AI / Sorftime 调用，也不向云端发起任何写入）。确认后将按观察日期 ${preview.observed_date} 写入本地工作库 data/category_sales_live.sqlite3 并自动更新 02 规则预处理。`,
        confirmLabel: (s.matched_observation_count || 0) > 0
          ? `确认同步写入本地工作库 (${formatInt(s.matched_observation_count)} 条观察)`
          : "无匹配目标类目数据",
        runningBtnLabel: "正在同步入库…",
        progressText: "正在从 Supabase 只读拉取并写入本地工作库...",
        canConfirm: (s.matched_observation_count || 0) > 0,
        missingKey: false,
        onConfirm: async () => {
          const res = await api.syncSupabaseData({
            observed_date: preview.observed_date,
            dry_run: false,
          });
          const [cfg, batches, health] = await Promise.all([
            api.getCollectionConfig(),
            api.getCollectionBatches(),
            api.getHealth(),
          ]);
          state.collectionConfig = cfg;
          state.collectionBatches = batches;
          state.health = health;
          if (res.observed_date) {
            state.selectedDate = res.observed_date;
          }
          renderSidebarHealth();
          renderCollection();
          resolve(res);
        },
      });
    });
  },
  async onJumpAfterCollection(targetRoute, observedDate) {
    if (observedDate) {
      state.selectedDate = observedDate;
    }
    state.table.page = 1;
    await navigateTo(targetRoute === "signals" ? "dashboard" : targetRoute);
  },
  async onSaveSignalNodeConfig(key, nodeId) {
    const res = await api.saveSignalNodeConfig({ key, node_id: nodeId });
    state.collectionConfig = await api.getCollectionConfig();
    state.collectionPlan = null;
    // If a new signal Node ID was saved, auto-select it in the manual collection form for convenience
    const validNodes = new Set([
      ...(state.collectionConfig.category_states || []).filter((c) => c.is_configured && c.node_id).map((c) => c.node_id),
      ...(state.collectionConfig.signal_category_states || []).filter((c) => c.is_configured && c.node_id).map((c) => c.node_id),
    ]);
    const nextCats = (state.collectionForm.categories || []).filter((id) => validNodes.has(id));
    if (res.node_id && validNodes.has(res.node_id) && !nextCats.includes(res.node_id)) {
      nextCats.push(res.node_id);
    }
    state.collectionForm = { ...state.collectionForm, categories: nextCats };
    renderCollection();
    return res;
  },
  async onRetryFailed(failedNodeIds) {
    state.collectionForm = {
      ...state.collectionForm,
      categories: failedNodeIds,
      simulate_partial_failure: false,
    };
    state.collectionPlan = await api.planCollection(state.collectionForm);
    renderCollection();
  },
};

function getDefaultScatterCategories(overview) {
  if (!overview) return [];
  const cmp = overview.category_comparison || {};
  const activeCats = (cmp.active_categories && cmp.active_categories.length)
    ? cmp.active_categories
    : (cmp.categories || []).filter((c) => c.has_data || c.observation_count > 0);
  const allPts = overview.price_analysis?.scatter || [];
  return [...activeCats]
    .sort((a, b) => {
      const ca = allPts.filter((pt) => pt.category_group === a.key).length;
      const cb = allPts.filter((pt) => pt.category_group === b.key).length;
      return cb - ca;
    })
    .slice(0, 2)
    .map((c) => c.key);
}

function renderDashboardFromCache() {
  const container = mainEl();
  if (!state.overview) return;
  const effectiveScatterCats = Array.isArray(state.scatterSelectedCategories)
    ? state.scatterSelectedCategories
    : getDefaultScatterCategories(state.overview);
  renderDashboardView(container, {
    overview: state.overview,
    productsPage: state.productsPage,
    tableState: state.table,
    prepScope: state.prepScope,
    scatterScaleMode: state.scatterScaleMode,
    scatterSelectedCategories: effectiveScatterCats,
    tableExpanded: state.dashboardTableExpanded,
    wordCloudPaused: state.wordCloudPaused,
    handlers: dashboardHandlers,
  });
}

async function renderDashboard() {
  const [overview, productsPage] = await Promise.all([
    api.getOverview({
      date: state.selectedDate,
      category: state.category,
      dedup: state.dedupMode,
      prep_scope: state.prepScope,
    }),
    api.getProducts({
      date: state.selectedDate,
      category: state.category,
      dedup: state.dedupMode,
      prep_scope: state.prepScope,
      ...state.table,
    }),
  ]);
  state.overview = overview;
  state.productsPage = productsPage;
  renderSidebarHealth();
  renderDashboardFromCache();
}

let hpStatusFetchSeq = 0;
function refreshSupabaseHpStatusAsync() {
  const seq = ++hpStatusFetchSeq;
  api
    .getSupabaseHpStatus()
    .then((st) => {
      if (seq !== hpStatusFetchSeq) return;
      state.supabaseHpStatus = st;
      if (state.route === "collection") {
        renderCollection(false, { skipHpAutoRefresh: true });
      }
    })
    .catch((err) => {
      if (seq !== hpStatusFetchSeq) return;
      state.supabaseHpStatus = {
        ...(state.supabaseHpStatus || {}),
        ok: false,
        configured: Boolean(state.collectionConfig?.supabase?.has_api_key),
        initialized: false,
        check_error: err.message || "检测失败",
      };
      if (state.route === "collection") {
        renderCollection(false, { skipHpAutoRefresh: true });
      }
    });
}

async function renderCollection(forceFetch = false, { skipHpAutoRefresh = false } = {}) {
  const container = mainEl();
  const needHpInit = forceFetch || !state.supabaseHpStatus;
  if (forceFetch || !state.collectionConfig) {
    const [cfg, batches] = await Promise.all([
      api.getCollectionConfig(),
      api.getCollectionBatches(),
    ]);
    state.collectionConfig = cfg;
    state.collectionBatches = batches;
    if (!state.collectionForm.observed_date) {
      state.collectionForm.observed_date = cfg.beijing_today;
    }
    if (!state.collectionForm.categories || !state.collectionForm.categories.length) {
      state.collectionForm.categories = (cfg.category_states || [])
        .filter((c) => c.is_configured && c.node_id)
        .map((c) => c.node_id);
      if (cfg.safety && cfg.safety.allow_live_collection) {
        state.collectionForm.mode = "live";
      }
    }
  }
  renderCollectionView(container, {
    config: state.collectionConfig,
    batchesResp: state.collectionBatches,
    hpStatus: state.supabaseHpStatus,
    form: state.collectionForm,
    settingsOpen: state.supabaseSettingsOpen,
    batchesExpanded: state.collectionBatchesExpanded,
    submitting: state.collectionSubmitting,
    handlers: collectionHandlers,
  });
  if (needHpInit && !skipHpAutoRefresh) {
    refreshSupabaseHpStatusAsync();
  }
}

const preprocessingHandlers = {
  async onRunPreprocessing() {
    if (state.preprocessingRunning) return;
    state.preprocessingRunning = true;
    renderPreprocessing(false);
    try {
      const res = await api.runPreprocessing(state.selectedDate);
      state.preprocessingOverview = res.overview;
      const run = res.run || {};
      state.preprocessingToast = {
        type: "ok",
        message: `预处理完成：共 ${run.unique_asin_count || 0} 件去重商品（可评分 ${run.counts?.ready ?? 0}，待核实 ${run.counts?.needs_review ?? 0}，已人工排除 ${run.counts?.excluded ?? 0}）；已有人工审核结论已完整保留。`,
      };
    } catch (err) {
      state.preprocessingToast = { type: "error", message: `预处理执行失败：${err.message}` };
    } finally {
      state.preprocessingRunning = false;
    }
    await renderPreprocessing(true);
  },
  async onSubmitReview(payload) {
    const res = await api.submitPreprocessingReview({
      ...payload,
      observed_date: state.selectedDate,
    });
    state.preprocessingOverview = res.overview;
    const item = res.item;
    if (payload.action === "confirm_exclude") {
      state.preprocessingToast = {
        type: "ok",
        message: `已人工确认排除 ${payload.asin}（${item.manual_review_reason || "已排除"}），已移出评分输入。`,
        undoAsin: payload.asin,
      };
    } else if (payload.action === "confirm_keep") {
      state.preprocessingToast = {
        type: "ok",
        message: `已人工确认保留 ${payload.asin}，当前准入状态：${item.eligibility_label}。`,
        undoAsin: payload.asin,
      };
    } else {
      state.preprocessingToast = {
        type: "ok",
        message: `已撤销 ${payload.asin} 的人工决定，恢复为：${item.eligibility_label}。`,
      };
    }
    await renderPreprocessing(true);
    return res;
  },
  onDismissToast() {
    state.preprocessingToast = null;
    renderPreprocessing(false);
  },
  onJumpToScoring() {
    navigateTo("scoring");
  },
  onOpenAiConfig() {
    openAiConfigModal({
      onSaved: (cfg) => {
        state.aiConfig = cfg;
      },
    });
  },
  async onRunAiPurify() {
    if (state.preprocessingAiRunning) return;
    let curLimit = 50;
    let curSkipSuspected = true;

    const loadAndOpenPurifyModal = async () => {
      let preview;
      try {
        preview = await api.runAiPurify({
          observed_date: state.selectedDate,
          dry_run: true,
          limit: curLimit,
          skip_suspected_non_print: curSkipSuspected,
        });
      } catch (err) {
        state.preprocessingToast = { type: "error", message: `AI 预检失败：${err.message}` };
        renderPreprocessing(false);
        return;
      }

      const willCall = preview.will_analyze_count || 0;

      openAiDryRunModal({
        title: "AI 视觉提纯 · 成本预检确认",
        summaryHtml: buildAiDryRunSummaryHtml(preview, { mode: "purify", interactivePurify: true }),
        warningText: "严格遵守方案 A：AI 视觉分析仅写入建议标签与理由，绝不自动排除任何商品。完成后请切换「AI 建议排除」Tab 由人工快速核对并一键批量确认排除。",
        confirmLabel: willCall > 0 ? `确认调用 ${formatInt(willCall)} 次看图 API` : "刷新 AI 缓存统计",
        estimatedCalls: willCall,
        canConfirm: true,
        missingKey: willCall > 0 && !preview.has_api_key,
        onOpenConfig: () => openAiConfigModal({ onSaved: (cfg) => { state.aiConfig = cfg; } }),
        onAfterRender: (modalBody) => {
          modalBody.querySelectorAll("[data-purify-limit]").forEach((btn) => {
            btn.addEventListener("click", async () => {
              const raw = btn.getAttribute("data-purify-limit");
              const nextLimit = raw === "all" ? "all" : Number(raw || 50);
              if (String(nextLimit) === String(curLimit)) return;
              curLimit = nextLimit;
              await loadAndOpenPurifyModal();
            });
          });
          modalBody.querySelector("#ai-purify-skip-suspected")?.addEventListener("change", async (e) => {
            curSkipSuspected = Boolean(e.currentTarget.checked);
            await loadAndOpenPurifyModal();
          });
        },
        onConfirm: async () => {
          state.preprocessingAiRunning = true;
          try {
            const res = await api.runAiPurify({
              observed_date: state.selectedDate,
              dry_run: false,
              limit: curLimit,
              skip_suspected_non_print: curSkipSuspected,
            });
            if (res.overview) {
              state.preprocessingOverview = res.overview;
            }
            const sug = res.total_suggest_exclude_count || 0;
            const skippedImg = res.skipped_missing_image_count || 0;
            const deferred = res.deferred_by_limit_count || 0;
            state.preprocessingToast = {
              type: res.failed_count > 0 && res.analyzed_count === 0 ? "error" : "ok",
              message: `AI 视觉提纯完成：新分析 ${formatInt(res.analyzed_count || 0)} 件，命中缓存 ${formatInt(res.cached_skipped || 0)} 件${skippedImg ? `，跳过缺主图 ${formatInt(skippedImg)} 件` : ""}${deferred ? `，剩余 ${formatInt(deferred)} 件可分批再跑` : ""}${res.failed_count ? `，失败 ${formatInt(res.failed_count)} 件` : ""}；共识别出 ${formatInt(sug)} 件「AI 建议排除（纯色/条纹/波点）」（未自动排除，请切至该 Tab 核验确认）。`,
            };
            await renderPreprocessing(true);
          } catch (err) {
            state.preprocessingAiRunning = false;
            state.preprocessingToast = { type: "error", message: `AI 视觉提纯失败：${err.message}` };
            renderPreprocessing(false);
            throw err;
          } finally {
            state.preprocessingAiRunning = false;
          }
        },
      });
    };

    await loadAndOpenPurifyModal();
  },
  onToggleViewMode(mode) {
    state.preprocessingViewMode = mode === "table" ? "table" : "gallery";
    renderPreprocessing(false);
  },
  getSelectedAsins() {
    return [...(state.preprocessingSelectedAsins || [])];
  },
  onToggleSelectAsin(asin, checked) {
    const set = new Set(state.preprocessingSelectedAsins || []);
    if (checked) set.add(asin);
    else set.delete(asin);
    state.preprocessingSelectedAsins = Array.from(set);
    syncPrepSelectionUi(state.preprocessingSelectedAsins);
  },
  onToggleSelectPage(asins, checked) {
    const set = new Set(state.preprocessingSelectedAsins || []);
    (asins || []).forEach((a) => {
      if (checked) set.add(a);
      else set.delete(a);
    });
    state.preprocessingSelectedAsins = Array.from(set);
    syncPrepSelectionUi(state.preprocessingSelectedAsins);
  },
  onClearSelection() {
    state.preprocessingSelectedAsins = [];
    syncPrepSelectionUi(state.preprocessingSelectedAsins);
  },
  onBatchInputChange(patch) {
    if (patch.batchReason !== undefined) state.preprocessingBatchReason = patch.batchReason;
    if (patch.batchNote !== undefined) state.preprocessingBatchNote = patch.batchNote;
  },
  async onSubmitBatchReview(payload) {
    try {
      const res = await api.submitPreprocessingBatchReview({
        ...payload,
        observed_date: state.selectedDate,
      });
      state.preprocessingOverview = res.overview;
      state.preprocessingSelectedAsins = [];
      const actLabel = res.action === "confirm_exclude"
        ? "批量确认排除"
        : res.action === "confirm_keep"
          ? "批量确认保留"
          : "批量重置待审";
      state.preprocessingToast = {
        type: "ok",
        message: `${actLabel}完成：匹配 ${res.matched_count} 件，实际更新 ${res.updated_count} 件${res.skipped_count ? `（跳过未变化 ${res.skipped_count} 件）` : ""}。`,
        undoAsins: res.action !== "restore" && (res.affected_asins || []).length ? res.affected_asins : null,
      };
      await renderPreprocessing(true);
      return res;
    } catch (err) {
      state.preprocessingToast = { type: "error", message: `批量操作失败：${err.message}` };
      renderPreprocessing(false);
      return null;
    }
  },
  async onFilterChange(patch) {
    Object.assign(state.preprocessingTable, patch);
    await renderPreprocessing(true);
  },
  async onResetFilters() {
    state.preprocessingTable = {
      category: "all",
      eligibility: "all",
      auto_class: "all",
      review_status: "all",
      issue: "all",
      ai_filter: "all",
      q: "",
      sort: "needs_review_first",
      order: "desc",
      page: 1,
      page_size: 24,
    };
    state.preprocessingSelectedAsins = [];
    await renderPreprocessing(true);
  },
  async onChangePage(delta) {
    const next = state.preprocessingTable.page + delta;
    if (!state.preprocessingPage) return;
    if (next < 1 || next > state.preprocessingPage.total_pages) return;
    state.preprocessingTable.page = next;
    await renderPreprocessing(true);
  },
  onToggleRulesDrawer() {
    state.preprocessingRulesOpen = !state.preprocessingRulesOpen;
    renderPreprocessing(false);
  },
  onToggleMoreFilters() {
    state.preprocessingMoreFiltersOpen = !state.preprocessingMoreFiltersOpen;
    renderPreprocessing(false);
  },
  onOpenDetail(asin) {
    const pageAsins = ((state.preprocessingPage && state.preprocessingPage.items) || []).map((i) => i.asin);
    openPreprocessingDrawer(asin, state.selectedDate, {
      excludeReasons: state.preprocessingOverview?.exclude_reasons || {},
      keepReasons: state.preprocessingOverview?.keep_reasons || {},
      pageAsins,
      onNavigateAsin: (nextAsin) => preprocessingHandlers.onOpenDetail(nextAsin),
      onReviewDone: (payload) => preprocessingHandlers.onSubmitReview(payload),
      onBatchReview: (payload) => preprocessingHandlers.onSubmitBatchReview(payload),
    });
  },
};

async function renderPreprocessing(forceFetch = true) {
  const container = mainEl();
  if (forceFetch || !state.preprocessingOverview) {
    const overview = await api.getPreprocessingOverview(state.selectedDate);
    state.preprocessingOverview = overview;
    if (
      !state.preprocessingInitialQueueApplied &&
      state.preprocessingTable.eligibility === "needs_review" &&
      overview?.counts?.needs_review === 0
    ) {
      state.preprocessingTable.eligibility = "all";
    }
    state.preprocessingInitialQueueApplied = true;
    const pageData = await api.getPreprocessingProducts({
      date: state.selectedDate,
      ...state.preprocessingTable,
    });
    state.preprocessingPage = pageData;
  }
  renderPreprocessingView(container, {
    overview: state.preprocessingOverview,
    pageData: state.preprocessingPage,
    filters: state.preprocessingTable,
    viewMode: state.preprocessingViewMode,
    rulesOpen: state.preprocessingRulesOpen,
    moreFiltersOpen: state.preprocessingMoreFiltersOpen,
    selectedAsins: state.preprocessingSelectedAsins,
    batchReason: state.preprocessingBatchReason,
    batchNote: state.preprocessingBatchNote,
    running: state.preprocessingRunning,
    aiRunning: state.preprocessingAiRunning,
    toast: state.preprocessingToast,
    handlers: preprocessingHandlers,
  });
}

function captureCurrentRanks(data) {
  if (!data || !Array.isArray(data.items)) return null;
  const map = {};
  data.items.forEach((item) => {
    if (item && item.asin && item.rank) {
      map[item.asin] = item.rank;
    }
  });
  return map;
}

const scoringHandlers = {
  onJumpToPreprocessing() {
    navigateTo("preprocessing");
  },
  onJumpToDirections() {
    navigateTo("print_verification");
  },
  onToggleTuningDrawer() {
    state.scoringTuningOpen = !state.scoringTuningOpen;
    renderScoringFromCache();
  },
  onChangeScoringPage(delta) {
    const totalItems = (state.scoringData?.items || []).length;
    const pageSize = state.scoringPageSize || 50;
    const totalPages = Math.max(1, Math.ceil(totalItems / pageSize));
    const next = Math.max(1, Math.min(totalPages, (state.scoringPage || 1) + delta));
    if (next === state.scoringPage) return;
    state.scoringPage = next;
    const wrap = document.getElementById("scoring-table-wrap");
    if (wrap && state.scoringData) {
      wrap.innerHTML = renderScoringTableHtml(state.scoringData, {
        compareMode: state.scoringFilters.compareMode,
        unrankedExpanded: state.scoringUnrankedExpanded,
        incompleteExpanded: Boolean(state.scoringIncompleteExpanded),
        page: state.scoringPage,
        pageSize: state.scoringPageSize,
      });
      bindScoringTableEvents(wrap, scoringHandlers);
      document.getElementById("scoring-table-wrap")?.scrollIntoView({ behavior: "smooth", block: "start" });
    } else {
      renderScoringFromCache();
    }
  },
  onChangeScoringPageSize(size) {
    state.scoringPageSize = Number(size) === 100 ? 100 : 50;
    state.scoringPage = 1;
    renderScoringFromCache();
  },
  onClearScoringFilters() {
    state.scoringFilters = {
      ...state.scoringFilters,
      q: "",
      category: "all",
      confidence: "all",
    };
    state.scoringPage = 1;
    evaluateScoringLive({ partialTableOnly: false, persistActive: false });
  },
  onOpenAsin(asin) {
    openProductDrawer(asin, state.selectedDate);
  },
  onDismissToast() {
    state.scoringToast = null;
    renderScoringFromCache();
  },
  onToggleUnranked() {
    state.scoringUnrankedExpanded = !state.scoringUnrankedExpanded;
    const wrap = document.getElementById("scoring-table-wrap");
    if (wrap && state.scoringData) {
      wrap.innerHTML = renderScoringTableHtml(state.scoringData, {
        compareMode: state.scoringFilters.compareMode,
        unrankedExpanded: state.scoringUnrankedExpanded,
        incompleteExpanded: Boolean(state.scoringIncompleteExpanded),
        page: state.scoringPage,
        pageSize: state.scoringPageSize,
      });
      bindScoringTableEvents(wrap, scoringHandlers);
    } else {
      renderScoringFromCache();
    }
  },
  onToggleIncomplete() {
    state.scoringIncompleteExpanded = !state.scoringIncompleteExpanded;
    const wrap = document.getElementById("scoring-table-wrap");
    if (wrap && state.scoringData) {
      wrap.innerHTML = renderScoringTableHtml(state.scoringData, {
        compareMode: state.scoringFilters.compareMode,
        unrankedExpanded: state.scoringUnrankedExpanded,
        incompleteExpanded: Boolean(state.scoringIncompleteExpanded),
        page: state.scoringPage,
        pageSize: state.scoringPageSize,
      });
      bindScoringTableEvents(wrap, scoringHandlers);
    } else {
      renderScoringFromCache();
    }
  },
  async onWeightsChange(nextWeights) {
    state.scoringPreviousRanks = captureCurrentRanks(state.scoringData) || state.scoringPreviousRanks;
    state.scoringWeights = { ...nextWeights };
    state.scoringActivePresetId = "";
    state.directionsFilters.preset_id = "";
    await evaluateScoringLive({ partialTableOnly: true, persistActive: true });
  },
  async onPriceRangesChange(nextRanges) {
    state.scoringPreviousRanks = captureCurrentRanks(state.scoringData) || state.scoringPreviousRanks;
    state.scoringPriceRanges = nextRanges;
    state.scoringActivePresetId = "";
    state.directionsFilters.preset_id = "";
    try {
      await evaluateScoringLive({ partialTableOnly: false, persistActive: true });
      state.scoringToast = {
        type: "ok",
        message: `已更新并同步目标价格区间（04 → 05/06）：T-Shirt $${nextRanges.t_shirt.min}–$${nextRanges.t_shirt.max} · Sweatshirt $${nextRanges.sweatshirt.min}–$${nextRanges.sweatshirt.max}`,
      };
      renderScoringFromCache();
    } catch (err) {
      state.scoringToast = { type: "error", message: `价格区间无效：${err.message}` };
      renderScoringFromCache();
    }
  },
  async onFilterChange(patch) {
    Object.assign(state.scoringFilters, patch);
    state.scoringPage = 1;
    await evaluateScoringLive({ partialTableOnly: false, persistActive: false });
  },
  onCompareModeChange(mode) {
    state.scoringFilters.compareMode = mode === "previous" ? "previous" : "default";
    renderScoringFromCache();
  },
  async onResetDefaults() {
    state.scoringPreviousRanks = captureCurrentRanks(state.scoringData) || state.scoringPreviousRanks;
    state.scoringActivePresetId = "system_default";
    state.directionsFilters.preset_id = "";
    state.scoringWeights = { sales: 35, opportunity: 25, rating: 15, freshness: 15, price: 10 };
    state.scoringPriceRanges = { t_shirt: { min: 15, max: 26 }, sweatshirt: { min: 26, max: 45 } };
    state.scoringToast = { type: "ok", message: "已恢复系统默认权重与目标价格区间，并同步至 05/06。" };
    await evaluateScoringLive({ partialTableOnly: false, persistActive: true });
  },
  async onLoadPreset(presetId) {
    const preset = (state.scoringPresets || []).find((p) => p.preset_id === presetId);
    if (!preset) return;
    state.scoringPreviousRanks = captureCurrentRanks(state.scoringData) || state.scoringPreviousRanks;
    state.scoringActivePresetId = preset.preset_id;
    state.directionsFilters.preset_id = "";
    state.scoringWeights = { ...preset.weights };
    state.scoringPriceRanges = {
      t_shirt: { ...preset.price_ranges.t_shirt },
      sweatshirt: { ...preset.price_ranges.sweatshirt },
    };
    state.scoringToast = { type: "ok", message: `已加载评分方案「${preset.name}」，并同步生效至 05/06。` };
    await evaluateScoringLive({ partialTableOnly: false, persistActive: true });
  },
  async onSavePreset(payload) {
    const res = await api.saveScoringPreset(payload);
    state.scoringPresets = res.presets || [];
    state.scoringActivePresetId = res.preset?.preset_id || "";
    state.directionsFilters.preset_id = "";
    state.scoringToast = { type: "ok", message: `已保存方案「${payload.name}」并同步为当前工作区评分标准。` };
    renderScoringFromCache();
  },
  async onDeletePreset(presetId) {
    try {
      const res = await api.deleteScoringPreset(presetId);
      state.scoringPresets = res.presets || [];
      if (state.scoringActivePresetId === presetId) {
        state.scoringActivePresetId = res.active_config?.preset_id || "custom";
      }
      if (state.directionsFilters.preset_id === presetId) {
        state.directionsFilters.preset_id = "";
      }
      state.scoringToast = { type: "ok", message: "已删除自定义评分方案。" };
      renderScoringFromCache();
    } catch (err) {
      state.scoringToast = { type: "error", message: `删除失败：${err.message}` };
      renderScoringFromCache();
    }
  },
};

function renderScoringFromCache() {
  const container = mainEl();
  renderScoringView(container, {
    data: state.scoringData,
    presets: state.scoringPresets,
    activePresetId: state.scoringActivePresetId,
    weights: state.scoringWeights,
    priceRanges: state.scoringPriceRanges,
    filters: state.scoringFilters,
    tuningOpen: Boolean(state.scoringTuningOpen),
    page: state.scoringPage || 1,
    pageSize: state.scoringPageSize || 50,
    loading: state.scoringLoading,
    toast: state.scoringToast,
    unrankedExpanded: state.scoringUnrankedExpanded,
    incompleteExpanded: Boolean(state.scoringIncompleteExpanded),
    handlers: scoringHandlers,
  });
}

async function evaluateScoringLive({ partialTableOnly = false, persistActive = false, useServerActive = false } = {}) {
  state.scoringLoading = true;
  try {
    const reqBody = {
      observed_date: state.selectedDate,
      category: state.scoringFilters.category,
      confidence: state.scoringFilters.confidence,
      q: state.scoringFilters.q,
      previous_ranks: state.scoringPreviousRanks,
      persist_active: persistActive,
      active_preset_id: state.scoringActivePresetId || "",
    };
    if (!useServerActive) {
      reqBody.weights = state.scoringWeights;
      reqBody.price_ranges = state.scoringPriceRanges;
    }
    const res = await api.evaluateScoring(reqBody);
    state.scoringData = res;
    if (Array.isArray(res.presets)) {
      state.scoringPresets = res.presets;
    }
    if (res.active_config) {
      if (useServerActive || !state.scoringInitialized) {
        state.scoringWeights = { ...res.active_config.weights };
        state.scoringPriceRanges = {
          t_shirt: { ...res.active_config.price_ranges.t_shirt },
          sweatshirt: { ...res.active_config.price_ranges.sweatshirt },
        };
      }
      state.scoringActivePresetId = res.active_config.is_custom ? "" : (res.active_config.preset_id || "system_default");
      state.scoringInitialized = true;
    }
    state.scoringLoading = false;
    if (partialTableOnly && document.getElementById("scoring-table-wrap")) {
      const wrap = document.getElementById("scoring-table-wrap");
      const badge = document.getElementById("scoring-count-badge");
      wrap.innerHTML = renderScoringTableHtml(state.scoringData, {
        compareMode: state.scoringFilters.compareMode,
        unrankedExpanded: state.scoringUnrankedExpanded,
        incompleteExpanded: Boolean(state.scoringIncompleteExpanded),
        page: state.scoringPage || 1,
        pageSize: state.scoringPageSize || 50,
      });
      if (badge) badge.textContent = `${(state.scoringData.items || []).length} 件`;
      document.querySelectorAll("#scoring-preset-list .preset-chip").forEach((el) => {
        el.classList.toggle("is-active", el.getAttribute("data-load-preset") === state.scoringActivePresetId);
      });
      bindScoringTableEvents(wrap, scoringHandlers);
      return;
    }
    renderScoringFromCache();
  } finally {
    state.scoringLoading = false;
  }
}

async function renderScoring() {
  await evaluateScoringLive({
    partialTableOnly: false,
    persistActive: false,
    useServerActive: !state.scoringInitialized,
  });
}

const directionsHandlers = {
  onJumpToScoring() {
    navigateTo("scoring");
  },
  onJumpToTop10() {
    navigateTo("top10_directions");
  },
  onToggleBasisDrawer() {
    state.directionsBasisOpen = !state.directionsBasisOpen;
    renderDirectionsFromCache();
  },
  onToggleShortlistedOnly() {
    state.directionsShortlistedOnly = !state.directionsShortlistedOnly;
    renderDirectionsFromCache();
  },
  onOpenAiConfig() {
    openAiConfigModal({
      onSaved: (cfg) => {
        state.aiConfig = cfg;
      },
    });
  },
  onChangeAiTopN(topN) {
    state.directionsAiTopN = [50, 60, 80].includes(Number(topN)) ? Number(topN) : 60;
    renderDirectionsFromCache();
  },
  onSwitchRightTab(tab) {
    state.directionsRightTab = tab === "standalone" ? "standalone" : "directions";
    state.directionsShowStandaloneNotice = false;
    renderDirectionsFromCache();
  },
  onDismissStandaloneNotice() {
    state.directionsShowStandaloneNotice = false;
    renderDirectionsFromCache();
  },
  onOpenDirectionDetail(dirId) {
    state.directionsDetailModalId = dirId;
  },
  onCloseDirectionDetail() {
    state.directionsDetailModalId = null;
  },
  async onRunAiCluster() {
    if (state.directionsAiRunning) return;
    const effPreset = state.directionsFilters.preset_id || "";
    const topN = state.directionsAiTopN || 60;
    let preview;
    try {
      preview = await api.runAiCluster({
        observed_date: state.selectedDate,
        preset_id: effPreset,
        top_n: topN,
        dry_run: true,
      });
    } catch (err) {
      state.directionsToast = { type: "error", message: `AI 归组预检失败：${err.message}` };
      renderDirectionsFromCache();
      return;
    }

    const imgCalls = preview.uncached_image_calls || 0;
    const total = preview.candidate_count || 0;
    const reduceCalls = preview.reduce_calls || 0;

    openAiDryRunModal({
      title: `AI 一键归纳分组（Top ${topN}）· 成本预检确认`,
      summaryHtml: buildAiDryRunSummaryHtml(preview, { mode: "cluster", topN }),
      warningText: "AI 生成的趋势分组仅写入为草稿（draft · AI 推荐），绝不覆盖或修改您已有的人工创建或已入选 Top 10 方向；未进组的 Top 20 高分/高销单品将自动归入「独立爆款单品（孤品推荐）」。",
      confirmLabel: `确认执行（共 ${formatInt(imgCalls + reduceCalls)} 次 API 调用）`,
      estimatedCalls: imgCalls,
      isCluster: true,
      canConfirm: total >= 2,
      missingKey: !preview.has_api_key,
      onOpenConfig: () => openAiConfigModal({ onSaved: (cfg) => { state.aiConfig = cfg; } }),
      onConfirm: async () => {
        state.directionsAiRunning = true;
        try {
          const res = await api.runAiCluster({
            observed_date: state.selectedDate,
            preset_id: effPreset,
            top_n: topN,
            dry_run: false,
          });
          const created = res.created_count || 0;
          const updated = res.updated_count || 0;
          const standalone = res.standalone_count || 0;
          const rejected = (res.rejected_clusters || []).length;
          const skipped = (res.skipped_existing || []).length;
          const mapFailed = res.map_stats?.image_calls_failed || 0;
          state.directionsRightTab = created > 0 || updated > 0 ? "directions" : "standalone";
          state.directionsShowStandaloneNotice = (created > 0 || updated > 0) && standalone > 0;
          if (created === 0 && updated === 0) {
            const firstErr = res.map_stats?.errors?.[0]?.error || "";
            state.directionsToast = {
              type: "error",
              message: `AI 本次未生成新的趋势分组（拦截不合格/杂项 ${rejected} 个、跳过已有保护方向 ${skipped} 个${mapFailed ? `、看图失败 ${mapFailed} 件` : ""}${firstErr ? `：${firstErr}` : ""}），已为你切换到「独立爆款单品」(${standalone} 件)。`,
            };
          } else {
            state.directionsToast = {
              type: "ok",
              message: `AI 归纳完成：新增 ${created} 个趋势方向${updated ? `、刷新 ${updated} 个 AI 草稿` : ""}，识别出 ${standalone} 件未归组的 Top 20 独立爆款单品${rejected ? `（已拦截 ${rejected} 个不合格/杂项分组）` : ""}${mapFailed ? `（另有 ${mapFailed} 件补图失败已跳过）` : ""}。`,
            };
          }
          await renderDirections();
        } catch (err) {
          state.directionsAiRunning = false;
          state.directionsToast = { type: "error", message: `AI 一键归纳分组失败：${err.message}` };
          renderDirectionsFromCache();
          throw err;
        } finally {
          state.directionsAiRunning = false;
        }
      },
    });
  },
  async onPromoteStandalone(item) {
    try {
      const effPreset = state.directionsFilters.preset_id || "";
      const ai = item.ai_analysis || {};
      const reasons = (item.standalone_reasons || []).join("；");
      const notes = [
        reasons ? `孤品入选依据：${reasons}` : "",
        ai.print_subject ? `核心印花主体：${ai.print_subject}` : "",
        ai.slogan_text ? `衣服文案 OCR：${ai.slogan_text}` : "",
      ].filter(Boolean).join("。");

      const res = await api.createDirection({
        name: item.suggested_name || `独立爆款 · ${item.asin}`,
        style_tags: item.suggested_tags && item.suggested_tags.length ? item.suggested_tags : ["独立爆款"],
        target_audience: "",
        feasibility_rating: 3,
        status: "shortlisted",
        direction_type: "standalone_hit",
        source: "manual",
        notes,
        asins: [item.asin],
        observed_date: state.selectedDate,
        preset_id: effPreset,
      });
      const d = res.direction;
      state.directionsToast = {
        type: "ok",
        message: `已将 ${item.asin} 作为「独立爆款」入选 Top 10：「${d.name}」（方向综合分 ${d.direction_score}），可直接前往 06 查看与导出。`,
      };
      await renderDirections();
    } catch (err) {
      state.directionsToast = { type: "error", message: `独立爆款入选失败：${err.message}` };
      renderDirectionsFromCache();
    }
  },
  onDismissToast() {
    state.directionsToast = null;
    renderDirectionsFromCache();
  },
  getSelectedAsins() {
    return [...(state.directionsSelectedAsins || [])];
  },
  onToggleSelectAsin(asin, checked) {
    const set = new Set(state.directionsSelectedAsins || []);
    if (checked) set.add(asin);
    else set.delete(asin);
    state.directionsSelectedAsins = Array.from(set);
    syncDirectionSelectionUi(
      state.directionsSelectedAsins,
      Boolean(state.directionsList && (state.directionsList.items || []).length > 0)
    );
  },
  onToggleSelectPage(asins, checked) {
    const set = new Set(state.directionsSelectedAsins || []);
    (asins || []).forEach((a) => {
      if (checked) set.add(a);
      else set.delete(a);
    });
    state.directionsSelectedAsins = Array.from(set);
    syncDirectionSelectionUi(
      state.directionsSelectedAsins,
      Boolean(state.directionsList && (state.directionsList.items || []).length > 0)
    );
  },
  onClearSelection() {
    state.directionsSelectedAsins = [];
    syncDirectionSelectionUi(
      state.directionsSelectedAsins,
      Boolean(state.directionsList && (state.directionsList.items || []).length > 0)
    );
  },
  onToggleExpand(dirId) {
    const set = new Set(state.directionsExpandedIds || []);
    if (set.has(dirId)) set.delete(dirId);
    else set.add(dirId);
    state.directionsExpandedIds = Array.from(set);
    renderDirectionsFromCache();
  },
  async onFilterChange(patch) {
    Object.assign(state.directionsFilters, patch);
    await renderDirections();
  },
  async onCreateDirection(payload) {
    const res = await api.createDirection({
      ...payload,
      observed_date: state.selectedDate,
      preset_id: state.directionsFilters.preset_id || "",
    });
    state.directionsSelectedAsins = [];
    const d = res.direction;
    state.directionsToast = {
      type: "ok",
      message: `已创建印花方向「${d.name}」${res.added_count ? `并归入 ${res.added_count} 件商品` : ""}，方向综合分 ${d.direction_score}。`,
    };
    await renderDirections();
  },
  async onUpdateDirection(dirId, payload) {
    try {
      const res = await api.updateDirection(dirId, {
        ...payload,
        observed_date: state.selectedDate,
        preset_id: state.directionsFilters.preset_id || "",
      });
      const d = res.direction;
      const msg = payload.status === "shortlisted"
        ? `已将「${d.name}」标记入选 Top 10 候选。`
        : payload.status === "draft"
          ? `已将「${d.name}」取消入选，恢复为草稿。`
          : `已更新印花方向「${d.name}」（方向综合分 ${d.direction_score}）。`;
      state.directionsToast = { type: "ok", message: msg };
      await renderDirections();
    } catch (err) {
      state.directionsToast = { type: "error", message: `更新失败：${err.message}` };
      renderDirectionsFromCache();
    }
  },
  async onDeleteDirection(dirId) {
    try {
      const res = await api.deleteDirection(dirId);
      state.directionsExpandedIds = (state.directionsExpandedIds || []).filter((id) => id !== dirId);
      if (state.directionsDetailModalId === dirId) {
        state.directionsDetailModalId = null;
        closeDirectionDetailModal();
      }
      state.directionsToast = {
        type: "ok",
        message: `已删除方向「${res.deleted_name}」（已解除 ${res.removed_links_count} 条商品关联，原始商品不受影响）。`,
      };
      await renderDirections();
    } catch (err) {
      state.directionsToast = { type: "error", message: `删除失败：${err.message}` };
      renderDirectionsFromCache();
    }
  },
  async onAddLinks(dirId, asins) {
    try {
      const res = await api.addDirectionLinks(dirId, {
        asins,
        observed_date: state.selectedDate,
        preset_id: state.directionsFilters.preset_id || "",
      });
      state.directionsSelectedAsins = [];
      const d = res.direction;
      state.directionsToast = {
        type: "ok",
        message: `已归入「${d.name}」：新增 ${res.added_count} 件${res.duplicate_count ? `（${res.duplicate_count} 件已在方向内自动去重）` : ""}，方向综合分更新为 ${d.direction_score}。`,
      };
      await renderDirections();
    } catch (err) {
      state.directionsToast = { type: "error", message: `批量归入失败：${err.message}` };
      renderDirectionsFromCache();
    }
  },
  async onRemoveLinks(dirId, asins) {
    try {
      const res = await api.removeDirectionLinks(dirId, {
        asins,
        observed_date: state.selectedDate,
        preset_id: state.directionsFilters.preset_id || "",
      });
      const d = res.direction;
      state.directionsToast = {
        type: "ok",
        message: `已从「${d.name}」移出 ${res.removed_count} 件商品，聚合指标已实时重算。`,
      };
      await renderDirections();
    } catch (err) {
      state.directionsToast = { type: "error", message: `移出失败：${err.message}` };
      renderDirectionsFromCache();
    }
  },
};

function renderDirectionsFromCache() {
  const container = mainEl();
  renderDirectionsView(container, {
    candidatesData: state.directionsCandidates,
    directionsData: state.directionsList,
    selectedAsins: state.directionsSelectedAsins,
    rightTab: state.directionsRightTab,
    showStandaloneNotice: state.directionsShowStandaloneNotice,
    basisOpen: Boolean(state.directionsBasisOpen),
    shortlistedOnly: Boolean(state.directionsShortlistedOnly),
    detailModalId: state.directionsDetailModalId,
    aiTopN: state.directionsAiTopN,
    aiRunning: state.directionsAiRunning,
    filters: state.directionsFilters,
    toast: state.directionsToast,
    handlers: directionsHandlers,
  });
}

async function renderDirections() {
  const effPreset = state.directionsFilters.preset_id || "";
  const [candData, dirData] = await Promise.all([
    api.getDirectionCandidates({
      date: state.selectedDate,
      preset_id: effPreset,
      top_n: state.directionsFilters.top_n,
      category: state.directionsFilters.category,
      assigned: state.directionsFilters.assigned,
      q: state.directionsFilters.q,
    }),
    api.getDirections({
      date: state.selectedDate,
      preset_id: effPreset,
      sort: state.directionsFilters.sort,
    }),
  ]);
  state.directionsCandidates = candData;
  state.directionsList = dirData;
  renderDirectionsFromCache();
}

let top10ToastTimer = null;

function setTop10Toast(toast, autoDismissMs = 5500) {
  if (top10ToastTimer) {
    clearTimeout(top10ToastTimer);
    top10ToastTimer = null;
  }
  state.top10Toast = toast;
  if (toast && autoDismissMs > 0) {
    top10ToastTimer = setTimeout(() => {
      state.top10Toast = null;
      top10ToastTimer = null;
      if (state.route === "top10_directions") renderTop10FromCache();
    }, autoDismissMs);
  }
}

const top10Handlers = {
  onJumpToDirections() {
    navigateTo("print_verification");
  },
  onSetPresentationMode(enabled) {
    state.top10PresentationMode = Boolean(enabled);
    if (state.top10PresentationMode) state.top10EditingNoteId = null;
    renderTop10FromCache();
  },
  onCopyBrief(name, failed = false) {
    setTop10Toast(
      failed
        ? { type: "error", message: `复制「${name}」设计 Brief 失败，请检查浏览器剪贴板权限。` }
        : { type: "ok", message: `已复制「${name}」完整设计 Brief，可直接粘贴发给设计师。` },
      3500
    );
    renderTop10FromCache();
  },
  onDismissToast() {
    setTop10Toast(null, 0);
    renderTop10FromCache();
  },
  onToggleExpand(dirId) {
    const set = new Set(state.top10ExpandedIds || []);
    if (set.has(dirId)) set.delete(dirId);
    else set.add(dirId);
    state.top10ExpandedIds = Array.from(set);
    renderTop10FromCache();
  },
  onStartEditNote(dirId) {
    state.top10EditingNoteId = dirId;
    renderTop10FromCache();
    document.getElementById(`top10-note-input-${dirId}`)?.focus();
  },
  onCancelEditNote() {
    state.top10EditingNoteId = null;
    renderTop10FromCache();
  },
  async onSaveNote(dirId, notes) {
    try {
      const res = await api.updateDirection(dirId, {
        notes,
        observed_date: state.selectedDate,
        preset_id: state.directionsFilters.preset_id || "",
      });
      state.top10EditingNoteId = null;
      setTop10Toast({
        type: "ok",
        message: `已保存「${res.direction.name}」的设计交接备注。`,
      }, 4000);
      await renderTop10();
    } catch (err) {
      setTop10Toast({ type: "error", message: `保存备注失败：${err.message}` }, 6000);
      renderTop10FromCache();
    }
  },
  async onRemoveShortlist(dirId) {
    try {
      const res = await api.updateDirection(dirId, {
        status: "draft",
        observed_date: state.selectedDate,
        preset_id: state.directionsFilters.preset_id || "",
      });
      setTop10Toast({
        type: "ok",
        message: `已将「${res.direction.name}」移出 Top 10 入选（状态退回草稿）。`,
        undoDirId: dirId,
      }, 6500);
      await renderTop10();
    } catch (err) {
      setTop10Toast({ type: "error", message: `操作失败：${err.message}` }, 6000);
      renderTop10FromCache();
    }
  },
  async onUndoRemoveShortlist(dirId) {
    try {
      const res = await api.updateDirection(dirId, {
        status: "shortlisted",
        observed_date: state.selectedDate,
        preset_id: state.directionsFilters.preset_id || "",
      });
      setTop10Toast({
        type: "ok",
        message: `已撤销移出，「${res.direction.name}」已重新放回 Top 10。`,
      }, 3500);
      await renderTop10();
    } catch (err) {
      setTop10Toast({ type: "error", message: `撤销失败：${err.message}` }, 6000);
      renderTop10FromCache();
    }
  },
  async onGenerateVariants(dirId) {
    const effPreset = state.directionsFilters.preset_id || "";
    let preview;
    try {
      preview = await api.generateDirectionVariants(dirId, {
        observed_date: state.selectedDate,
        preset_id: effPreset,
        dry_run: true,
      });
    } catch (err) {
      setTop10Toast({ type: "error", message: `变体建议预检失败：${err.message}` }, 6000);
      renderTop10FromCache();
      return;
    }

    openAiDryRunModal({
      title: `生成 POD 二创变体建议 · ${preview.direction_name || `#${dirId}`}`,
      summaryHtml: `
        <div class="kv-grid">
          <div class="kv-box">
            <div class="kv-label">本次模型调用</div>
            <div class="kv-val" style="color:var(--accent);font-size:19px;">${formatInt(preview.api_calls || 1)} <span style="font-size:12px;font-weight:500;color:var(--ink-muted);">次文本调用</span></div>
          </div>
          <div class="kv-box">
            <div class="kv-label">参考支撑商品</div>
            <div class="kv-val" style="font-size:19px;">${formatInt(preview.linked_count || 0)} <span style="font-size:12px;font-weight:500;color:var(--ink-muted);">件（已有 ${formatInt(preview.existing_count || 0)} 条建议）</span></div>
          </div>
        </div>
        <div style="font-size:12.5px;color:var(--ink-secondary);line-height:1.55;">
          模型：<span class="mono" style="font-weight:700;">${escapeHtml(preview.model || "deepseek-flash")}</span> · 将输出 3~5 条涵盖构图、配色、文案与细分受众角度的可测试 POD 二创变体建议并持久化至方向记录。
        </div>
      `,
      warningText: preview.existing_count > 0
        ? "该方向已有变体建议，确认后将生成全新 3~5 条建议覆盖当前列表，不会改动原始商品与设计备注。"
        : "仅生成并写入该方向的二创变体建议，绝不影响原始商品数据或人工设计备注。",
      confirmLabel: "确认生成 3~5 条变体建议",
      canConfirm: true,
      missingKey: !preview.has_api_key,
      onOpenConfig: () => openAiConfigModal({ onSaved: (cfg) => { state.aiConfig = cfg; } }),
      onConfirm: async () => {
        const res = await api.generateDirectionVariants(dirId, {
          observed_date: state.selectedDate,
          preset_id: effPreset,
          dry_run: false,
        });
        const cnt = (res.variant_suggestions || []).length;
        setTop10Toast({
          type: "ok",
          message: `已为「${res.direction?.name || `#${dirId}`}」生成并保存 ${cnt} 条 POD 二创变体建议。`,
        }, 4500);
        await renderTop10();
      },
    });
  },
};

function renderTop10FromCache() {
  const container = mainEl();
  const effPreset = state.directionsFilters.preset_id || "";
  renderTop10View(container, {
    data: state.top10Data,
    expandedIds: state.top10ExpandedIds,
    editingNoteId: state.top10EditingNoteId,
    presentationMode: Boolean(state.top10PresentationMode),
    toast: state.top10Toast,
    exportUrls: {
      json: api.getDirectionsExportUrl({ format: "json", date: state.selectedDate, preset_id: effPreset }),
      csv: api.getDirectionsExportUrl({ format: "csv", date: state.selectedDate, preset_id: effPreset }),
    },
    handlers: top10Handlers,
  });
}

async function renderTop10() {
  const effPreset = state.directionsFilters.preset_id || "";
  state.top10Data = await api.getTop10Directions({
    date: state.selectedDate,
    preset_id: effPreset,
  });
  renderTop10FromCache();
}

async function renderCurrentRoute(forceFetch = false) {
  hideTooltip();
  closeImageLightbox();
  closeDirectionDetailModal();
  state.directionsDetailModalId = null;
  stopWordCloudPhysics();
  updateTopbarAndNav();
  const container = mainEl();
  try {
    if (state.route === "dashboard") {
      await renderDashboard();
    } else if (state.route === "collection") {
      await renderCollection(forceFetch);
    } else if (state.route === "preprocessing") {
      await renderPreprocessing(forceFetch);
    } else if (state.route === "scoring") {
      await renderScoring();
    } else if (state.route === "print_verification") {
      await renderDirections();
    } else if (state.route === "top10_directions") {
      await renderTop10();
    } else {
      await renderDashboard();
    }
  } catch (err) {
    container.innerHTML = `
      <div class="alert alert-danger">
        <strong>页面加载失败</strong>：${escapeHtml(err.message)}
        <div style="margin-top:8px;">
          <button type="button" class="btn btn-secondary btn-sm" id="btn-retry-load">重试</button>
        </div>
      </div>
    `;
    document.getElementById("btn-retry-load")?.addEventListener("click", () => renderCurrentRoute(true));
  }
}

async function navigateTo(route) {
  if (!ROUTE_META[route]) route = "dashboard";
  state.route = route;
  if (window.location.hash !== `#${route}`) {
    window.history.replaceState(null, "", `#${route}`);
  }
  closeSidebarMobile();
  await renderCurrentRoute(true);
}

function bindGlobalEvents() {
  document.querySelectorAll(".nav-item[data-route]").forEach((btn) => {
    btn.addEventListener("click", () => navigateTo(btn.getAttribute("data-route")));
  });

  document.getElementById("menu-toggle")?.addEventListener("click", () => {
    document.getElementById("app-shell")?.classList.add("sidebar-open");
  });
  document.getElementById("sidebar-close")?.addEventListener("click", closeSidebarMobile);
  document.getElementById("sidebar-backdrop")?.addEventListener("click", closeSidebarMobile);

  document.getElementById("date-select")?.addEventListener("change", async (e) => {
    state.selectedDate = e.target.value;
    state.table.page = 1;
    await renderCurrentRoute(true);
  });

  // Icon-only refresh button with brief spin animation
  document.getElementById("btn-refresh")?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.classList.add("is-spinning");
    try {
      state.health = await api.getHealth();
      renderSidebarHealth();
      await renderCurrentRoute(true);
    } finally {
      setTimeout(() => btn.classList.remove("is-spinning"), 280);
    }
  });

  document.getElementById("drawer-close")?.addEventListener("click", closeDrawer);
  document.getElementById("drawer-overlay")?.addEventListener("click", closeDrawer);
  document.getElementById("review-modal-close")?.addEventListener("click", closeReviewModal);
  document.getElementById("review-modal-overlay")?.addEventListener("click", (e) => {
    if (e.target.id === "review-modal-overlay") closeReviewModal();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (document.getElementById("dir-lightbox-overlay")?.classList.contains("is-open")) {
        closeImageLightbox();
        return;
      }
      if (document.getElementById("review-modal-overlay")?.classList.contains("is-open")) {
        closeReviewModal();
        return;
      }
      if (document.getElementById("dir-detail-overlay")?.classList.contains("is-open")) {
        closeDirectionDetailModal();
        state.directionsDetailModalId = null;
        return;
      }
      closeDrawer();
      closeSidebarMobile();
      hideTooltip();
    }
  });

  window.addEventListener("hashchange", () => {
    const hash = window.location.hash.replace(/^#/, "");
    if (hash && ROUTE_META[hash] && hash !== state.route) {
      state.route = hash;
      renderCurrentRoute(true);
    }
  });
}

async function bootstrap() {
  const initialHash = window.location.hash.replace(/^#/, "");
  if (initialHash && ROUTE_META[initialHash]) {
    state.route = initialHash;
  }
  bindGlobalEvents();
  try {
    state.health = await api.getHealth();
    renderSidebarHealth();
  } catch (err) {
    const container = mainEl();
    container.innerHTML = `<div class="alert alert-danger">初始化失败：${escapeHtml(err.message)}</div>`;
    return;
  }
  await renderCurrentRoute(true);
}

bootstrap();
