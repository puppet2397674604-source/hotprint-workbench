/**
 * Centralized HTTP API client for HotPrint Workbench.
 */

async function request(url, options = {}) {
  let response;
  try {
    response = await fetch(url, {
      headers: {
        "Content-Type": "application/json",
        ...(options.headers || {}),
      },
      ...options,
    });
  } catch (err) {
    throw new Error(`无法连接本地服务（${err.message || "网络异常"}），请确认端口 8772 正在运行。`);
  }

  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }

  if (!response.ok) {
    const message = (data && data.error) || `接口请求失败（HTTP ${response.status}）`;
    const error = new Error(message);
    error.status = response.status;
    error.code = data && data.code;
    error.payload = data;
    throw error;
  }

  return data;
}

export const api = {
  getHealth() {
    return request("/api/health");
  },
  getOverview({ date = "", category = "all", dedup = "listing", prep_scope = "printable" } = {}) {
    const params = new URLSearchParams();
    if (date) params.set("date", date);
    if (category) params.set("category", category);
    if (dedup) params.set("dedup", dedup);
    if (prep_scope) params.set("prep_scope", prep_scope);
    return request(`/api/overview?${params.toString()}`);
  },
  getProducts({
    date = "",
    category = "all",
    dedup = "listing",
    prep_scope = "printable",
    q = "",
    anomaly = "all",
    price_band = "all",
    brand = "",
    sort = "listing_sales_30d",
    order = "desc",
    page = 1,
    page_size = 20,
  } = {}) {
    const params = new URLSearchParams();
    if (date) params.set("date", date);
    if (category) params.set("category", category);
    if (dedup) params.set("dedup", dedup);
    if (prep_scope) params.set("prep_scope", prep_scope);
    if (q) params.set("q", q);
    if (anomaly && anomaly !== "all") params.set("anomaly", anomaly);
    if (price_band && price_band !== "all") params.set("price_band", price_band);
    if (brand) params.set("brand", brand);
    if (sort) params.set("sort", sort);
    if (order) params.set("order", order);
    params.set("page", String(page));
    params.set("page_size", String(page_size));
    return request(`/api/products?${params.toString()}`);
  },
  getProductDetail(asin, date = "") {
    const qs = date ? `?date=${encodeURIComponent(date)}` : "";
    return request(`/api/products/${encodeURIComponent(asin)}${qs}`);
  },
  getCollectionConfig() {
    return request("/api/collection/config");
  },
  getCollectionBatches() {
    return request("/api/collection/batches");
  },
  planCollection(payload) {
    return request("/api/collection/plan", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  runCollection(payload) {
    return request("/api/collection/run", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  saveCollectionApiKey(apiKey) {
    return request("/api/collection/api-key", {
      method: "POST",
      body: JSON.stringify({ api_key: apiKey }),
    });
  },
  clearCollectionApiKey() {
    return request("/api/collection/api-key", {
      method: "POST",
      body: JSON.stringify({ action: "clear" }),
    });
  },
  getSupabaseConfig() {
    return request("/api/supabase/config");
  },
  saveSupabaseConfig(payload = {}) {
    return request("/api/supabase/config", {
      method: "POST",
      body: JSON.stringify({ action: "save", ...payload }),
    });
  },
  clearSupabaseApiKey() {
    return request("/api/supabase/config", {
      method: "POST",
      body: JSON.stringify({ action: "clear" }),
    });
  },
  testSupabaseConfig(payload = {}) {
    return request("/api/supabase/config", {
      method: "POST",
      body: JSON.stringify({ action: "test", ...payload }),
    });
  },
  syncSupabaseData(payload = {}) {
    return request("/api/supabase/sync", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  getSupabaseHpStatus() {
    return request("/api/supabase/hp-status");
  },
  pushSupabaseHpState(payload = {}) {
    return request("/api/supabase/hp-push", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  pullSupabaseHpState(payload = {}) {
    return request("/api/supabase/hp-pull", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  getPreprocessingOverview(date = "") {
    const qs = date ? `?date=${encodeURIComponent(date)}` : "";
    return request(`/api/preprocessing/overview${qs}`);
  },
  runPreprocessing(observedDate = "") {
    return request("/api/preprocessing/run", {
      method: "POST",
      body: JSON.stringify(observedDate ? { observed_date: observedDate } : {}),
    });
  },
  getPreprocessingProducts({
    date = "",
    category = "all",
    eligibility = "all",
    auto_class = "all",
    review_status = "all",
    issue = "all",
    ai_filter = "all",
    q = "",
    sort = "needs_review_first",
    order = "desc",
    page = 1,
    page_size = 20,
  } = {}) {
    const params = new URLSearchParams();
    if (date) params.set("date", date);
    if (category && category !== "all") params.set("category", category);
    if (eligibility && eligibility !== "all") params.set("eligibility", eligibility);
    if (auto_class && auto_class !== "all") params.set("auto_class", auto_class);
    if (review_status && review_status !== "all") params.set("review_status", review_status);
    if (issue && issue !== "all") params.set("issue", issue);
    if (ai_filter && ai_filter !== "all") params.set("ai_filter", ai_filter);
    if (q) params.set("q", q);
    if (sort) params.set("sort", sort);
    if (order) params.set("order", order);
    params.set("page", String(page));
    params.set("page_size", String(page_size));
    return request(`/api/preprocessing/products?${params.toString()}`);
  },
  getAiConfig() {
    return request("/api/ai/config");
  },
  saveAiConfig(payload) {
    return request("/api/ai/config", {
      method: "POST",
      body: JSON.stringify({ action: "save", ...payload }),
    });
  },
  clearAiApiKey() {
    return request("/api/ai/config", {
      method: "POST",
      body: JSON.stringify({ action: "clear" }),
    });
  },
  testAiConfig(payload = {}) {
    return request("/api/ai/config", {
      method: "POST",
      body: JSON.stringify({ action: "test", ...payload }),
    });
  },
  runAiPurify(payload = {}) {
    return request("/api/preprocess/ai-purify", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  runAiCluster(payload = {}) {
    return request("/api/directions/ai-cluster", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  getSignalsOverview({ date = "", signal_type = "all", tag_type = "all", q = "" } = {}) {
    const params = new URLSearchParams();
    if (date) params.set("date", date);
    if (signal_type && signal_type !== "all") params.set("signal_type", signal_type);
    if (tag_type && tag_type !== "all") params.set("tag_type", tag_type);
    if (q) params.set("q", q);
    return request(`/api/signals/overview?${params.toString()}`);
  },
  runSignalsAiTag(payload = {}) {
    return request("/api/signals/ai-tag", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  getPreprocessingProductDetail(asin, date = "") {
    const qs = date ? `?date=${encodeURIComponent(date)}` : "";
    return request(`/api/preprocessing/products/${encodeURIComponent(asin)}${qs}`);
  },
  submitPreprocessingReview(payload) {
    return request("/api/preprocessing/reviews", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  submitPreprocessingBatchReview(payload) {
    return request("/api/preprocess/batch-review", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  evaluateScoring(payload) {
    return request("/api/scoring/evaluate", {
      method: "POST",
      body: JSON.stringify(payload || {}),
    });
  },
  getScoringPresets() {
    return request("/api/scoring/presets");
  },
  saveScoringPreset(payload) {
    return request("/api/scoring/presets", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  deleteScoringPreset(presetId) {
    return request(`/api/scoring/presets/${encodeURIComponent(presetId)}`, {
      method: "DELETE",
    });
  },
  getDirectionCandidates({
    date = "",
    preset_id = "",
    top_n = 80,
    category = "all",
    assigned = "unassigned",
    q = "",
  } = {}) {
    const params = new URLSearchParams();
    if (date) params.set("date", date);
    if (preset_id) params.set("preset_id", preset_id);
    params.set("top_n", String(top_n));
    if (category && category !== "all") params.set("category", category);
    if (assigned) params.set("assigned", assigned);
    if (q) params.set("q", q);
    return request(`/api/directions/candidates?${params.toString()}`);
  },
  getDirections({ date = "", preset_id = "", sort = "direction_score", status = "all" } = {}) {
    const params = new URLSearchParams();
    if (date) params.set("date", date);
    if (preset_id) params.set("preset_id", preset_id);
    if (sort) params.set("sort", sort);
    if (status && status !== "all") params.set("status", status);
    return request(`/api/directions?${params.toString()}`);
  },
  createDirection(payload) {
    return request("/api/directions", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  updateDirection(id, payload) {
    return request(`/api/directions/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
  },
  deleteDirection(id) {
    return request(`/api/directions/${encodeURIComponent(id)}`, {
      method: "DELETE",
    });
  },
  generateDirectionVariants(id, payload = {}) {
    return request(`/api/directions/${encodeURIComponent(id)}/generate-variants`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  addDirectionLinks(id, payload) {
    return request(`/api/directions/${encodeURIComponent(id)}/links`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  removeDirectionLinks(id, payload) {
    return request(`/api/directions/${encodeURIComponent(id)}/links`, {
      method: "DELETE",
      body: JSON.stringify(payload),
    });
  },
  getTop10Directions({ date = "", preset_id = "" } = {}) {
    const params = new URLSearchParams();
    if (date) params.set("date", date);
    if (preset_id) params.set("preset_id", preset_id);
    const qs = params.toString();
    return request(`/api/directions/top10${qs ? `?${qs}` : ""}`);
  },
  getDirectionsExportUrl({ format = "json", date = "", preset_id = "" } = {}) {
    const params = new URLSearchParams();
    params.set("format", format);
    if (date) params.set("date", date);
    if (preset_id) params.set("preset_id", preset_id);
    return `/api/directions/export?${params.toString()}`;
  },
  getScoringCandidates({
    date = "",
    category = "all",
    garment_type = "all",
    gender = "all",
    eligibility = "ready",
    page = 1,
    page_size = 50,
  } = {}) {
    const params = new URLSearchParams();
    if (date) params.set("date", date);
    if (category && category !== "all") params.set("category", category);
    if (garment_type && garment_type !== "all") params.set("garment_type", garment_type);
    if (gender && gender !== "all") params.set("gender", gender);
    if (eligibility) params.set("eligibility", eligibility);
    params.set("page", String(page));
    params.set("page_size", String(page_size));
    return request(`/api/scoring/candidates?${params.toString()}`);
  },
  saveSignalNodeConfig({ key, node_id = "" } = {}) {
    return request("/api/signals/config", {
      method: "POST",
      body: JSON.stringify({ key, node_id }),
    });
  },
  getSignalProducts({
    category = "all",
    ai_status = "all",
    q = "",
    sort = "sales_desc",
    page = 1,
    page_size = 24,
  } = {}) {
    const params = new URLSearchParams();
    if (category && category !== "all") params.set("category", category);
    if (ai_status && ai_status !== "all") params.set("ai_status", ai_status);
    if (q) params.set("q", q);
    if (sort) params.set("sort", sort);
    params.set("page", String(page));
    params.set("page_size", String(page_size));
    return request(`/api/signals/products?${params.toString()}`);
  },
};
