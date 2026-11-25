"""Unit & HTTP API tests for Round 2 DeepSeek Vision (deepseek-flash) integration:
- `ai_service.py`: multimodal payload (`detail: low`), fault-tolerant JSON parsing,
  SQLite `product_ai_cache` hit/invalidation, secret masking & local config persistence.
- Stage 02: `POST /api/preprocess/ai-purify` (`dry_run` + mocked run), Plan A invariant
  (0 auto-exclusions), `ai_filter=suggest_exclude` (`is_graphic_print=false & confidence>=0.85`).
- Stage 05: `POST /api/directions/ai-cluster` (`dry_run` + Map-Reduce), multi-label clustering,
  forbidden catch-all/misc rejection, manual/shortlisted direction protection,
  `standalone_candidates` -> 1-click promote as `standalone_hit` -> Stage 06 Top 10 & JSON/CSV export.
- Idempotent SQLite migration of legacy `print_directions` schema.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen

from backend.ai_service import (
    AIResponseParseError,
    AIService,
    build_vision_request_payload,
    compute_evidence_hash,
    is_forbidden_cluster_name,
    parse_llm_json,
)
from backend.directions import DirectionsService
from backend.preprocessing import PreprocessingService
from backend.repository import SnapshotRepository
from backend.scoring import ScoringService
from backend.server import create_server
from tests.test_batch_and_scoring import _build_synthetic_snapshot_db


class TestAIVisionAndClustering(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        self.snap_db = self.tmp_dir / "snap.sqlite3"
        self.app_db = self.tmp_dir / "app.sqlite3"
        self.ai_cfg_path = self.tmp_dir / "ai_config.local"
        _build_synthetic_snapshot_db(self.snap_db)

    def tearDown(self):
        self._tmp.cleanup()

    # ------------------------------------------------------------------
    # 1. `ai_service.py`: Payload format, JSON parser, Cache & Security
    # ------------------------------------------------------------------
    def test_vision_multimodal_payload_json_tolerance_and_cache_dedup(self):
        calls = []

        def mock_http(url, headers, payload, timeout):
            calls.append({"url": url, "headers": headers, "payload": payload})
            # Intentionally wrap JSON in markdown fences + leading prose + trailing comma
            fenced_content = """以下是视觉分析结果：
```json
{
  "is_graphic_print": false,
  "pattern_type": "stripes",
  "confidence": 0.95,
  "filter_reason": "仅横向条纹基础款，无独立印花图案",
  "print_subject": "",
  "slogan_text": "",
  "visual_style": "",
  "theme_tags": [],
}
```
分析完成。"""
            return 200, {
                "choices": [{"message": {"role": "assistant", "content": fenced_content}}]
            }

        ai = AIService(
            app_db_path=self.app_db,
            config_path=self.ai_cfg_path,
            http_requester=mock_http,
            retry_delay=0.0,
        )

        # Save key and verify masked config never exposes plaintext
        saved = ai.save_config(
            {
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-flash",
                "api_key": "sk-test-secret-key-9876",
            }
        )
        cfg = saved["config"]
        self.assertTrue(cfg["has_api_key"])
        self.assertEqual(cfg["model"], "deepseek-flash")
        self.assertEqual(cfg["base_url"], "https://api.deepseek.com/v1")
        self.assertNotIn("sk-test-secret-key-9876", json.dumps(cfg))
        self.assertTrue(cfg["api_key_masked"].endswith("9876"))

        product = {
            "asin": "B0STRIPE01",
            "title": "Womens Casual Striped Long Sleeve Top",
            "brand": "BasicCo",
            "main_image_url": "https://m.media-amazon.com/images/I/stripe01.jpg",
        }

        # First call -> cache miss -> invokes mocked HTTP
        res1, from_cache1 = ai.analyze_product_image(product)
        self.assertFalse(from_cache1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["url"], "https://api.deepseek.com/v1/chat/completions")
        self.assertEqual(calls[0]["headers"]["Authorization"], "Bearer sk-test-secret-key-9876")
        req_body = calls[0]["payload"]
        self.assertEqual(req_body["model"], "deepseek-flash")
        user_content = req_body["messages"][1]["content"]
        self.assertIsInstance(user_content, list)
        img_parts = [p for p in user_content if p.get("type") == "image_url"]
        self.assertEqual(len(img_parts), 1)
        self.assertEqual(
            img_parts[0]["image_url"],
            {"url": "https://m.media-amazon.com/images/I/stripe01.jpg", "detail": "low"},
        )
        self.assertFalse(res1["is_graphic_print"])
        self.assertEqual(res1["pattern_type"], "stripes")
        self.assertAlmostEqual(res1["confidence"], 0.95)
        self.assertTrue(res1["suggest_exclude"])

        # Second call with identical title + image_url -> cache hit -> ZERO extra HTTP calls!
        res2, from_cache2 = ai.analyze_product_image(product)
        self.assertTrue(from_cache2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(res2["pattern_type"], "stripes")
        self.assertTrue(res2["suggest_exclude"])

        # Changing main_image_url invalidates evidence_hash -> triggers fresh HTTP call
        updated_product = {**product, "main_image_url": "https://m.media-amazon.com/images/I/stripe02.jpg"}
        self.assertNotEqual(
            compute_evidence_hash(product["title"], product["main_image_url"]),
            compute_evidence_hash(updated_product["title"], updated_product["main_image_url"]),
        )
        _, from_cache3 = ai.analyze_product_image(updated_product)
        self.assertFalse(from_cache3)
        self.assertEqual(len(calls), 2)

        # Direct parser & forbidden cluster name guards
        self.assertEqual(parse_llm_json('prefix {"a": 1,} suffix'), {"a": 1})
        with self.assertRaises(AIResponseParseError):
            parse_llm_json("not json at all")
        for bad in ("其他", "杂项", "其他/杂项", "未分类", "Misc", "Other"):
            self.assertTrue(is_forbidden_cluster_name(bad), f"Expected forbidden: {bad}")
        self.assertFalse(is_forbidden_cluster_name("复古圣诞猫咪幽默梗"))

        # Verify build_vision_request_payload helper directly
        built = build_vision_request_payload(product, model="deepseek-flash")
        self.assertEqual(built["messages"][1]["content"][1]["image_url"]["detail"], "low")
        self.assertGreaterEqual(built["max_tokens"], 1000)
        self.assertEqual(built.get("thinking"), {"type": "disabled"})

    def test_stage04_custom_weights_auto_persist_and_sync_to_stage05_and_stage06(self):
        """Plan A: Stage 04 slider adjustments persist in SQLite and automatically drive 05 & 06."""
        server, _ = create_server(
            port=0,
            db_path=self.snap_db,
            app_db_path=self.app_db,
            ai_config_path=self.ai_cfg_path,
        )
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def http_json(method, path, body=None):
            data = json.dumps(body).encode("utf-8") if body is not None else None
            req = Request(
                f"http://127.0.0.1:{port}{path}",
                data=data,
                method=method,
                headers={"Content-Type": "application/json"} if data is not None else {},
            )
            with urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))

        try:
            # 1) Baseline Stage 05 scores under system_default
            _, cand_default = http_json("GET", "/api/directions/candidates?top_n=50&assigned=all")
            self.assertEqual(cand_default["active_scoring_config"]["preset_id"], "system_default")
            default_scores = {i["asin"]: i["total_score"] for i in cand_default["items"]}

            # 2) User drags Stage 04 sliders WITHOUT saving a named preset (persist_active=True)
            custom_weights = {"sales": 5, "opportunity": 45, "rating": 5, "freshness": 40, "price": 5}
            code, eval_custom = http_json(
                "POST",
                "/api/scoring/evaluate",
                {"weights": custom_weights, "persist_active": True},
            )
            self.assertEqual(code, 200)
            self.assertTrue(eval_custom["active_config"]["is_custom"])
            self.assertEqual(eval_custom["active_config"]["weights"]["opportunity"], 45.0)
            custom_04_scores = {i["asin"]: i["total_score"] for i in eval_custom["items"]}

            # 3) Reload Stage 04 via GET -> persisted custom weights survive refresh!
            _, eval_reload = http_json("GET", "/api/scoring/evaluate")
            self.assertTrue(eval_reload["active_config"]["is_custom"])
            self.assertEqual(eval_reload["weights"]["freshness"], 40.0)

            # 4) Stage 05 candidates automatically reflect Stage 04's custom weights & exact scores!
            _, cand_custom = http_json("GET", "/api/directions/candidates?top_n=50&assigned=all")
            self.assertTrue(cand_custom["active_scoring_config"]["is_custom"])
            self.assertEqual(cand_custom["active_scoring_config"]["weight_shares"]["opportunity"], 45.0)
            stage05_scores = {i["asin"]: i["total_score"] for i in cand_custom["items"]}
            self.assertEqual(stage05_scores, custom_04_scores)
            self.assertNotEqual(stage05_scores, default_scores)

            # 5) Create a shortlisted direction -> Stage 06 Top 10 also uses active custom weights!
            top_asin = cand_custom["items"][0]["asin"]
            http_json(
                "POST",
                "/api/directions",
                {
                    "name": "自定义权重同步方向",
                    "status": "shortlisted",
                    "asins": [top_asin],
                },
            )
            _, top10 = http_json("GET", "/api/directions/top10")
            self.assertTrue(top10["active_scoring_config"]["is_custom"])
            self.assertEqual(
                top10["items"][0]["supporting_products"][0]["total_score"],
                custom_04_scores[top_asin],
            )
        finally:
            server.shutdown()
            server.server_close()

    def test_reduce_top_level_array_and_truncation_error_handling(self):
        """Verify Reduce tolerates top-level JSON arrays and surfaces clear length truncation errors."""
        # Case 1: Model returns top-level JSON array instead of {"clusters": [...]}
        # Also verify deepseek-flash sets thinking={"type": "disabled"} and max_tokens >= 8000,
        # and gracefully strips `thinking` if a 3rd-party provider returns HTTP 400 for it.
        call_count = {"n": 0}

        def mock_array_http(url, headers, payload, timeout):
            call_count["n"] += 1
            self.assertGreaterEqual(timeout, 120.0)
            self.assertGreaterEqual(payload["max_tokens"], 8000)
            if call_count["n"] == 1:
                self.assertEqual(payload.get("thinking"), {"type": "disabled"})
                return 400, {"error": {"message": "Unknown parameter: thinking"}}
            self.assertNotIn("thinking", payload)
            arr = [
                {
                    "name": "复古万圣节幽灵",
                    "style_tags": ["复古", "万圣节"],
                    "target_audience": "年轻女性",
                    "notes": "高转化节日主题",
                    "asin_list": ["B0TEST0001", "B0TEST0002"],
                }
            ]
            return 200, {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(arr, ensure_ascii=False)}}]}

        ai = AIService(
            app_db_path=self.app_db,
            config_path=self.ai_cfg_path,
            http_requester=mock_array_http,
            retry_delay=0.0,
        )
        ai.save_config({"api_key": "sk-test-array-1234"})
        # Pre-seed cache for 2 synthetic candidates so Map makes 0 image calls
        cands = [
            {"asin": "B0TEST0001", "title": "Vintage Ghost Tee", "image_url": "https://img/1.jpg", "total_score": 80, "listing_sales_30d": 1000, "source_categories": []},
            {"asin": "B0TEST0002", "title": "Spooky Ghost Hoodie", "image_url": "https://img/2.jpg", "total_score": 78, "listing_sales_30d": 900, "source_categories": []},
        ]
        for c in cands:
            ai._save_cache_result(
                asin=c["asin"],
                evidence_hash=compute_evidence_hash(c["title"], c["image_url"]),
                model="deepseek-flash",
                normalized={
                    "is_graphic_print": True,
                    "pattern_type": "graphic_print",
                    "pattern_label": "印花图案",
                    "confidence": 0.95,
                    "filter_reason": "印花",
                    "print_subject": "幽灵",
                    "slogan_text": "",
                    "visual_style": "复古",
                    "theme_tags": ["万圣节"],
                    "suggest_exclude": False,
                },
                raw_obj={},
            )

        res = ai.cluster_candidates(cands, dry_run=False)
        self.assertEqual(len(res["clusters"]), 1)
        self.assertEqual(res["clusters"][0]["name"], "复古万圣节幽灵")
        self.assertEqual(res["clusters"][0]["asins"], ["B0TEST0001", "B0TEST0002"])

        # Case 2: Model hits finish_reason="length" with truncated JSON -> clear Chinese message
        def mock_truncated_http(url, headers, payload, timeout):
            return 200, {"choices": [{"finish_reason": "length", "message": {"content": '{"clusters": [{"name": "截断'}}]}

        ai_trunc = AIService(
            app_db_path=self.app_db,
            config_path=self.ai_cfg_path,
            http_requester=mock_truncated_http,
            retry_delay=0.0,
        )
        with self.assertRaises(AIResponseParseError) as ctx:
            ai_trunc.cluster_candidates(cands, dry_run=False)
        self.assertIn("截断", str(ctx.exception))

    # ------------------------------------------------------------------
    # 2. Legacy `print_directions` Schema Idempotent Migration
    # ------------------------------------------------------------------
    def test_legacy_print_directions_table_migrates_idempotently(self):
        # Create pre-Round-2 print_directions table WITHOUT direction_type and source
        conn = sqlite3.connect(self.app_db)
        conn.executescript(
            """
            CREATE TABLE print_directions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                style_tags TEXT NOT NULL DEFAULT '[]',
                target_audience TEXT NOT NULL DEFAULT '',
                feasibility_rating INTEGER NOT NULL DEFAULT 3,
                notes TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO print_directions
                (name, style_tags, target_audience, feasibility_rating, notes, status, created_at, updated_at)
            VALUES ('已有人工方向', '["复古"]', '女装', 4, '老数据', 'shortlisted', '2026-11-20T00:00:00Z', '2026-11-20T00:00:00Z');
            """
        )
        conn.commit()
        conn.close()

        repo = SnapshotRepository(self.snap_db)
        prep = PreprocessingService(repo, app_db_path=self.app_db)
        scoring = ScoringService(prep, app_db_path=self.app_db)
        directions = DirectionsService(scoring, app_db_path=self.app_db)

        listed = directions.list_directions()
        self.assertEqual(len(listed["items"]), 1)
        row = listed["items"][0]
        self.assertEqual(row["name"], "已有人工方向")
        self.assertEqual(row["direction_type"], "trend_cluster")
        self.assertEqual(row["source"], "manual")
        self.assertEqual(row["status"], "shortlisted")

    # ------------------------------------------------------------------
    # 3. End-to-End HTTP Flow: 02 AI Purify + 05 AI Cluster + Standalone + 06 Export
    # ------------------------------------------------------------------
    def test_http_end_to_end_ai_purify_cluster_standalone_and_top10_export(self):
        http_calls = []

        def mock_deepseek(url, headers, payload, timeout):
            http_calls.append({"url": url, "payload": payload})
            user_content = payload["messages"][-1]["content"]

            # Connectivity test ping
            if isinstance(user_content, str) and user_content.strip() == "Reply with OK":
                return 200, {"choices": [{"message": {"content": "OK"}}]}

            # Vision call (multimodal list with image_url)
            if isinstance(user_content, list):
                text_part = user_content[0]["text"]
                # Parse ASIN from prompt
                asin = ""
                for line in text_part.splitlines():
                    if line.startswith("- ASIN:"):
                        asin = line.split(":", 1)[1].strip()
                        break

                # Route specific ASINs to distinct visual verdicts:
                # - B0PLAIN001 -> solid 0.96 (AI suggest exclude)
                # - B0PLAIN002 -> stripes 0.92 (AI suggest exclude)
                # - B0UNCERT01 -> uncertain 0.65 (NOT suggest exclude, < 0.85)
                # - Others -> graphic_print 0.94 with rich print metadata
                if asin == "B0PLAIN001":
                    ans = {
                        "is_graphic_print": False,
                        "pattern_type": "solid",
                        "confidence": 0.96,
                        "filter_reason": "纯色圆领打底衫，无印花图案",
                        "print_subject": "",
                        "slogan_text": "",
                        "visual_style": "",
                        "theme_tags": [],
                    }
                elif asin == "B0PLAIN002":
                    ans = {
                        "is_graphic_print": False,
                        "pattern_type": "stripes",
                        "confidence": 0.92,
                        "filter_reason": "仅横向条纹，无独立印花",
                        "print_subject": "",
                        "slogan_text": "",
                        "visual_style": "",
                        "theme_tags": [],
                    }
                elif asin == "B0SCORE005":
                    ans = {
                        "is_graphic_print": False,
                        "pattern_type": "uncertain",
                        "confidence": 0.65,
                        "filter_reason": "主图角度偏暗，难以确认印花细节",
                        "print_subject": "",
                        "slogan_text": "",
                        "visual_style": "",
                        "theme_tags": [],
                    }
                else:
                    ans = {
                        "is_graphic_print": True,
                        "pattern_type": "graphic_print",
                        "confidence": 0.94,
                        "filter_reason": "胸前含清晰主题插画与文字标语",
                        "print_subject": f"复古节日猫咪插画 ({asin})",
                        "slogan_text": "THANKFUL & CATS",
                        "visual_style": "复古做旧",
                        "theme_tags": ["感恩节热点", "宠物幽默梗"],
                    }
                return 200, {
                    "choices": [{"message": {"content": json.dumps(ans, ensure_ascii=False)}}]
                }

            # Reduce text-mode clustering call
            # Extract candidate ASINs from JSON array embedded in prompt
            arr_start = user_content.find("[")
            items = json.loads(user_content[arr_start:])
            asins = [i["asin"] for i in items]
            # Guarantee multi-label (asins[0] appears in BOTH cluster 1 & cluster 2),
            # leave asins[-1] (or a top-ranked ASIN) UNGROUPED so it becomes a Standalone Hit!
            # Also include forbidden "其他/杂项" and 1-ASIN cluster to prove server rejects them!
            # Note: pick asins[1], asins[2], asins[3] for clusters and leave asins[0] (Rank #1!) ungrouped!
            cluster_payload = {
                "clusters": [
                    {
                        "name": "复古感恩节热点文字",
                        "style_tags": ["复古做旧", "感恩节", "文字梗"],
                        "target_audience": "年轻女性 / 节日派对",
                        "notes": "多款高分热销商品围绕感恩节幽默文字展开。",
                        "asins": asins[1:4],
                    },
                    {
                        "name": "宠物幽默插画梗",
                        "style_tags": ["卡通手绘", "宠物梗"],
                        "target_audience": "宠物爱好者",
                        "notes": "与感恩节趋势交叉（支持一图多组）。",
                        "asins": [asins[1], asins[2]],  # Multi-label overlap with cluster 1!
                    },
                    {
                        "name": "已有手工精选方向",  # Collides with pre-existing manual shortlisted direction!
                        "style_tags": ["应被保护跳过"],
                        "target_audience": "不应覆盖",
                        "notes": "试图覆盖人工方向",
                        "asins": asins[1:3],
                    },
                    {
                        "name": "其他/杂项",  # Forbidden catch-all -> MUST be rejected!
                        "style_tags": ["杂项"],
                        "target_audience": "通用",
                        "notes": "垃圾桶分组",
                        "asins": asins[0:2],
                    },
                    {
                        "name": "Misc",  # Forbidden English catch-all -> MUST be rejected!
                        "style_tags": ["misc"],
                        "target_audience": "all",
                        "notes": "misc",
                        "asins": asins[0:2],
                    },
                    {
                        "name": "单品凑数组",  # Only 1 ASIN -> MUST be rejected!
                        "style_tags": ["孤品"],
                        "target_audience": "小众",
                        "notes": "不足2件",
                        "asins": [asins[0]],
                    },
                ]
            }
            return 200, {
                "choices": [{"message": {"content": json.dumps(cluster_payload, ensure_ascii=False)}}]
            }

        server, _ = create_server(
            port=0,
            db_path=self.snap_db,
            app_db_path=self.app_db,
            ai_config_path=self.ai_cfg_path,
            ai_requester=mock_deepseek,
        )
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def http_json(method, path, body=None):
            data = json.dumps(body).encode("utf-8") if body is not None else None
            req = Request(
                f"http://127.0.0.1:{port}{path}",
                data=data,
                method=method,
                headers={"Content-Type": "application/json"} if data is not None else {},
            )
            with urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))

        try:
            # 1) Configure AI key via API & verify masked response + test connection
            code, cfg0 = http_json("GET", "/api/ai/config")
            self.assertEqual(code, 200)
            self.assertFalse(cfg0["has_api_key"])
            self.assertEqual(cfg0["model"], "deepseek-flash")

            code, saved = http_json(
                "POST",
                "/api/ai/config",
                {
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-flash",
                    "api_key": "sk-live-masked-4321",
                },
            )
            self.assertEqual(code, 200)
            self.assertTrue(saved["config"]["has_api_key"])
            self.assertNotIn("sk-live-masked-4321", json.dumps(saved))

            code, test_res = http_json("POST", "/api/ai/config", {"action": "test"})
            self.assertEqual(code, 200)
            self.assertTrue(test_res["ok"])
            http_calls.clear()

            # 2) Stage 02 AI Visual Purification:
            # 2a) Default dry_run (skip_suspected_non_print=True, limit=50) skips suspected_non_print items!
            code, dry_default = http_json("POST", "/api/preprocess/ai-purify", {"dry_run": True})
            self.assertEqual(code, 200)
            self.assertTrue(dry_default["skip_suspected_non_print"])
            self.assertEqual(dry_default["limit"], 50)
            self.assertEqual(dry_default["skipped_suspected_non_print_count"], 3)
            self.assertEqual(len(http_calls), 0)

            # 2b) Explicit skip_suspected_non_print=False & limit="all": dry_run MUST make 0 HTTP calls
            code, dry_purify = http_json(
                "POST",
                "/api/preprocess/ai-purify",
                {"dry_run": True, "skip_suspected_non_print": False, "limit": "all"},
            )
            self.assertEqual(code, 200)
            self.assertTrue(dry_purify["dry_run"])
            self.assertGreater(dry_purify["will_analyze_count"], 0)
            self.assertEqual(len(http_calls), 0)
            self.assertFalse(dry_purify["auto_exclude"])

            # 3) Execute Stage 02 AI Purify (mocked, skip_suspected_non_print=False) -> Plan A: 0 auto-exclusions!
            code, run_purify = http_json(
                "POST",
                "/api/preprocess/ai-purify",
                {"dry_run": False, "skip_suspected_non_print": False, "limit": "all"},
            )
            self.assertEqual(code, 200)
            self.assertFalse(run_purify["dry_run"])
            self.assertEqual(run_purify["analyzed_count"], dry_purify["will_analyze_count"])
            self.assertEqual(run_purify["overview"]["counts"]["excluded"], 0)  # Plan A!

            # 4) Verify "AI 建议排除" filter returns ONLY non-print & confidence >= 0.85
            code, ai_excl_page = http_json(
                "GET", "/api/preprocessing/products?ai_filter=suggest_exclude&page_size=50"
            )
            self.assertEqual(code, 200)
            excl_asins = {i["asin"] for i in ai_excl_page["items"]}
            self.assertEqual(excl_asins, {"B0PLAIN001", "B0PLAIN002"})
            for it in ai_excl_page["items"]:
                self.assertFalse(it["ai_analysis"]["is_graphic_print"])
                self.assertGreaterEqual(it["ai_analysis"]["confidence"], 0.85)
                self.assertEqual(it["manual_review_status"], "unreviewed")  # Still not auto-excluded!

            # Second dry_run now shows 0 uncached!
            code, dry_purify2 = http_json(
                "POST",
                "/api/preprocess/ai-purify",
                {"dry_run": True, "skip_suspected_non_print": False, "limit": "all"},
            )
            self.assertEqual(dry_purify2["will_analyze_count"], 0)
            self.assertEqual(dry_purify2["cached_count"], dry_purify["total_candidates"])

            # 5) Create a manual shortlisted direction BEFORE AI clustering to verify protection
            code, manual_created = http_json(
                "POST",
                "/api/directions",
                {
                    "name": "已有手工精选方向",
                    "style_tags": ["手工保留"],
                    "target_audience": "核心客群",
                    "feasibility_rating": 5,
                    "status": "shortlisted",
                    "notes": "人工创建不可被AI覆盖",
                },
            )
            self.assertEqual(code, 201)
            manual_id = manual_created["direction"]["id"]

            # 6) Stage 05 AI Cluster dry_run -> 0 new HTTP calls, 0 new directions
            http_calls.clear()
            code, dry_cl = http_json(
                "POST", "/api/directions/ai-cluster", {"top_n": 60, "dry_run": True}
            )
            self.assertEqual(code, 200)
            self.assertTrue(dry_cl["dry_run"])
            # All candidates were already cached during Stage 02 purify -> 0 image calls needed!
            self.assertEqual(dry_cl["uncached_image_calls"], 0)
            self.assertEqual(dry_cl["reduce_calls"], 1)
            self.assertEqual(len(http_calls), 0)

            # 7) Execute Stage 05 AI Cluster
            code, cl_res = http_json(
                "POST", "/api/directions/ai-cluster", {"top_n": 60, "dry_run": False}
            )
            self.assertEqual(code, 200)
            self.assertEqual(len(http_calls), 1)  # ONLY 1 Reduce call because Map hit cache 100%!
            self.assertEqual(cl_res["created_count"], 2)  # Only the 2 valid clusters created
            self.assertEqual(len(cl_res["skipped_existing"]), 1)  # Manual shortlisted protected!
            rejected_names = {r["name"] for r in cl_res["rejected_clusters"]}
            self.assertIn("其他/杂项", rejected_names)
            self.assertIn("Misc", rejected_names)
            self.assertIn("单品凑数组", rejected_names)

            # Verify created directions have source=ai_suggested, direction_type=trend_cluster, status=draft
            created_map = {d["name"]: d for d in cl_res["created_directions"]}
            self.assertIn("复古感恩节热点文字", created_map)
            self.assertIn("宠物幽默插画梗", created_map)
            for d in cl_res["created_directions"]:
                self.assertEqual(d["source"], "ai_suggested")
                self.assertEqual(d["direction_type"], "trend_cluster")
                self.assertEqual(d["status"], "draft")

            # Verify Multi-Label: same ASIN exists in BOTH trend clusters!
            c1_asins = {p["asin"] for p in created_map["复古感恩节热点文字"]["linked_products"]}
            c2_asins = {p["asin"] for p in created_map["宠物幽默插画梗"]["linked_products"]}
            self.assertGreaterEqual(len(c1_asins & c2_asins), 2)

            # Verify Manual direction remained untouched
            code, manual_check = http_json("GET", f"/api/directions/{manual_id}")
            self.assertEqual(manual_check["direction"]["notes"], "人工创建不可被AI覆盖")
            self.assertEqual(manual_check["direction"]["source"], "manual")
            self.assertEqual(manual_check["direction"]["status"], "shortlisted")

            # Verify Standalone Hits captured Rank #1 ungrouped candidate!
            standalone = cl_res["standalone_candidates"]
            self.assertGreaterEqual(len(standalone), 1)
            top_standalone = standalone[0]
            self.assertNotIn(top_standalone["asin"], c1_asins | c2_asins)
            self.assertTrue(any("Top 20" in r for r in top_standalone["standalone_reasons"]))

            # 8) One-click promote standalone hit -> Stage 06 Top 10 & JSON/CSV export!
            code, promoted = http_json(
                "POST",
                "/api/directions",
                {
                    "name": top_standalone["suggested_name"],
                    "style_tags": top_standalone["suggested_tags"] or ["独立爆款"],
                    "status": "shortlisted",
                    "direction_type": "standalone_hit",
                    "source": "manual",
                    "asins": [top_standalone["asin"]],
                },
            )
            self.assertEqual(code, 201)
            self.assertEqual(promoted["direction"]["direction_type"], "standalone_hit")
            self.assertEqual(promoted["direction"]["status"], "shortlisted")

            # Also shortlist one AI trend cluster so Stage 06 has both types & sources
            ai_dir_id = created_map["复古感恩节热点文字"]["id"]
            http_json("PATCH", f"/api/directions/{ai_dir_id}", {"status": "shortlisted"})

            # Verify Stage 06 Top 10 list
            code, top10 = http_json("GET", "/api/directions/top10")
            self.assertEqual(code, 200)
            by_type = {d["direction_type"] for d in top10["items"]}
            by_source = {d["source"] for d in top10["items"]}
            self.assertIn("standalone_hit", by_type)
            self.assertIn("trend_cluster", by_type)
            self.assertIn("ai_suggested", by_source)
            self.assertIn("manual", by_source)

            # Verify JSON export includes direction_type and source
            with urlopen(f"http://127.0.0.1:{port}/api/directions/export?format=json", timeout=10) as resp:
                export_json = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(len(export_json["directions"]), len(top10["items"]))
            for d in export_json["directions"]:
                self.assertIn(d["direction_type"], ("trend_cluster", "standalone_hit"))
                self.assertIn(d["source"], ("manual", "ai_suggested"))

            # Verify CSV export includes 方向类型 and 创建来源 columns
            with urlopen(f"http://127.0.0.1:{port}/api/directions/export?format=csv", timeout=10) as resp:
                csv_text = resp.read().decode("utf-8-sig")
            rows = list(csv.reader(io.StringIO(csv_text)))
            headers = rows[0]
            self.assertIn("方向类型", headers)
            self.assertIn("创建来源", headers)
            type_idx = headers.index("方向类型")
            src_idx = headers.index("创建来源")
            csv_types = {r[type_idx] for r in rows[1:]}
            csv_sources = {r[src_idx] for r in rows[1:]}
            self.assertIn("独立爆款", csv_types)
            self.assertIn("趋势集群", csv_types)
            self.assertIn("AI 推荐", csv_sources)
            self.assertIn("人工创建", csv_sources)
        finally:
            server.shutdown()
            server.server_close()

    def test_v2_prompt_migration_cache_coexistence_theme_tags_and_enum_fallback(self):
        """P2: Legacy v1 migration, v1/v2 coexistence, dry_run v1-upgrade count, theme_tags norm, enum fallback."""
        from backend.ai_service import normalize_theme_tags, normalize_vision_result
        from backend.config import (
            MOTIF_VOCABULARY,
            SEASON_TAGS,
            TRANSFERABILITY_TYPES,
            VISION_PROMPT_VERSION,
        )

        # Verify controlled vocabulary definitions
        self.assertEqual(VISION_PROMPT_VERSION, "v2")
        self.assertEqual(len(SEASON_TAGS), 9)
        self.assertEqual(len(TRANSFERABILITY_TYPES), 3)
        self.assertEqual(len(MOTIF_VOCABULARY), 46)  # 45 controlled motifs + 'other'
        self.assertIn("other", MOTIF_VOCABULARY)
        self.assertIn("grinch_like", MOTIF_VOCABULARY)

        # 1) theme_tags standardization: lowercase, punctuation strip, singularization, synonyms, garment stopword drop
        norm_tags = normalize_theme_tags(
            ["##Cats!!", "Pumpkins.", "Xmas", "T-Shirt", "Sweatshirt", "Christmas T-Shirt", "Puppies"]
        )
        self.assertEqual(norm_tags, ["cat", "pumpkin", "christmas", "dog"])
        self.assertEqual(normalize_theme_tags("Witches, #GHOSTS!, graphic tee, floral"), ["witch", "ghost", "floral"])

        # 2) Enum whitelist validation + fallback on illegal values
        valid_res = normalize_vision_result(
            {
                "is_graphic_print": True,
                "pattern_type": "graphic_print",
                "confidence": 0.93,
                "print_subject": "感恩节火鸡文字梗",
                "slogan_text": "GOBBLE TIL YOU WOBBLE",
                "visual_style": "复古插画",
                "theme_tags": ["Turkeys!", "Xmas", "t-shirt"],
                "season_tag": "Thanksgiving",
                "motif": "turkey",
                "transferability": "text_pun",
            }
        )
        self.assertEqual(valid_res["prompt_version"], "v2")
        self.assertEqual(valid_res["season_tag"], "thanksgiving")
        self.assertEqual(valid_res["motif"], "turkey")
        self.assertEqual(valid_res["transferability"], "text_pun")
        self.assertEqual(valid_res["theme_tags"], ["turkey", "christmas"])

        bad_enum_res = normalize_vision_result(
            {
                "is_graphic_print": True,
                "pattern_type": "graphic_print",
                "confidence": 0.88,
                "season_tag": "winter_festival_invalid",
                "motif": "alien_spaceship_invalid",
                "transferability": "super_transferable_invalid",
                "theme_tags": ["Retro!!"],
            }
        )
        self.assertEqual(bad_enum_res["season_tag"], "evergreen")
        self.assertEqual(bad_enum_res["motif"], "other")
        self.assertEqual(bad_enum_res["transferability"], "pure_graphic")

        # 3) Legacy v1 table migration (pre-P2 schema without prompt_version/season_tag/motif/transferability)
        legacy_db = self.tmp_dir / "legacy_ai_cache.sqlite3"
        p_v1 = {
            "asin": "B0V1OLD001",
            "title": "Retro Santa Sweatshirt",
            "brand": "HolidayCo",
            "main_image_url": "https://img.example/santa.jpg",
        }
        p_v2 = {
            "asin": "B0V2NEW001",
            "title": "Funny Cat Coffee Tee",
            "brand": "CatCo",
            "main_image_url": "https://img.example/cat.jpg",
        }
        p_fresh = {
            "asin": "B0FRESH001",
            "title": "Spooky Ghost Graphic Hoodie",
            "brand": "GhostCo",
            "main_image_url": "https://img.example/ghost.jpg",
        }
        conn = sqlite3.connect(legacy_db)
        conn.executescript(
            """
            CREATE TABLE product_ai_cache (
                asin TEXT PRIMARY KEY,
                evidence_hash TEXT NOT NULL,
                model TEXT NOT NULL,
                is_graphic_print INTEGER NOT NULL,
                pattern_type TEXT NOT NULL,
                confidence REAL NOT NULL,
                filter_reason TEXT NOT NULL DEFAULT '',
                print_subject TEXT NOT NULL DEFAULT '',
                slogan_text TEXT NOT NULL DEFAULT '',
                visual_style TEXT NOT NULL DEFAULT '',
                theme_tags_json TEXT NOT NULL DEFAULT '[]',
                raw_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """INSERT INTO product_ai_cache
               (asin, evidence_hash, model, is_graphic_print, pattern_type, confidence,
                filter_reason, print_subject, slogan_text, visual_style, theme_tags_json, raw_json, updated_at)
               VALUES (?, ?, 'deepseek-flash', 1, 'graphic_print', 0.92,
                       '胸前含圣诞老人印花', '圣诞老人', 'HO HO HO', '复古手绘', '["christmas"]', '{}', '2026-11-20T00:00:00Z')""",
            (p_v1["asin"], compute_evidence_hash(p_v1["title"], p_v1["main_image_url"])),
        )
        conn.commit()
        conn.close()

        http_calls = []

        def mock_http(url, headers, payload, timeout):
            http_calls.append(payload)
            user_str = json.dumps(payload["messages"], ensure_ascii=False)
            if "B0V2NEW001" in user_str:
                body = {
                    "is_graphic_print": True,
                    "pattern_type": "graphic_print",
                    "confidence": 0.95,
                    "filter_reason": "猫咪喝咖啡插画",
                    "print_subject": "猫咪喝咖啡",
                    "slogan_text": "COFFEE FIRST",
                    "visual_style": "卡通手绘",
                    "theme_tags": ["Cats!", "Coffee", "t-shirt"],
                    "season_tag": "evergreen",
                    "motif": "cat",
                    "transferability": "pure_graphic",
                }
            elif "B0V1OLD001" in user_str:
                body = {
                    "is_graphic_print": True,
                    "pattern_type": "graphic_print",
                    "confidence": 0.94,
                    "filter_reason": "复古圣诞老人印花",
                    "print_subject": "圣诞老人",
                    "slogan_text": "HO HO HO",
                    "visual_style": "复古手绘",
                    "theme_tags": ["Xmas", "Santa"],
                    "season_tag": "christmas",
                    "motif": "santa",
                    "transferability": "pure_graphic",
                }
            else:
                body = {
                    "is_graphic_print": True,
                    "pattern_type": "graphic_print",
                    "confidence": 0.91,
                    "filter_reason": "万圣节幽灵插画",
                    "print_subject": "可爱幽灵",
                    "slogan_text": "BOO",
                    "visual_style": "美式复古",
                    "theme_tags": ["Ghosts", "Halloween"],
                    "season_tag": "halloween",
                    "motif": "ghost",
                    "transferability": "pure_graphic",
                }
            return 200, {"choices": [{"message": {"role": "assistant", "content": json.dumps(body, ensure_ascii=False)}}]}

        ai = AIService(
            app_db_path=legacy_db,
            config_path=self.ai_cfg_path,
            http_requester=mock_http,
            retry_delay=0.0,
        )
        ai.save_config({"api_key": "sk-v2-test-key-1234", "model": "deepseek-flash"})

        # Verify migrated v1 row coexists safely with prompt_version='v1'
        cache_map = ai.get_cache_map()
        self.assertEqual(cache_map["B0V1OLD001"]["prompt_version"], "v1")
        self.assertTrue(cache_map["B0V1OLD001"]["needs_v2_upgrade"])
        self.assertEqual(cache_map["B0V1OLD001"]["print_subject"], "圣诞老人")

        # Populate p_v2 as a genuine v2 cache record (1 HTTP call)
        res_v2, from_cache = ai.analyze_product_image(p_v2)
        self.assertFalse(from_cache)
        self.assertEqual(res_v2["prompt_version"], "v2")
        self.assertEqual(res_v2["motif"], "cat")
        self.assertEqual(res_v2["theme_tags"], ["cat", "coffee"])
        self.assertEqual(len(http_calls), 1)

        # 4) dry_run on [p_v1, p_v2, p_fresh] -> MUST identify 1 v2 hit, 1 new uncached, 1 v1 upgrade, and 0 new HTTP calls!
        dry_purify = ai.purify_products([p_v1, p_v2, p_fresh], dry_run=True)
        self.assertEqual(len(http_calls), 1)  # Zero extra HTTP calls during dry_run!
        self.assertEqual(dry_purify["prompt_version"], "v2")
        self.assertEqual(dry_purify["cached_count"], 1)
        self.assertEqual(dry_purify["new_uncached_count"], 1)
        self.assertEqual(dry_purify["upgrade_v1_count"], 1)
        self.assertEqual(dry_purify["will_analyze_count"], 2)

        dry_cluster = ai.cluster_candidates([p_v1, p_v2, p_fresh], dry_run=True)
        self.assertEqual(len(http_calls), 1)  # Still zero extra HTTP calls!
        self.assertEqual(dry_cluster["cached_count"], 1)
        self.assertEqual(dry_cluster["new_uncached_count"], 1)
        self.assertEqual(dry_cluster["upgrade_v1_count"], 1)
        self.assertEqual(dry_cluster["uncached_image_calls"], 2)
        self.assertEqual(dry_cluster["reduce_calls"], 1)

        # 5) Real run after user confirmation -> executes ONLY 2 calls (skips v2, upgrades v1 + analyzes fresh)
        real_purify = ai.purify_products([p_v1, p_v2, p_fresh], dry_run=False)
        self.assertEqual(len(http_calls), 3)  # 1 initial + 2 incremental
        self.assertEqual(real_purify["cached_skipped"], 1)
        self.assertEqual(real_purify["analyzed_count"], 2)

        # Subsequent dry_run hits 100% v2 cache (0 calls needed)
        dry_after = ai.purify_products([p_v1, p_v2, p_fresh], dry_run=True)
        self.assertEqual(dry_after["cached_count"], 3)
        self.assertEqual(dry_after["new_uncached_count"], 0)
        self.assertEqual(dry_after["upgrade_v1_count"], 0)
        self.assertEqual(dry_after["will_analyze_count"], 0)

        upgraded = ai.get_valid_cached_result(p_v1)
        self.assertIsNotNone(upgraded)
        self.assertEqual(upgraded["prompt_version"], "v2")
        self.assertEqual(upgraded["season_tag"], "christmas")
        self.assertEqual(upgraded["motif"], "santa")
        self.assertEqual(upgraded["transferability"], "pure_graphic")

    def test_ai_purify_skips_missing_images_batches_by_limit_50_and_sorts_by_heat(self):
        """Verify missing-image skipping, skip_suspected_non_print toggle, limit=50 batching, and heat sorting."""
        analyzed_order = []

        def tracking_requester(url, headers, payload, timeout):
            import re as _re
            user_str = json.dumps(payload["messages"], ensure_ascii=False)
            m = _re.search(r"B0(?:HEAT\d{4}|SUSP\d{4}|NOIMG\d{3})", user_str)
            asin = m.group(0) if m else "UNKNOWN"
            analyzed_order.append(asin)
            return 200, {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "is_graphic_print": True,
                                    "pattern_type": "graphic_print",
                                    "confidence": 0.92,
                                    "filter_reason": "清晰印花",
                                    "print_subject": f"插画 {asin}",
                                    "slogan_text": "",
                                    "visual_style": "复古",
                                    "season_tag": "evergreen",
                                    "motif": "cat",
                                    "theme_tags": ["cat"],
                                    "transferability": "pure_graphic",
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            }

        ai = AIService(
            app_db_path=self.tmp_dir / "purify_batch_app.sqlite3",
            config_path=self.tmp_dir / "purify_batch_ai_config.local",
            http_requester=tracking_requester,
        )
        ai.save_config({"api_key": "sk-test-purify-batch-9999"})

        products = []
        # 4 missing-image products -> MUST be skipped unconditionally
        for idx in range(4):
            products.append(
                {
                    "asin": f"B0NOIMG{idx:03d}",
                    "title": f"Missing Image Graphic Tee {idx}",
                    "brand": "NoImgBrand",
                    "image_url": "" if idx % 2 == 0 else None,
                    "automatic_classification": "possible_print",
                    "listing_sales_30d": 9999,
                }
            )
        # 3 suspected_non_print products with images -> skipped by default, included when skip_suspected_non_print=False
        for idx in range(3):
            products.append(
                {
                    "asin": f"B0SUSP{idx:04d}",
                    "title": f"Solid Plain Cotton Tee {idx}",
                    "brand": "BasicBlank",
                    "image_url": f"https://m.media-amazon.com/images/I/71susp{idx:03d}.jpg",
                    "automatic_classification": "suspected_non_print",
                    "listing_sales_30d": 500,
                }
            )
        # 65 possible_print products with varied BSR & sales (put coldest items first in list to test heat sorting!)
        for idx in range(65):
            bsr_rank = 1000 - idx * 10  # idx=64 has BSR #360 (hotter), idx=0 has BSR #1000 (colder)
            sales_val = 300 + idx if idx >= 62 else None  # top 3 items (idx 62, 63, 64) have explicit sales!
            products.append(
                {
                    "asin": f"B0HEAT{idx:04d}",
                    "title": f"Retro Graphic Sweatshirt {idx}",
                    "brand": "PrintBrand",
                    "image_url": f"https://m.media-amazon.com/images/I/71heat{idx:03d}.jpg",
                    "automatic_classification": "possible_print",
                    "listing_sales_30d": sales_val,
                    "source_categories": [{"node_id": "9056986011", "source_category_bsr": bsr_rank}],
                }
            )

        # 1) Default dry_run: limit=50, skip_suspected_non_print=True
        dry1 = ai.purify_products(products, dry_run=True)
        self.assertEqual(dry1["skipped_missing_image_count"], 4)
        self.assertEqual(dry1["skipped_suspected_non_print_count"], 3)
        self.assertEqual(dry1["uncached_count"], 65)
        self.assertEqual(dry1["will_analyze_count"], 50)
        self.assertEqual(dry1["deferred_by_limit_count"], 15)
        self.assertEqual(len(analyzed_order), 0)

        # 2) Execute first batch of 50 (single-threaded max_workers=1 to verify exact submission order, then 8 workers on rest)
        run1 = ai.purify_products(products, dry_run=False, limit=50, skip_suspected_non_print=True, max_workers=1)
        self.assertEqual(run1["analyzed_count"], 50)
        self.assertEqual(run1["skipped_missing_image_count"], 4)
        self.assertEqual(run1["skipped_suspected_non_print_count"], 3)
        self.assertEqual(run1["deferred_by_limit_count"], 15)
        # Hottest 3 with explicit sales (B0HEAT0064 > B0HEAT0063 > B0HEAT0062) MUST come first, followed by best BSR (B0HEAT0061...)!
        self.assertEqual(analyzed_order[:4], ["B0HEAT0064", "B0HEAT0063", "B0HEAT0062", "B0HEAT0061"])
        # None of B0NOIMG* or B0SUSP* or coldest 15 (B0HEAT0000..B0HEAT0014) were called!
        analyzed_set_1 = set(analyzed_order)
        self.assertTrue(analyzed_set_1.isdisjoint({f"B0NOIMG{i:03d}" for i in range(4)}))
        self.assertTrue(analyzed_set_1.isdisjoint({f"B0SUSP{i:04d}" for i in range(3)}))
        self.assertTrue(analyzed_set_1.isdisjoint({f"B0HEAT{i:04d}" for i in range(15)}))

        # 3) Run remaining with limit="all" and skip_suspected_non_print=False using default max_workers=8
        run2 = ai.purify_products(products, dry_run=False, limit="all", skip_suspected_non_print=False)
        self.assertEqual(run2["skipped_missing_image_count"], 4)
        self.assertEqual(run2["skipped_suspected_non_print_count"], 0)
        self.assertEqual(run2["cached_skipped"], 50)
        self.assertEqual(run2["analyzed_count"], 18)  # 15 remaining heat items + 3 suspected_non_print items
        self.assertTrue(set(analyzed_order).isdisjoint({f"B0NOIMG{i:03d}" for i in range(4)}))


if __name__ == "__main__":
    unittest.main()
