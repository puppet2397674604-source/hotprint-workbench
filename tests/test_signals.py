"""Unit & HTTP API tests for P4 Cross-Category Signal Layer.

Verifies:
1. Writing to `signal_observations` & `signal_ai_cache` NEVER leaks into Stage 02
   preprocessing or Stage 04 candidate scoring pools (100% isolation).
2. `form_dependent` items are strictly filtered out of cross-category heat comparison.
3. Gap Signal (`gap`) and Validated Signal (`validated`) classification relies strictly
   on within-category relative sales percentiles (never comparing raw sales or mixing BSR).
4. Unconfigured signal categories (`signal_mugs`, `signal_tote_bags`, `signal_phone_cases`
   with `node_id = None`) are hard-blocked on server (`CollectionValidationError` / HTTP 400).
5. `POST /api/signals/ai-tag` with `dry_run=True` makes zero external API calls, and
   mocked execution writes strictly to `signal_ai_cache` (never `product_ai_cache`).
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from backend.collector import CollectionValidationError
from backend.config import (
    CATEGORIES,
    DEFAULT_DB_PATH,
    SIGNAL_CATEGORIES,
    VISION_PROMPT_VERSION,
)
from backend.server import WorkbenchApp, create_server
from backend.signals import compute_cross_category_signals


class TestCrossCategorySignalsPure(unittest.TestCase):
    """Pure math tests for within-category relative percentiles, form_dependent filtering,
    and Gap vs Validated classification."""

    def test_form_dependent_filtered_and_gap_vs_validated_relative_percentiles(self):
        # Core apparel pool: 10 items in 2 apparel nodes with sales in range 100..2,000
        # - `cat` motif: 3 high-sales core items (>= P70 in core) -> Core STRONG
        # - `frog` motif: 0 core items -> Core WEAK (Gap candidate!)
        # - `checkerboard_pattern` motif: 3 high-sales items marked `form_dependent` -> MUST BE FILTERED OUT!
        # - `coffee` motif: 4 low-sales core items (< P40 in core) -> Core WEAK
        core_items = []
        core_specs = [
            # (asin, node_id, sales, motif, theme_tags, transferability)
            ("B0CORE0001", "9056923011", 100, "coffee", ["coffee lover"], "pure_graphic"),
            ("B0CORE0002", "9056923011", 150, "coffee", ["coffee lover"], "pure_graphic"),
            ("B0CORE0003", "9056923011", 200, "dog", ["cute dog"], "pure_graphic"),
            ("B0CORE0004", "9056923011", 300, "dog", ["cute dog"], "pure_graphic"),
            ("B0CORE0005", "9056923011", 400, "floral", ["boho floral"], "pure_graphic"),
            ("B0CORE0006", "9056923011", 600, "checkerboard_pattern", ["sequin stripes"], "form_dependent"),
            ("B0CORE0007", "9056923011", 800, "checkerboard_pattern", ["sequin stripes"], "form_dependent"),
            ("B0CORE0008", "9056923011", 1200, "cat", ["funny cat"], "text_pun"),
            ("B0CORE0009", "9056923011", 1600, "cat", ["funny cat"], "pure_graphic"),
            ("B0CORE0010", "9056923011", 2000, "cat", ["funny cat"], "pure_graphic"),
        ]
        for asin, nid, sales, motif, tags, tr in core_specs:
            core_items.append(
                {
                    "asin": asin,
                    "listing_sales_30d": sales,
                    "source_categories": [{"node_id": nid}],
                    "ai_analysis": {
                        "is_graphic_print": True,
                        "motif": motif,
                        "theme_tags": tags,
                        "transferability": tr,
                    },
                }
            )

        # Signal category 1: Mugs — wildly higher absolute sales scale (10,000..100,000)
        # Signal category 2: Tote Bags — wildly lower absolute sales scale (10..100)
        # Because computation is strictly within-category relative percentile, BOTH scales work cleanly!
        mugs_specs = [
            ("B0MUG00001", 10000, "dog", ["cute dog"], "pure_graphic"),
            ("B0MUG00002", 15000, "dog", ["cute dog"], "pure_graphic"),
            ("B0MUG00003", 20000, "floral", ["boho floral"], "pure_graphic"),
            ("B0MUG00004", 30000, "floral", ["boho floral"], "pure_graphic"),
            ("B0MUG00005", 40000, "checkerboard_pattern", ["sequin stripes"], "form_dependent"),
            ("B0MUG00006", 55000, "checkerboard_pattern", ["sequin stripes"], "form_dependent"),
            ("B0MUG00007", 70000, "cat", ["funny cat"], "pure_graphic"),
            ("B0MUG00008", 80000, "cat", ["funny cat"], "text_pun"),
            ("B0MUG00009", 90000, "frog", ["sarcastic frog"], "text_pun"),
            ("B0MUG00010", 100000, "frog", ["sarcastic frog"], "pure_graphic"),
        ]
        totes_specs = [
            ("B0TOTE0001", 10, "dog", ["cute dog"], "pure_graphic"),
            ("B0TOTE0002", 20, "dog", ["cute dog"], "pure_graphic"),
            ("B0TOTE0003", 30, "floral", ["boho floral"], "pure_graphic"),
            ("B0TOTE0004", 40, "floral", ["boho floral"], "pure_graphic"),
            ("B0TOTE0005", 80, "coffee", ["coffee lover"], "pure_graphic"),
            ("B0TOTE0006", 90, "coffee", ["coffee lover"], "text_pun"),
            ("B0TOTE0007", 100, "coffee", ["coffee lover"], "pure_graphic"),
        ]

        def _make_sig_list(specs):
            return [
                {
                    "asin": asin,
                    "listing_sales_30d": sales,
                    "ai_analysis": {
                        "is_graphic_print": True,
                        "motif": motif,
                        "theme_tags": tags,
                        "transferability": tr,
                    },
                }
                for asin, sales, motif, tags, tr in specs
            ]

        result = compute_cross_category_signals(
            core_items,
            {
                "signal_mugs": _make_sig_list(mugs_specs),
                "signal_tote_bags": _make_sig_list(totes_specs),
                "signal_phone_cases": [],
            },
        )

        summary = result["summary"]
        self.assertEqual(summary["core_filtered_form_dependent"], 2)
        self.assertEqual(summary["signal_filtered_form_dependent"], 2)

        by_key = {row["tag_key"]: row for row in result["items"]}

        # 1) `form_dependent` motif and theme_tag MUST be completely absent
        self.assertNotIn("motif:checkerboard_pattern", by_key)
        self.assertNotIn("theme_tag:sequin stripes", by_key)

        # 2) `frog` (strong in Mugs >= P70, 0 in core) -> GAP SIGNAL
        self.assertIn("motif:frog", by_key)
        frog_row = by_key["motif:frog"]
        self.assertEqual(frog_row["signal_type"], "gap")
        self.assertEqual(frog_row["core_count"], 0)
        self.assertGreaterEqual(frog_row["signal_max_avg_percentile"], 70.0)

        # 3) `coffee` (strong in Tote Bags >= P70 despite tiny absolute sales 80..100,
        #    weak in core apparel < P40 despite 100..150 sales!) -> GAP SIGNAL
        self.assertIn("motif:coffee", by_key)
        coffee_row = by_key["motif:coffee"]
        self.assertEqual(coffee_row["signal_type"], "gap")
        self.assertEqual(coffee_row["core_count"], 2)
        self.assertLess(coffee_row["core_avg_percentile"], 40.0)
        tote_cell = next(c for c in coffee_row["signal_breakdown"] if c["category_key"] == "signal_tote_bags")
        self.assertTrue(tote_cell["is_strong"])
        self.assertGreaterEqual(tote_cell["avg_percentile"], 70.0)

        # 4) `cat` (strong in Mugs >= P70 AND strong in core apparel >= P50) -> VALIDATED SIGNAL
        self.assertIn("motif:cat", by_key)
        cat_row = by_key["motif:cat"]
        self.assertEqual(cat_row["signal_type"], "validated")
        self.assertGreaterEqual(cat_row["core_avg_percentile"], 50.0)
        self.assertGreaterEqual(cat_row["signal_max_avg_percentile"], 70.0)

        # 5) `dog` (weak in Mugs & Tote Bags < P70) -> neutral
        self.assertIn("motif:dog", by_key)
        self.assertEqual(by_key["motif:dog"]["signal_type"], "neutral")


class TestCrossCategorySignalsIsolationAndHttp(unittest.TestCase):
    """End-to-end isolation, server hard-blocking, and HTTP API tests on isolated temp DBs."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name)
        self.working_db = self.tmp_path / "working_sales.sqlite3"
        shutil.copy2(DEFAULT_DB_PATH, self.working_db)
        self.app_db = self.tmp_path / "preprocessing.sqlite3"
        self.ai_cfg_path = self.tmp_path / "ai_config.local"
        self.api_key_path = self.tmp_path / "api_key.local"

        self.app = WorkbenchApp(
            db_path=self.working_db,
            app_db_path=self.app_db,
            allow_live_collection=False,
            workbench_api_key_path=self.api_key_path,
            ai_config_path=self.ai_cfg_path,
        )
        self.preprocessing = self.app.preprocessing
        self.ai = self.app.ai
        self.scoring = self.app.scoring
        self.signals = self.app.signals
        self.collector = self.app.collector

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_category_registry_roles_and_unconfigured_signal_hard_block(self):
        # 6 core categories remain intact; 3 signal categories default to node_id=None
        self.assertEqual(len(CATEGORIES), 6)
        self.assertTrue(all(c.category_role == "core" for c in CATEGORIES))
        self.assertEqual(len(SIGNAL_CATEGORIES), 3)
        self.assertEqual(
            [c.key for c in SIGNAL_CATEGORIES],
            ["signal_mugs", "signal_tote_bags", "signal_phone_cases"],
        )
        self.assertTrue(all(c.category_role == "signal" and c.node_id is None for c in SIGNAL_CATEGORIES))

        # Collection config surfaces core (6) and signal (3) separately
        cfg = self.collector.get_config_and_summary()
        self.assertEqual(len(cfg["category_states"]), 6)
        self.assertEqual(len(cfg["signal_category_states"]), 3)
        self.assertEqual(cfg["signal_category_summary"]["total"], 3)
        self.assertEqual(cfg["signal_category_summary"]["pending_config"], 3)

        # Attempting to plan collection for unconfigured signal_mugs MUST be hard-blocked!
        with self.assertRaises(CollectionValidationError) as ctx:
            self.collector.create_plan(
                {
                    "categories": ["signal_mugs"],
                    "mode": "dry_run",
                    "observed_date": "2026-11-23",
                    "pages": 1,
                    "range": 100,
                    "budget_cap": 30,
                }
            )
        self.assertIn("尚未配置已核实的 Sorftime Node ID", str(ctx.exception))

    def test_signal_observations_strictly_isolated_from_stage02_and_stage04(self):
        # Baseline Stage 02 & Stage 04 BEFORE ingesting any signal observations
        dates = self.app.repo.get_available_dates()
        self.assertTrue(dates)
        obs_date = dates[0]

        before_prep = self.preprocessing.get_overview(observed_date=obs_date)
        before_score = self.scoring.evaluate(observed_date=obs_date)
        before_asins = {item["asin"] for item in before_score.get("items", [])}
        before_unranked = {item["asin"] for item in before_score.get("unranked", [])}

        # Ingest 5 fake mug signal observations into `signal_observations`
        signal_asins = [f"B0SIGMUG0{i}" for i in range(1, 6)]
        inserted = self.signals.ingest_signal_snapshot(
            observed_date=obs_date,
            signal_category_key="signal_mugs",
            node_id="TEST_MUG_NODE",
            products=[
                {
                    "asin": asin,
                    "title": f"Funny Frog Graphic Coffee Mug #{idx}",
                    "brand": "MugBrand",
                    "image_url": f"https://example.com/{asin}.jpg",
                    "listing_sales_30d": 1000 * idx,
                    "sales_price_usd": 14.99,
                    "rating_stars": 4.7,
                    "ratings_count": 200,
                    "source_category_bsr": idx * 10,
                }
                for idx, asin in enumerate(signal_asins, start=1)
            ],
        )
        self.assertEqual(inserted, 5)

        # Verify Stage 02 & Stage 04 are 100% IDENTICAL after signal ingestion
        after_prep = self.preprocessing.get_overview(observed_date=obs_date)
        after_score = self.scoring.evaluate(observed_date=obs_date)
        after_asins = {item["asin"] for item in after_score.get("items", [])}
        after_unranked = {item["asin"] for item in after_score.get("unranked", [])}

        self.assertEqual(before_prep["counts"], after_prep["counts"])
        self.assertEqual(before_score["summary"], after_score["summary"])
        self.assertEqual(before_asins, after_asins)
        self.assertEqual(before_unranked, after_unranked)

        for sig_asin in signal_asins:
            self.assertNotIn(sig_asin, after_asins)
            self.assertNotIn(sig_asin, after_unranked)

        # Also verify core `category_sales_observations` never received any signal ASIN
        conn = sqlite3.connect(self.working_db)
        try:
            placeholders = ",".join("?" for _ in signal_asins)
            cnt = conn.execute(
                f"SELECT COUNT(*) FROM category_sales_observations WHERE asin IN ({placeholders})",
                signal_asins,
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(cnt, 0)

    def test_http_endpoints_signal_block_dry_run_and_mocked_v2_tagging(self):
        llm_calls = []

        def mock_http(url, headers, payload, timeout):
            llm_calls.append({"url": url, "payload": payload})
            return 200, {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "is_graphic_print": True,
                                    "pattern_type": "graphic_print",
                                    "confidence": 0.96,
                                    "filter_reason": "正面清晰青蛙趣味印花图案",
                                    "print_subject": "sarcastic green frog",
                                    "slogan_text": "UNBOTHERED",
                                    "visual_style": "retro cartoon",
                                    "theme_tags": ["Funny Frog", "sarcastic humor"],
                                    "season_tag": "evergreen",
                                    "motif": "frog",
                                    "transferability": "pure_graphic",
                                }
                            ),
                        }
                    }
                ]
            }

        server, app = create_server(
            "127.0.0.1",
            0,
            self.working_db,
            app_db_path=self.app_db,
            allow_live_collection=False,
            workbench_api_key_path=self.api_key_path,
            ai_config_path=self.ai_cfg_path,
            ai_requester=mock_http,
        )
        app.ai.save_config(
            {
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-flash",
                "api_key": "sk-test-signals-only",
            }
        )

        obs_date = app.repo.get_available_dates()[0]
        app.signals.ingest_signal_snapshot(
            observed_date=obs_date,
            signal_category_key="signal_mugs",
            node_id="TEST_MUG_NODE",
            products=[
                {
                    "asin": "B0HTTPMUG1",
                    "title": "Funny Frog Graphic Mug 1",
                    "image_url": "https://example.com/mug1.jpg",
                    "listing_sales_30d": 5000,
                    "sales_price_usd": 15.99,
                },
                {
                    "asin": "B0HTTPMUG2",
                    "title": "Funny Frog Graphic Mug 2",
                    "image_url": "https://example.com/mug2.jpg",
                    "listing_sales_30d": 4800,
                    "sales_price_usd": 15.99,
                },
                {
                    "asin": "B0HTTPMUG3",
                    "title": "Low Sales Plain Mug 3",
                    "image_url": "https://example.com/mug3.jpg",
                    "listing_sales_30d": 50,
                    "sales_price_usd": 12.99,
                },
            ],
        )

        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{port}"

        def _req(method: str, path: str, payload=None):
            data = json.dumps(payload).encode("utf-8") if payload is not None else None
            headers = {"Content-Type": "application/json"} if data is not None else {}
            req = urllib.request.Request(f"{base}{path}", data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.status, json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read().decode("utf-8"))

        try:
            # 1) Unconfigured signal category via HTTP POST /api/collection/plan -> HTTP 400
            status, err_body = _req(
                "POST",
                "/api/collection/plan",
                {"categories": ["signal_mugs"], "mode": "dry_run", "observed_date": obs_date},
            )
            self.assertEqual(status, 400)
            self.assertIn("尚未配置已核实的 Sorftime Node ID", err_body.get("error", ""))

            # 2) POST /api/signals/ai-tag dry_run=True -> 0 external calls, reports 3 uncached
            status, preview = _req(
                "POST",
                "/api/signals/ai-tag",
                {"observed_date": obs_date, "dry_run": True},
            )
            self.assertEqual(status, 200)
            self.assertTrue(preview["dry_run"])
            self.assertEqual(preview["will_analyze_count"], 3)
            self.assertEqual(preview["new_uncached_count"], 3)
            self.assertEqual(preview["prompt_version"], VISION_PROMPT_VERSION)
            self.assertEqual(len(llm_calls), 0)

            # 3) POST /api/signals/ai-tag dry_run=False with mocked VLM -> writes ONLY to signal_ai_cache
            status, run_res = _req(
                "POST",
                "/api/signals/ai-tag",
                {"observed_date": obs_date, "dry_run": False},
            )
            self.assertEqual(status, 200)
            self.assertEqual(run_res["analyzed_count"], 3)
            self.assertEqual(len(llm_calls), 3)

            # Verify signal ASINs were written to `signal_ai_cache` and NEVER to `product_ai_cache`
            conn = sqlite3.connect(self.app_db)
            try:
                sig_cache_cnt = conn.execute("SELECT COUNT(*) FROM signal_ai_cache").fetchone()[0]
                prod_leak_cnt = conn.execute(
                    "SELECT COUNT(*) FROM product_ai_cache WHERE asin LIKE 'B0HTTPMUG%'"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(sig_cache_cnt, 3)
            self.assertEqual(prod_leak_cnt, 0)

            # 4) GET /api/signals/overview returns frog signal (check filter works)
            status, ov_all = _req("GET", f"/api/signals/overview?date={obs_date}")
            self.assertEqual(status, 200)
            self.assertEqual(ov_all["total_signal_observations"], 3)
            self.assertTrue(any(r["tag_key"] == "motif:frog" for r in ov_all["items"]))

            # Verify dry_run=True now sees 0 uncached (all 3 cached at v2!)
            status, preview2 = _req(
                "POST",
                "/api/signals/ai-tag",
                {"observed_date": obs_date, "dry_run": True},
            )
            self.assertEqual(status, 200)
            self.assertEqual(preview2["will_analyze_count"], 0)
            self.assertEqual(preview2["cached_count"], 3)
            self.assertEqual(len(llm_calls), 3)

            # 5) Dynamic Signal Node ID config: invalid/core-collision blocked -> valid unlocks plan & dry_run -> clear re-blocks
            st_bad, _ = _req("POST", "/api/signals/config", {"key": "signal_mugs", "node_id": "abc"})
            self.assertEqual(st_bad, 400)
            st_dup, _ = _req("POST", "/api/signals/config", {"key": "signal_mugs", "node_id": "1044544"})
            self.assertEqual(st_dup, 400)

            st_ok, saved = _req("POST", "/api/signals/config", {"key": "signal_mugs", "node_id": "9302388011"})
            self.assertEqual(st_ok, 200)
            self.assertEqual(saved["node_id"], "9302388011")

            st_cfg, cfg_after = _req("GET", "/api/collection/config")
            self.assertEqual(st_cfg, 200)
            self.assertEqual(cfg_after["signal_category_summary"]["configured"], 1)

            st_plan, sig_plan = _req(
                "POST",
                "/api/collection/plan",
                {"categories": ["9302388011"], "mode": "dry_run", "observed_date": "2026-11-24"},
            )
            self.assertEqual(st_plan, 200)
            self.assertEqual(sig_plan["items"][0]["category_role"], "signal")

            st_run, sig_run = _req(
                "POST",
                "/api/collection/run",
                {"confirmation_token": sig_plan["confirmation_token"], "confirmed": True},
            )
            self.assertEqual(st_run, 200)
            self.assertEqual(sig_run["overall_status"], "completed")
            self.assertEqual(sig_run["results"][0]["status"], "dry_run_ready")
            self.assertEqual(sig_run["results"][0]["category_role"], "signal")

            # Clear Node ID -> reverts to hard-blocked HTTP 400
            st_clr, _ = _req("POST", "/api/signals/config", {"key": "signal_mugs", "node_id": ""})
            self.assertEqual(st_clr, 200)
            st_blk2, _ = _req(
                "POST",
                "/api/collection/plan",
                {"categories": ["signal_mugs"], "mode": "dry_run", "observed_date": "2026-11-24"},
            )
            self.assertEqual(st_blk2, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
