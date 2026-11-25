# Printpool 印花热点智能选品工作台（HotPrint Workbench）

独立设计的 Amazon 美国站印花／刺绣服饰热点选品工作台（已上线阶段 01 数据采集、阶段 02 市场分析看板、阶段 03 数据预处理 V0.1），采用原生 HTML / CSS / ES Modules 与 Python 标准库实现，零第三方依赖，默认只读挂载本地 Sorftime SQLite 快照，不修改旧版 Printpool 业务代码或原始数据库。

---

## 快速启动

环境要求：Python 3.9+（macOS Apple Silicon 开箱即用，无需安装第三方依赖）。

```bash
cd sorftime-example/hotprint-workbench
python3 app.py
```

默认监听：`http://127.0.0.1:8772`

### 可选启动参数

```bash
# 安全模式启动（只读原快照，仅演练 Dry-Run / Mock）
python3 app.py --port 8772

# 启用前端真实 Sorftime 采集（自动克隆至独立工作库 data/category_sales_live.sqlite3，不修改原备份库）
python3 app.py --port 8772 --allow-live-collection
```

> 安全默认策略：直接运行 `python3 app.py` 默认关闭真实付费采集（`allow_live_collection=False`）；显式传入 `--allow-live-collection` 后，可在前端「01 数据采集」中配置本地 Sorftime API 密钥（仅保存在 `data/api_key.local`、`chmod 0600`、已被 `.gitignore` 排除，绝不回传浏览器）并经两步确认发起真实采集，写入独立工作库 `data/category_sales_live.sqlite3`，绝不改写原始 `sorftime-example/category_sales.sqlite3`。阶段 03 预处理结果与人工审核记录保存在独立本地库 `data/preprocessing.sqlite3`。

---

## 目录结构

```text
hotprint-workbench/
├── app.py                   # 本地服务入口（默认 127.0.0.1:8772）
├── README.md                # 项目说明与 API 文档
├── backend/
│   ├── config.py            # 类目配置、默认参数、预算上限与口径声明
│   ├── db.py                # SQLite 只读 URI 连接与库状态诊断（ready/missing/empty）
│   ├── repository.py        # 批次与观察读取、raw_response 安全解析、跨类目 ASIN 去重与同款折叠
│   ├── keywords.py          # 服装无关词清洗与热门印花关键词词云提取引擎
│   ├── analytics.py         # 销量分位数、价格带、散点、类目对比、数据质量与服务端筛选排序
│   ├── preprocessing.py     # 阶段 03 数据预处理引擎（方案 A：规则初筛 + 人工确认排除 + 独立 SQLite 持久化）
│   ├── collector.py         # CategoryProducts 适配器：参数校验、同日跳过、二次确认令牌、防重入锁、dry-run/mock
│   ├── contracts.py         # 阶段 04–06 预留数据契约与产品开发衔接协议草案
│   └── server.py            # ThreadingHTTPServer REST API 路由与静态资源托管
├── data/                    # 运行时独立应用数据库（preprocessing.sqlite3，已 gitignore）
├── frontend/
│   ├── index.html           # 语义化单页应用壳层与确认弹窗
│   ├── css/                 # tokens.css / layout.css / components.css（1440/768/390 响应式）
│   └── js/                  # main.js / state.js / api.js / format.js / charts.js + views/*（含 preprocessing.js）
└── tests/
    ├── fixtures.py          # 独立隔离测试快照工厂
    ├── test_db_and_dedup.py # 只读保护、缺库/空库、跨类目去重、原库不可变性
    ├── test_analytics.py    # 异常价/缺失评分隔离、类目 BSR 排序保护、缺失值置底、关键词词云
    ├── test_collector.py    # 二次确认、防重复提交、预算校验、mock 部分失败重试、live 锁定
    ├── test_preprocessing.py# 阶段 03 自动分类、方案 A 人工确认排除/保留/恢复、持久化、幂等性与评分候选接口
    ├── test_six_categories.py # 六类目统一配置、未配置节点禁采、多类目去重与 BSR 隔离、T恤/卫衣预处理规则
    ├── test_api.py          # HTTP REST API 端到端契约测试
    └── verify_browser.py    # 无头 Chrome（CDP）实渲染验收与多端截图脚本
```

---

## 核心业务口径与数据边界

1. **只读数据安全**：常规展示、分析与预处理上游读取全部通过 SQLite 只读 URI（`?mode=ro` + `PRAGMA query_only = ON`）访问 `sorftime-example/category_sales.sqlite3`；预处理执行快照与人工审核台账独立落盘到 `data/preprocessing.sqlite3`。
2. **六类目统一配置与节点安全**：统一在 `backend/config.py` 维护六大目标类目（全部 6 个 Amazon US Browse Node ID 均已核实：`1044544`、`1045624`、`9056923011`、`9056987011`、`9056929011`、`9056993011`）；任何未核实或 `node_id=None` 的类目自动标记「待配置」并由前后端双重拦截。
3. **估算销量语义**：`listing_sales_30d` 为第三方提供的 Listing 滚动近 30 天估算销量，不等同于真实订单量或单个印花图案销量。
4. **两阶段去重口径分工**：
   - **市场看板（阶段 02）**：采用两层清洗（`200 观察 → 155 ASIN → 142 独立商品`），折叠同品牌同描述／同标题孤立子体，提升宏观分布与榜单可读性，绝不累加销量。
   - **数据预处理（阶段 03）**：严格按 `站点 + ASIN` 去重（`200 观察 → 155 去重 ASIN`），保留多类目来源明细，不静默删除任何 `ParentAsin = Asin` 或疑似同款记录。
5. **方案 A（规则初筛 + 人工确认排除）**：自动规则覆盖 T-Shirt 与 Sweatshirt，仅标记 `possible_print` / `suspected_non_print` / `uncertain` / `conflicting`，绝不自动排除或删除商品；仅人工明确确认排除后才置为 `excluded`，且支持随时撤销恢复与全量审计历史追溯；重新运行预处理时严格保留已有人工审核结论（当关键文本/主图发生变化时提示 `stale` 待复核）。
6. **BSR 类目隔离与异常值透明隔离**：全部类目及待配置类目视图禁止混合排序 `source_category_bsr`；`$0` 售价、缺失星级、`ParentAsin = Asin`、缺失源类目 BSR 均作为独立数据质量标签保留原貌，不编造补值、不默认排除。
7. **样本覆盖声明**：当前快照为 2026-11-19 单日已配置 2/6 类目第 1 页（每类 100 条），不代表全美市场总规模或销量增长趋势；跨类目评分权重与 Top 10 名额分配规则暂未预设。

---

## 主要 HTTP API 契约

| 接口 | 方法 | 说明 |
| --- | --- | --- |
| `/api/health` | `GET` | 服务状态、SQLite 只读诊断（`ready/missing/empty_schema/empty_data`）、安全锁状态 |
| `/api/overview?date=&category=` | `GET` | 市场看板完整聚合指标、销量折线与分位数、价格环形图、散点图、双类目对比、关键词词云、热销 Top 8 |
| `/api/products?date=&category=&q=&anomaly=&price_band=&brand=&sort=&order=&page=&page_size=` | `GET` | 服务端筛选、缺失值置底排序与分页的去重商品列表 |
| `/api/products/{asin}?date=` | `GET` | 单品完整证据详情（含 `raw_response` 多图、各来源类目独立 BSR、数据质量核验） |
| `/api/preprocessing/overview?date=` | `GET` | 阶段 03 预处理统计概览（去重总数、可评分/待核实/已排除/阻断、自动初筛分布与数据质量分布） |
| `/api/preprocessing/run` | `POST` | 执行或重跑预处理（只读原库 → 重建预处理快照，完整保留已有人工审核结论） |
| `/api/preprocessing/products?date=&eligibility=&auto=&manual=&quality=&q=&sort=&page=&page_size=` | `GET` | 预处理商品核验列表（默认待核实优先，支持多维筛选、搜索与分页） |
| `/api/preprocessing/products/{asin}?date=` | `GET` | 单品预处理核验详情（多图、规则命中证据、质量标签与人工审核审计时间线） |
| `/api/preprocessing/reviews` | `POST` | 提交人工审核（`confirm_keep` / `confirm_exclude` / `restore`，支持 `request_id` 幂等防重） |
| `/api/scoring/candidates?date=&eligibility=ready` | `GET` | 阶段 04 商品候选评分标准输入接口（默认仅返回 `ready`，带 `visual_verified: false` 与未实施评分声明） |
| `/api/collection/config` | `GET` | 目标类目配置、北京时间当日去重状态、额度参考与安全锁状态（绝不返回 API Key） |
| `/api/collection/batches` | `GET` | `category_sales_batches` 历史批次台账（含抓取时间、实付额度、总页数与响应字节） |
| `/api/collection/plan` | `POST` | 第一步采集预检：参数/预算校验、同日跳过检测、额度估算与一次性确认令牌签发 |
| `/api/collection/run` | `POST` | 第二步确认执行：校验一次性令牌与并发锁，支持 `dry_run` / `mock`（默认拦截 `live` 返回 HTTP 403） |
| `/api/roadmap` | `GET` | 阶段 04–06（候选评分、印花验证、Top 10 方向输出）真实上游样本数与预留契约 |

---

## 自动化测试与浏览器验收

```bash
cd sorftime-example/hotprint-workbench

# 1. 后端与 API 单元测试（35 项）
python3 -B -m unittest discover -s tests -p 'test_*.py' -v

# 2. 真实无头 Chrome 浏览器多端验收（1440 / 768 / 390 + 截图）
python3 -B tests/verify_browser.py
```
