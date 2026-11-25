"""Future-stage (Stages 03-06) product contracts and readiness metadata.

Explicitly marks unimplemented stages as planned without fabricating scores,
visual print detection, or Top 10 directions.
"""

from __future__ import annotations

from typing import Any, Dict

from .config import CATEGORIES, CONFIGURED_CATEGORIES, PENDING_CATEGORIES, serialize_categories
from .repository import SnapshotRepository


def build_roadmap_payload(repo: SnapshotRepository) -> Dict[str, Any]:
    scope = repo.get_scope_data(category="all")
    ready = scope["status"].state == "ready"
    products = scope["products"] if ready else []
    unique_count = len(products)
    same_parent = sum(1 for p in products if p["needs_parent_verification"])
    invalid_price = sum(1 for p in products if not p["price_valid"])
    missing_rating = sum(1 for p in products if p["rating_status"] != "valid")

    return {
        "db_ready": ready,
        "selected_date": scope.get("selected_date"),
        "target_categories": serialize_categories(),
        "category_readiness": {
            "total_target_categories": len(CATEGORIES),
            "configured_count": len(CONFIGURED_CATEGORIES),
            "pending_config_count": len(PENDING_CATEGORIES),
            "pending_en_names": [c.en_name for c in PENDING_CATEGORIES],
            "cross_category_quota_policy": "TBD — 跨类目最终评分权重与 Top 10 名额分配规则尚未确定，本阶段仅预留多类目数据结构与过滤接口。",
        },
        "input_pool_summary": {
            "unique_asin_count": unique_count,
            "same_parent_verification_pending": same_parent,
            "invalid_price_count": invalid_price,
            "missing_rating_count": missing_rating,
            "visual_verified_count": 0,
            "note": "当前已扩展至六大目标类目架构（已核实 2 类目、待配置 4 类目），暂未执行自动视觉识别、跨类目评分权重或 Top 10 名额分配。",
        },
        "stages": [
            {
                "id": "preprocessing",
                "step": "02",
                "name": "数据预处理",
                "en_name": "Data Preprocessing & Human-Confirmed Exclusion",
                "status": "implemented",
                "status_label": "V0.1 已上线 · 规则初筛 + 人工确认排除",
                "summary": "已完成规则初筛（纯色/空白/条纹/冲突识别）、多维数据质量检查、人工确认保留/排除/恢复持久化及面向评分阶段的标准输入契约。",
                "input_contract": {
                    "source": "Stage 01/02 只读 SQLite 快照（按站点 + ASIN 去重）",
                    "output_api": "GET /api/scoring/candidates?eligibility=ready",
                },
                "rules_planned": [
                    "自动规则仅提示疑点（suspected_non_print / conflicting），绝不自动正式排除。",
                    "人工审核结论持久化在独立应用库 data/preprocessing.sqlite3，可随时撤销恢复并保留完整历史。",
                ],
                "reserved_schema": {
                    "api": "GET /api/scoring/candidates?eligibility=ready",
                    "contract_version": "hotprint-scoring-input-v0.1",
                },
            },
            {
                "id": "scoring",
                "step": "04",
                "name": "商品候选评分",
                "en_name": "Candidate Scoring Engine V1",
                "status": "implemented",
                "status_label": "V1 已上线 · 动态权重评分 + 方案持久化",
                "summary": "基于预处理 Ready 合格池，以六类目统一竞争口径计算 5 维动态潜力分（销量、动销机会、贝叶斯星级、新鲜度、价格适配）与独立数据可信度，支持实时调权、排名变化追踪与方案保存。",
                "input_contract": {
                    "source": "数据预处理核验通过的 Ready 商品池（跨类目按 ASIN 去重）",
                    "output_api": "POST /api/scoring/evaluate",
                },
                "rules_planned": [
                    "缺失维度绝不补 0，动态剔除后自动重归一化剩余有效维度权重。",
                    "0 销量或缺失销量商品不参与主榜排名，单独折叠至底部无销量区。",
                ],
                "reserved_schema": {
                    "api": "POST /api/scoring/evaluate",
                    "presets_api": "GET/POST/DELETE /api/scoring/presets",
                    "scoring_version": "scoring-v1.0",
                },
            },
            {
                "id": "print_verification",
                "step": "05",
                "name": "印花识别与归组",
                "en_name": "Visual Pattern Grouping & Direction Aggregation",
                "status": "implemented",
                "status_label": "V1 已上线 · 人工核图 + 印花方向归组与聚合",
                "summary": "基于 04 候选评分 Top N 商品池，支持高清核图放大、多选批量归组、新建/编辑印花方向、独立父体广度与跨类目聚合评分及入选 Top 10 候选标记。",
                "input_contract": {
                    "source": "Stage 04 候选评分 Top N 商品池（支持切换已保存权重方案）",
                    "output_api": "GET /api/directions?status=shortlisted",
                },
                "rules_planned": [
                    "严禁累加多个商品的 listing_sales_30d，统一以独立父体数、峰值评分、最高/中位月销衡量方向潜力。",
                    "方向与商品为多对多关系，同一方向内联合唯一约束防重复关联，删除方向不影响原始商品数据。",
                ],
                "reserved_schema": {
                    "api": "GET/POST /api/directions, POST/DELETE /api/directions/<id>/links",
                    "tables": ["print_directions", "direction_asin_links"],
                },
            },
            {
                "id": "top10_directions",
                "step": "06",
                "name": "Top 10 印花方向输出",
                "en_name": "Top 10 Printable Direction Output & Handoff",
                "status": "implemented",
                "status_label": "V1 已上线 · 决策卡片展示 + JSON/CSV 交接导出",
                "summary": "读取 05 标记入选（shortlisted）的印花方向，按方向综合推荐分降序呈现视觉参考大图、商业依据与设计交接备注，遵循宁缺毋滥原则，支持一键导出 JSON 交接包与 Excel 友好 CSV。",
                "input_contract": {
                    "source": "Stage 05 标记为 shortlisted 的印花方向与关联 ASIN",
                    "output_api": "GET /api/directions/top10 & GET /api/directions/export?format=json|csv",
                },
                "rules_planned": [
                    "入选不足 10 个时如实展示实际数量，绝不强行凑满；超过 10 个时提示精简。",
                    "严禁累加商品销量，统一透传独立父体数、最高/中位月销、价格带与支撑商品明细。",
                ],
                "reserved_schema": {
                    "api": "GET /api/directions/top10",
                    "export_api": "GET /api/directions/export?format=json|csv",
                    "handoff_contract_version": "hotprint-top10-handoff-v1.0",
                },
            },
        ],
    }
