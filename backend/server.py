"""HTTP server and REST API router for HotPrint Workbench (Python stdlib)."""

from __future__ import annotations

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from .ai_service import (
    AIConfigError,
    AIRequestError,
    AIResponseParseError,
    AIService,
)
from .analytics import build_market_overview, query_products
from .collector import (
    CollectionConflictError,
    CollectionForbiddenError,
    CollectionService,
    CollectionValidationError,
    build_sorftime_live_executor,
    request_sorftime_api,
)
from .config import (
    DEFAULT_DB_PATH,
    DEFAULT_HOST,
    DEFAULT_LIVE_DB_PATH,
    DEFAULT_PORT,
    FRONTEND_DIR,
    WORKBENCH_AI_CONFIG_PATH,
    WORKBENCH_API_KEY_PATH,
    WORKBENCH_SUPABASE_CONFIG_PATH,
    serialize_categories,
)
from .contracts import build_roadmap_payload
from .directions import (
    DirectionNotFoundError,
    DirectionsService,
    DirectionValidationError,
)
from .preprocessing import (
    DEFAULT_APP_DB_PATH,
    PreprocessingNotFoundError,
    PreprocessingService,
    PreprocessingValidationError,
)
from .repository import SnapshotRepository
from .scoring import (
    ScoringNotFoundError,
    ScoringService,
    ScoringValidationError,
)
from .signals import SignalsService, SignalValidationError
from .supabase_state import SupabaseStateService
from .supabase_sync import (
    SupabaseConfigError,
    SupabaseRequestError,
    SupabaseSyncError,
    SupabaseSyncService,
)

MAX_REQUEST_BYTES = 1 * 1024 * 1024


class WorkbenchApp:
    """Dependency container shared across HTTP requests."""

    def __init__(
        self,
        db_path: Path = DEFAULT_DB_PATH,
        *,
        app_db_path: Path = DEFAULT_APP_DB_PATH,
        allow_live_collection: bool = False,
        workbench_api_key_path: Path = WORKBENCH_API_KEY_PATH,
        ai_config_path: Path = WORKBENCH_AI_CONFIG_PATH,
        supabase_config_path: Path = WORKBENCH_SUPABASE_CONFIG_PATH,
        api_requester=request_sorftime_api,
        ai_requester=None,
        supabase_requester=None,
        supabase_hp_requester=None,
        frontend_dir: Path = FRONTEND_DIR,
    ):
        self.db_path = db_path
        self.app_db_path = app_db_path
        self.frontend_dir = frontend_dir
        self.repo = SnapshotRepository(db_path)
        self.ai = AIService(
            app_db_path=app_db_path,
            config_path=ai_config_path,
            http_requester=ai_requester,
        )
        self.preprocessing = PreprocessingService(self.repo, app_db_path=app_db_path)
        self.scoring = ScoringService(self.preprocessing, app_db_path=app_db_path)
        self.directions = DirectionsService(self.scoring, app_db_path=app_db_path)
        self.signals = SignalsService(self.preprocessing, self.ai, app_db_path=app_db_path)
        supabase_target_db = (
            DEFAULT_LIVE_DB_PATH
            if Path(db_path).resolve() == DEFAULT_DB_PATH.resolve()
            else Path(db_path)
        )
        self.supabase = SupabaseSyncService(
            live_db_path=supabase_target_db,
            config_path=supabase_config_path,
            preprocessing_service=self.preprocessing,
            http_requester=supabase_requester,
        )
        self.supabase_state = SupabaseStateService(
            app_db_path=app_db_path,
            config_path=supabase_config_path,
            directions_service=self.directions,
            http_requester=supabase_hp_requester,
        )
        self.collector = CollectionService(
            self.repo,
            allow_live_collection=allow_live_collection,
            workbench_api_key_path=workbench_api_key_path,
            live_executor=None,
            signals_service=self.signals,
        )
        if allow_live_collection:
            self.collector._live_executor = build_sorftime_live_executor(
                db_path,
                self.collector.resolve_api_key,
                api_requester=api_requester,
                signals_service=self.signals,
            )

    def _apply_prep_scope(self, scope: Dict[str, Any], prep_scope: str) -> Dict[str, Any]:
        """Filter Market Analytics scope using Stage 02 Preprocessing results."""
        mode = (prep_scope or "hide_excluded").strip().lower()
        if mode not in ("printable", "hide_excluded", "all"):
            mode = "hide_excluded"
        if scope["status"].state != "ready":
            scope["prep_scope"] = mode
            return scope

        pf = self.preprocessing.get_dashboard_filter_sets(observed_date=scope.get("selected_date"))
        scope["prep_scope"] = mode
        scope["prep_filter_summary"] = pf["summary"]
        if mode == "all":
            return scope

        excluded = pf["excluded_asins"]
        printable = pf["printable_asins"]

        def keep_asin(asin: str) -> bool:
            if mode == "printable":
                return asin in printable
            return asin not in excluded

        filtered = dict(scope)
        filtered["products"] = [p for p in scope["products"] if keep_asin(p["asin"])]
        filtered["asin_products"] = [p for p in scope.get("asin_products", []) if keep_asin(p["asin"])]
        filtered["observations"] = [o for o in scope["observations"] if keep_asin(o["asin"])]
        filtered["all_date_observations"] = [
            o for o in scope.get("all_date_observations", []) if keep_asin(o["asin"])
        ]
        return filtered

    def dispatch_api(
        self,
        method: str,
        raw_path: str,
        body_bytes: bytes = b"",
    ) -> Optional[Tuple[int, Dict[str, Any]]]:
        parsed = urlparse(raw_path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        if not path.startswith("/api/"):
            return None

        def qstr(key: str, default: str = "") -> str:
            vals = qs.get(key)
            return vals[0] if vals else default

        def qint(key: str, default: int) -> int:
            vals = qs.get(key)
            if not vals:
                return default
            try:
                return int(vals[0])
            except ValueError:
                return default

        if method == "GET" and path == "/api/health":
            status = self.repo.status()
            dates = self.repo.get_available_dates()
            return 200, {
                "ok": True,
                "service": "hotprint-workbench",
                "version": "0.1.2",
                "db_status": status.to_dict(),
                "available_dates": dates,
                "latest_observed_date": dates[0] if dates else None,
                "allow_live_collection": self.collector.allow_live_collection,
                "categories": serialize_categories(),
            }

        if method == "GET" and path == "/api/overview":
            scope = self.repo.get_scope_data(
                observed_date=qstr("date") or None,
                category=qstr("category", "all") or "all",
                dedup_mode=qstr("dedup", "listing") or "listing",
            )
            scope = self._apply_prep_scope(scope, qstr("prep_scope", "hide_excluded"))
            return 200, build_market_overview(scope)

        if method == "GET" and path == "/api/products":
            scope = self.repo.get_scope_data(
                observed_date=qstr("date") or None,
                category=qstr("category", "all") or "all",
                dedup_mode=qstr("dedup", "listing") or "listing",
            )
            scope = self._apply_prep_scope(scope, qstr("prep_scope", "hide_excluded"))
            return 200, query_products(
                scope,
                q=qstr("q"),
                anomaly=qstr("anomaly", "all"),
                price_band=qstr("price_band", "all"),
                brand=qstr("brand"),
                sort=qstr("sort", "listing_sales_30d"),
                order=qstr("order", "desc"),
                page=qint("page", 1),
                page_size=qint("page_size", 20),
            )

        if method == "GET" and path.startswith("/api/products/"):
            asin = unquote(path[len("/api/products/") :]).strip()
            if not asin:
                return 400, {"error": "缺少 ASIN 参数。"}
            detail = self.repo.get_product_detail(asin, observed_date=qstr("date") or None)
            if detail is None:
                return 404, {"error": f"未在当前快照中找到 ASIN：{asin}。"}
            return 200, {"item": detail}

        if method == "GET" and path == "/api/collection/config":
            cfg = self.collector.get_config_and_summary()
            cfg["supabase"] = self.supabase.get_config_status()
            latest_scope = self.repo.get_scope_data(category="all", dedup_mode="listing")
            if latest_scope["status"].state == "ready":
                ov = build_market_overview(latest_scope)
                cfg["snapshot_quality"] = {
                    "selected_date": ov["selected_date"],
                    "unique_asin_count": ov["kpis"]["unique_asin_count"],
                    "clean_asin_count": ov["data_quality"]["clean_asin_count"],
                    "flagged_asin_count": ov["data_quality"]["flagged_asin_count"],
                    "checks": ov["data_quality"]["checks"],
                }
            else:
                cfg["snapshot_quality"] = None
            return 200, cfg

        if method == "GET" and path == "/api/collection/batches":
            status, batches = self.repo.get_batches()
            return 200, {
                "db_status": status.to_dict(),
                "total": len(batches),
                "batches": batches,
            }

        if method == "GET" and path == "/api/roadmap":
            return 200, build_roadmap_payload(self.repo)

        # AI Vision & Clustering Config APIs (masked key only)
        if method == "GET" and path == "/api/ai/config":
            return 200, self.ai.get_config_status()

        if method == "POST" and path == "/api/ai/config":
            try:
                payload = json.loads(body_bytes.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                return 400, {"error": "请求体不是合法的 UTF-8 JSON。"}
            if not isinstance(payload, dict):
                return 400, {"error": "请求体必须为 JSON 对象。"}
            try:
                action = str(payload.get("action") or "save").strip().lower()
                if action == "clear":
                    return 200, self.ai.clear_api_key()
                if action == "test":
                    return 200, self.ai.test_connection(overrides=payload)
                return 200, self.ai.save_config(payload)
            except AIConfigError as exc:
                return 400, {"error": str(exc), "code": "ai_config_error"}
            except (AIRequestError, AIResponseParseError) as exc:
                print(f"[HotPrint AI] {path} failed: {exc}", file=sys.stderr, flush=True)
                return 502, {"error": str(exc), "code": "ai_request_error"}

        # Stage 03: Data Preprocessing endpoints
        if method == "GET" and path == "/api/preprocessing/overview":
            return 200, self.preprocessing.get_overview(observed_date=qstr("date") or None)

        if method == "GET" and path == "/api/preprocessing/products":
            return 200, self.preprocessing.query_products(
                observed_date=qstr("date") or None,
                category=qstr("category", "all"),
                eligibility=qstr("eligibility", "all"),
                auto_class=qstr("auto_class", "all"),
                review_status=qstr("review_status", "all"),
                issue=qstr("issue", "all"),
                ai_filter=qstr("ai_filter", "all"),
                q=qstr("q"),
                sort=qstr("sort", "needs_review_first"),
                order=qstr("order", "desc"),
                page=qint("page", 1),
                page_size=qint("page_size", 20),
            )

        if method == "GET" and path.startswith("/api/preprocessing/products/"):
            asin = unquote(path[len("/api/preprocessing/products/") :]).strip()
            if not asin:
                return 400, {"error": "缺少 ASIN 参数。"}
            detail = self.preprocessing.get_product_detail(asin, observed_date=qstr("date") or None)
            if detail is None:
                return 404, {"error": f"未在预处理快照中找到 ASIN：{asin}。"}
            return 200, {"item": detail}

        # Stage 04 standardized scoring input contract (multi-category aware)
        if method == "GET" and path == "/api/scoring/candidates":
            return 200, self.preprocessing.get_scoring_candidates(
                observed_date=qstr("date") or None,
                category=qstr("category", "all"),
                garment_type=qstr("garment_type", "all"),
                gender=qstr("gender", "all"),
                eligibility=qstr("eligibility", "ready"),
                page=qint("page", 1),
                page_size=qint("page_size", 50),
            )

        # Stage 04 dynamic candidate scoring engine & presets
        if method == "GET" and path == "/api/scoring/presets":
            return 200, {"presets": self.scoring.list_presets(), "active_config": self.scoring.get_active_config()}

        if method == "GET" and path == "/api/scoring/active":
            return 200, {"active_config": self.scoring.get_active_config()}

        if method == "GET" and path == "/api/scoring/evaluate":
            try:
                return 200, self.scoring.evaluate(
                    observed_date=qstr("date") or None,
                    category=qstr("category", "all"),
                    confidence=qstr("confidence", "all"),
                    q=qstr("q", ""),
                )
            except ScoringValidationError as exc:
                return 400, {"error": str(exc), "code": "validation_error"}

        if method == "POST" and path in (
            "/api/scoring/evaluate",
            "/api/scoring/active",
            "/api/scoring/presets",
            "/api/scoring/presets/delete",
        ):
            try:
                payload = json.loads(body_bytes.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                return 400, {"error": "请求体不是合法的 UTF-8 JSON。"}
            if not isinstance(payload, dict):
                return 400, {"error": "请求体必须为 JSON 对象。"}
            try:
                if path == "/api/scoring/active":
                    cfg = self.scoring.set_active_config(
                        weights=payload.get("weights"),
                        price_ranges=payload.get("price_ranges"),
                        preset_id=payload.get("preset_id"),
                        preset_name=payload.get("preset_name"),
                    )
                    return 200, {"active_config": cfg}
                if path == "/api/scoring/evaluate":
                    return 200, self.scoring.evaluate(
                        observed_date=payload.get("observed_date") or payload.get("date") or None,
                        category=str(payload.get("category") or "all"),
                        confidence=str(payload.get("confidence") or "all"),
                        q=str(payload.get("q") or ""),
                        weights=payload.get("weights"),
                        price_ranges=payload.get("price_ranges"),
                        previous_ranks=payload.get("previous_ranks"),
                        persist_active=bool(payload.get("persist_active", False)),
                        active_preset_id=payload.get("active_preset_id") or payload.get("preset_id"),
                    )
                if path == "/api/scoring/presets":
                    return 200, self.scoring.save_preset(payload)
                return 200, self.scoring.delete_preset(str(payload.get("preset_id") or ""))
            except ScoringValidationError as exc:
                return 400, {"error": str(exc), "code": "validation_error"}
            except ScoringNotFoundError as exc:
                return 404, {"error": str(exc), "code": "not_found"}

        if method == "DELETE" and path.startswith("/api/scoring/presets/"):
            preset_id = unquote(path[len("/api/scoring/presets/") :]).strip()
            try:
                return 200, self.scoring.delete_preset(preset_id)
            except ScoringValidationError as exc:
                return 400, {"error": str(exc), "code": "validation_error"}
            except ScoringNotFoundError as exc:
                return 404, {"error": str(exc), "code": "not_found"}

        # Stage 05 Print Pattern Recognition & Direction Grouping APIs
        if method == "GET" and path == "/api/directions/candidates":
            return 200, self.directions.get_candidates(
                observed_date=qstr("date") or None,
                preset_id=qstr("preset_id") or None,
                top_n=qint("top_n", 80),
                category=qstr("category", "all"),
                assigned=qstr("assigned", "all"),
                q=qstr("q", ""),
            )

        # Stage 06 Top 10 Output & Standardized Handoff APIs
        if method == "GET" and path == "/api/directions/top10":
            return 200, self.directions.get_top10(
                observed_date=qstr("date") or None,
                preset_id=qstr("preset_id") or None,
            )

        if method == "GET" and path == "/api/directions/export":
            fmt = qstr("format", "json").strip().lower()
            obs_date = qstr("date") or None
            preset_id = qstr("preset_id") or None
            if fmt == "json":
                pkg = self.directions.build_export_package(
                    observed_date=obs_date, preset_id=preset_id
                )
                snap = pkg.get("data_snapshot_date") or "latest"
                return 200, {
                    "__raw_download__": True,
                    "content_type": "application/json; charset=utf-8",
                    "filename": f"hotprint-top10-directions-{snap}.json",
                    "body": json.dumps(pkg, ensure_ascii=False, indent=2).encode("utf-8"),
                }
            if fmt == "csv":
                filename, csv_bytes = self.directions.build_export_csv(
                    observed_date=obs_date, preset_id=preset_id
                )
                return 200, {
                    "__raw_download__": True,
                    "content_type": "text/csv; charset=utf-8",
                    "filename": filename,
                    "body": csv_bytes,
                }
            return 400, {"error": "不支持的导出格式，仅允许 format=json 或 format=csv。", "code": "validation_error"}

        if method == "GET" and path == "/api/directions":
            return 200, self.directions.list_directions(
                observed_date=qstr("date") or None,
                preset_id=qstr("preset_id") or None,
                sort=qstr("sort", "direction_score"),
                status=qstr("status", "all"),
            )

        if method == "POST" and path == "/api/directions/ai-cluster":
            try:
                payload = json.loads(body_bytes.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                return 400, {"error": "请求体不是合法的 UTF-8 JSON。"}
            if not isinstance(payload, dict):
                return 400, {"error": "请求体必须为 JSON 对象。"}
            try:
                return 200, self.directions.ai_cluster_candidates(
                    self.ai,
                    observed_date=payload.get("observed_date") or payload.get("date") or None,
                    preset_id=payload.get("preset_id") or None,
                    top_n=int(payload.get("top_n") or 60),
                    dry_run=bool(payload.get("dry_run", False)),
                )
            except (DirectionValidationError, AIConfigError, ValueError) as exc:
                return 400, {"error": str(exc), "code": "validation_error"}
            except (AIRequestError, AIResponseParseError) as exc:
                print(f"[HotPrint AI] {path} failed: {exc}", file=sys.stderr, flush=True)
                return 502, {"error": str(exc), "code": "ai_request_error"}

        if method == "POST" and path == "/api/directions":
            try:
                payload = json.loads(body_bytes.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                return 400, {"error": "请求体不是合法的 UTF-8 JSON。"}
            try:
                return 201, self.directions.create_direction(payload)
            except DirectionValidationError as exc:
                return 400, {"error": str(exc), "code": "validation_error"}

        if path.startswith("/api/directions/"):
            rest = path[len("/api/directions/") :]
            parts = [p for p in rest.split("/") if p]
            if len(parts) == 1 and parts[0].isdigit():
                dir_id = int(parts[0])
                try:
                    if method == "GET":
                        return 200, {
                            "direction": self.directions.get_direction(
                                dir_id,
                                observed_date=qstr("date") or None,
                                preset_id=qstr("preset_id") or None,
                            )
                        }
                    if method == "PATCH":
                        try:
                            payload = json.loads(body_bytes.decode("utf-8") or "{}")
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            return 400, {"error": "请求体不是合法的 UTF-8 JSON。"}
                        return 200, self.directions.update_direction(dir_id, payload)
                    if method == "DELETE":
                        return 200, self.directions.delete_direction(dir_id)
                except DirectionValidationError as exc:
                    return 400, {"error": str(exc), "code": "validation_error"}
                except DirectionNotFoundError as exc:
                    return 404, {"error": str(exc), "code": "not_found"}

            if method == "POST" and len(parts) == 2 and parts[0].isdigit() and parts[1] == "generate-variants":
                dir_id = int(parts[0])
                try:
                    payload = json.loads(body_bytes.decode("utf-8") or "{}") if body_bytes else {}
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return 400, {"error": "请求体不是合法的 UTF-8 JSON。"}
                if not isinstance(payload, dict):
                    return 400, {"error": "请求体必须为 JSON 对象。"}
                try:
                    return 200, self.directions.generate_variants(
                        dir_id,
                        self.ai,
                        observed_date=payload.get("observed_date") or qstr("date") or None,
                        preset_id=payload.get("preset_id") or qstr("preset_id") or None,
                        dry_run=bool(payload.get("dry_run", False)),
                    )
                except DirectionNotFoundError as exc:
                    return 404, {"error": str(exc), "code": "not_found"}
                except (DirectionValidationError, AIConfigError, ValueError) as exc:
                    return 400, {"error": str(exc), "code": "validation_error"}
                except (AIRequestError, AIResponseParseError) as exc:
                    print(f"[HotPrint AI] {path} failed: {exc}", file=sys.stderr, flush=True)
                    return 502, {"error": str(exc), "code": "ai_request_error"}

            if len(parts) == 2 and parts[0].isdigit() and parts[1] == "links":
                dir_id = int(parts[0])
                try:
                    payload = json.loads(body_bytes.decode("utf-8") or "{}") if body_bytes else {}
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return 400, {"error": "请求体不是合法的 UTF-8 JSON。"}
                if not isinstance(payload, dict):
                    return 400, {"error": "请求体必须为 JSON 对象。"}
                asins_arg = payload.get("asins")
                if asins_arg is None and qstr("asins"):
                    asins_arg = [x.strip() for x in qstr("asins").split(",") if x.strip()]
                obs_date = payload.get("observed_date") or qstr("date") or None
                preset_id = payload.get("preset_id") or qstr("preset_id") or None
                try:
                    if method == "POST":
                        return 200, self.directions.add_links(
                            dir_id, asins_arg, observed_date=obs_date, preset_id=preset_id
                        )
                    if method == "DELETE":
                        return 200, self.directions.remove_links(
                            dir_id, asins_arg, observed_date=obs_date, preset_id=preset_id
                        )
                except DirectionValidationError as exc:
                    return 400, {"error": str(exc), "code": "validation_error"}
                except DirectionNotFoundError as exc:
                    return 404, {"error": str(exc), "code": "not_found"}

        if method == "POST" and path in (
            "/api/preprocess/ai-purify",
            "/api/preprocessing/ai-purify",
        ):
            try:
                payload = json.loads(body_bytes.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                return 400, {"error": "请求体不是合法的 UTF-8 JSON。"}
            if not isinstance(payload, dict):
                return 400, {"error": "请求体必须为 JSON 对象。"}
            try:
                obs_date = payload.get("observed_date") or payload.get("date") or None
                dry_run = bool(payload.get("dry_run", False))
                raw_limit = payload.get("limit", 50)
                if raw_limit is None:
                    limit_arg: Any = 50
                elif isinstance(raw_limit, str) and raw_limit.strip().lower() in ("all", "unlimited", "0", "-1"):
                    limit_arg = "all"
                else:
                    limit_arg = int(raw_limit)
                skip_suspected = payload.get("skip_suspected_non_print", True)
                target_date, unreviewed = self.preprocessing.get_unreviewed_products(
                    observed_date=obs_date
                )
                result = self.ai.purify_products(
                    unreviewed,
                    dry_run=dry_run,
                    limit=limit_arg,
                    skip_suspected_non_print=skip_suspected,
                )
                result["selected_date"] = target_date
                if not dry_run:
                    result["overview"] = self.preprocessing.get_overview(observed_date=target_date)
                return 200, result
            except (AIConfigError, ValueError) as exc:
                return 400, {"error": str(exc), "code": "ai_config_error"}
            except (AIRequestError, AIResponseParseError) as exc:
                print(f"[HotPrint AI] {path} failed: {exc}", file=sys.stderr, flush=True)
                return 502, {"error": str(exc), "code": "ai_request_error"}

        if method == "GET" and path == "/api/signals/overview":
            return 200, self.signals.get_overview(
                observed_date=qstr("date") or None,
                signal_type=qstr("signal_type", "all"),
                tag_type=qstr("tag_type", "all"),
                q=qstr("q", ""),
            )

        if method == "GET" and path == "/api/signals/products":
            return 200, self.signals.get_signal_products(
                category=qstr("category", "all"),
                ai_status=qstr("ai_status", "all"),
                q=qstr("q", ""),
                sort=qstr("sort", "sales_desc"),
                page=qint("page", 1),
                page_size=qint("page_size", 24),
            )

        if method == "POST" and path == "/api/signals/ai-tag":
            try:
                payload = json.loads(body_bytes.decode("utf-8") or "{}") if body_bytes else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                return 400, {"error": "请求体不是合法的 UTF-8 JSON。"}
            if not isinstance(payload, dict):
                return 400, {"error": "请求体必须为 JSON 对象。"}
            try:
                limit = payload.get("limit")
                if limit is not None:
                    limit = int(limit)
                return 200, self.signals.tag_signals(
                    category=str(payload.get("category") or "all"),
                    dry_run=bool(payload.get("dry_run", False)),
                    limit=limit,
                )
            except (AIConfigError, ValueError) as exc:
                return 400, {"error": str(exc), "code": "ai_config_error"}
            except (AIRequestError, AIResponseParseError) as exc:
                print(f"[HotPrint AI] {path} failed: {exc}", file=sys.stderr, flush=True)
                return 502, {"error": str(exc), "code": "ai_request_error"}

        if method == "POST" and path in (
            "/api/preprocessing/run",
            "/api/preprocessing/reviews",
            "/api/preprocess/batch-review",
            "/api/preprocessing/batch-review",
        ):
            try:
                payload = json.loads(body_bytes.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                return 400, {"error": "请求体不是合法的 UTF-8 JSON。"}
            try:
                if path == "/api/preprocessing/run":
                    obs_date = payload.get("observed_date") if isinstance(payload, dict) else None
                    run_res = self.preprocessing.run_preprocessing(observed_date=obs_date)
                    return 200, {
                        "run": run_res,
                        "overview": self.preprocessing.get_overview(observed_date=obs_date),
                    }
                if path in ("/api/preprocess/batch-review", "/api/preprocessing/batch-review"):
                    return 200, self.preprocessing.submit_batch_review(payload)
                return 200, self.preprocessing.submit_manual_review(payload)
            except PreprocessingValidationError as exc:
                return 400, {"error": str(exc), "code": "validation_error"}
            except PreprocessingNotFoundError as exc:
                return 404, {"error": str(exc), "code": "not_found"}

        if method == "POST" and path == "/api/signals/config":
            try:
                payload = json.loads(body_bytes.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                return 400, {"error": "请求体不是合法的 UTF-8 JSON。"}
            if not isinstance(payload, dict):
                return 400, {"error": "请求体必须为 JSON 对象。"}
            try:
                return 200, self.signals.set_signal_node_id(
                    str(payload.get("key") or ""),
                    payload.get("node_id"),
                )
            except SignalValidationError as exc:
                return 400, {"error": str(exc), "code": "validation_error"}

        # Supabase Read-Only Cloud Data Source Config & Sync APIs
        if method == "GET" and path == "/api/supabase/config":
            return 200, self.supabase.get_config_status()

        if method == "POST" and path == "/api/supabase/config":
            try:
                payload = json.loads(body_bytes.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                return 400, {"error": "请求体不是合法的 UTF-8 JSON。"}
            if not isinstance(payload, dict):
                return 400, {"error": "请求体必须为 JSON 对象。"}
            try:
                action = str(payload.get("action") or "save").strip().lower()
                if action == "clear":
                    return 200, self.supabase.clear_api_key()
                if action == "test":
                    return 200, self.supabase.test_connection(overrides=payload)
                return 200, self.supabase.save_config(payload)
            except SupabaseConfigError as exc:
                return 400, {"error": str(exc), "code": "supabase_config_error"}
            except SupabaseRequestError as exc:
                return 502, {"error": str(exc), "code": "supabase_request_error"}

        if method == "POST" and path == "/api/supabase/sync":
            try:
                payload = json.loads(body_bytes.decode("utf-8") or "{}") if body_bytes else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                return 400, {"error": "请求体不是合法的 UTF-8 JSON。"}
            if not isinstance(payload, dict):
                return 400, {"error": "请求体必须为 JSON 对象。"}
            try:
                dry_run = bool(payload.get("dry_run", True))
                obs_date = payload.get("observed_date") or payload.get("date") or None
                if not dry_run and self.repo.db_path.resolve() != self.supabase.live_db_path.resolve():
                    self.repo.db_path = self.supabase.live_db_path
                    self.db_path = self.supabase.live_db_path
                res = self.supabase.sync_to_local_db(
                    observed_date=obs_date,
                    dry_run=dry_run,
                )
                return 200, res
            except SupabaseConfigError as exc:
                return 400, {"error": str(exc), "code": "supabase_config_error"}
            except SupabaseSyncError as exc:
                return 400, {"error": str(exc), "code": "supabase_sync_error"}
            except SupabaseRequestError as exc:
                return 502, {"error": str(exc), "code": "supabase_request_error"}

        # Supabase Independent Business State Tables (`select_*`) APIs
        if method == "GET" and path in ("/api/supabase/hp-status", "/api/supabase/select-status"):
            try:
                return 200, self.supabase_state.get_hp_status()
            except SupabaseConfigError as exc:
                return 400, {"error": str(exc), "code": "supabase_config_error"}
            except SupabaseRequestError as exc:
                return 502, {"error": str(exc), "code": "supabase_request_error"}

        if method == "POST" and path in (
            "/api/supabase/hp-push",
            "/api/supabase/select-push",
            "/api/supabase/hp-pull",
            "/api/supabase/select-pull",
        ):
            try:
                payload = json.loads(body_bytes.decode("utf-8") or "{}") if body_bytes else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                return 400, {"error": "请求体不是合法的 UTF-8 JSON。"}
            if not isinstance(payload, dict):
                return 400, {"error": "请求体必须为 JSON 对象。"}
            try:
                if path in ("/api/supabase/hp-push", "/api/supabase/select-push"):
                    batch_sz = int(payload.get("batch_size") or 200)
                    return 200, self.supabase_state.push_to_cloud(batch_size=batch_sz)
                page_sz = int(payload.get("page_size") or 500)
                return 200, self.supabase_state.pull_from_cloud(page_size=page_sz)
            except PermissionError as exc:
                return 403, {"error": str(exc), "code": "supabase_write_forbidden"}
            except (SupabaseConfigError, ValueError) as exc:
                return 400, {"error": str(exc), "code": "supabase_config_error"}
            except SupabaseRequestError as exc:
                return 502, {"error": str(exc), "code": "supabase_request_error"}

        if method == "POST" and path in ("/api/collection/plan", "/api/collection/run", "/api/collection/api-key"):
            try:
                payload = json.loads(body_bytes.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                return 400, {"error": "请求体不是合法的 UTF-8 JSON。"}

            try:
                if path == "/api/collection/api-key":
                    if isinstance(payload, dict) and payload.get("action") == "clear":
                        return 200, self.collector.clear_api_key()
                    return 200, self.collector.save_api_key(
                        payload.get("api_key") if isinstance(payload, dict) else ""
                    )
                if path == "/api/collection/plan":
                    return 200, self.collector.create_plan(payload)
                return 200, self.collector.execute_run(payload)
            except CollectionValidationError as exc:
                return 400, {"error": str(exc), "code": "validation_error"}
            except CollectionConflictError as exc:
                return 409, {"error": str(exc), "code": "conflict_error"}
            except CollectionForbiddenError as exc:
                return 403, {"error": str(exc), "code": "live_collection_blocked"}

        return 404, {"error": f"未找到接口：{method} {path}"}


def make_handler(app: WorkbenchApp):
    class WorkbenchHTTPHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(app.frontend_dir), **kwargs)

        def _send_json(self, status_code: int, payload: Dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def _handle_api(self) -> bool:
            if not self.path.startswith("/api/"):
                return False
            body = b""
            if self.command in ("POST", "PATCH", "DELETE"):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self._send_json(400, {"error": "无效的 Content-Length 头。"})
                    return True
                if not 0 <= length <= MAX_REQUEST_BYTES:
                    self._send_json(413, {"error": "请求体过大。"})
                    return True
                body = self.rfile.read(length) if length > 0 else b""
            res = app.dispatch_api(self.command, self.path, body)
            if res is None:
                return False
            code, payload = res
            if isinstance(payload, dict) and payload.get("__raw_download__"):
                raw_bytes = payload["body"]
                self.send_response(code)
                self.send_header("Content-Type", payload["content_type"])
                self.send_header("Content-Length", str(len(raw_bytes)))
                self.send_header(
                    "Content-Disposition",
                    f'attachment; filename="{payload["filename"]}"',
                )
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(raw_bytes)
                self.close_connection = True
                return True
            self._send_json(code, payload)
            return True

        def do_GET(self) -> None:
            if self._handle_api():
                return
            # SPA fallback for clean hash/path navigation
            parsed = urlparse(self.path)
            if parsed.path in ("", "/"):
                self.path = "/index.html"
            super().do_GET()

        def do_POST(self) -> None:
            if not self._handle_api():
                self._send_json(404, {"error": "不支持的 POST 路径。"})

        def do_PATCH(self) -> None:
            if not self._handle_api():
                self._send_json(404, {"error": "不支持的 PATCH 路径。"})

        def do_DELETE(self) -> None:
            if not self._handle_api():
                self._send_json(404, {"error": "不支持的 DELETE 路径。"})

        def end_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            super().end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            # Keep server output concise and never log headers/secrets.
            pass

    return WorkbenchHTTPHandler


def create_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    db_path: Path = DEFAULT_DB_PATH,
    *,
    app_db_path: Path = DEFAULT_APP_DB_PATH,
    allow_live_collection: bool = False,
    workbench_api_key_path: Path = WORKBENCH_API_KEY_PATH,
    ai_config_path: Path = WORKBENCH_AI_CONFIG_PATH,
    supabase_config_path: Path = WORKBENCH_SUPABASE_CONFIG_PATH,
    api_requester=request_sorftime_api,
    ai_requester=None,
    supabase_requester=None,
    supabase_hp_requester=None,
) -> Tuple[ThreadingHTTPServer, WorkbenchApp]:
    app = WorkbenchApp(
        db_path=db_path,
        app_db_path=app_db_path,
        allow_live_collection=allow_live_collection,
        workbench_api_key_path=workbench_api_key_path,
        ai_config_path=ai_config_path,
        supabase_config_path=supabase_config_path,
        api_requester=api_requester,
        ai_requester=ai_requester,
        supabase_requester=supabase_requester,
        supabase_hp_requester=supabase_hp_requester,
    )
    server = ThreadingHTTPServer((host, port), make_handler(app))
    return server, app
