"""Unit & HTTP API tests for Stage 02/03 Batch Preprocessing & Stage 04 Candidate Scoring V1."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from backend.config import DEFAULT_DB_PATH
from backend.preprocessing import PreprocessingService
from backend.repository import SnapshotRepository
from backend.scoring import (
    DEFAULT_PRICE_RANGES,
    DEFAULT_WEIGHTS,
    ScoringService,
    ScoringValidationError,
    compute_candidate_scores,
    compute_confidence,
)
from backend.server import create_server
from tests.fixtures import SCHEMA_SQL


def _build_synthetic_snapshot_db(db_path: Path) -> None:
    """Create a realistic synthetic Sorftime snapshot DB using exact Sorftime schema."""
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_SQL)

    tshirt_products = [
        {
            "Asin": "B0PLAIN001", "ParentAsin": "B0PAR00001",
            "Title": "Plain Solid Basic Crewneck T-Shirt", "Brand": "PlainBrand",
            "SalesPrice": 1499, "ListingSalesVolumeOfMonth": 8000,
            "Ratings": 4.5, "RatingsCount": 1200, "Rank": 10, "OnlineDate": "2023-01-10",
            "Photo": ["https://example.com/B0PLAIN001.jpg"],
            "BsrCategory": [["T-Shirts", "9056923011", "1"]],
        },
        {
            "Asin": "B0PLAIN002", "ParentAsin": "B0PAR00001",
            "Title": "Plain Solid Basic V-Neck T-Shirt", "Brand": "PlainBrand",
            "SalesPrice": 1599, "ListingSalesVolumeOfMonth": 6000,
            "Ratings": 4.4, "RatingsCount": 900, "Rank": 20, "OnlineDate": "2023-05-10",
            "Photo": ["https://example.com/B0PLAIN002.jpg"],
            "BsrCategory": [["T-Shirts", "9056923011", "2"]],
        },
        {
            "Asin": "B0PLAIN003", "ParentAsin": "B0PAR00002",
            "Title": "Solid Color Multipack Basic Tee", "Brand": "PlainBrand",
            "SalesPrice": 1999, "ListingSalesVolumeOfMonth": 4500,
            "Ratings": 4.6, "RatingsCount": 500, "Rank": 30, "OnlineDate": "2024-01-15",
            "Photo": ["https://example.com/B0PLAIN003.jpg"],
            "BsrCategory": [["T-Shirts", "9056923011", "3"]],
        },
        {
            "Asin": "B0GRPH0001", "ParentAsin": "B0PARGRPH1",
            "Title": "Funny Halloween Pumpkin Graphic Tee", "Brand": "PrintStudio",
            "SalesPrice": 1999, "ListingSalesVolumeOfMonth": 5000,
            "Ratings": 4.7, "RatingsCount": 600, "Rank": 50, "OnlineDate": "2024-06-01",
            "Photo": ["https://example.com/B0GRPH0001.jpg"],
            "BsrCategory": [["T-Shirts", "9056923011", "4"], ["Sweatshirts", "9056929011", "2"]],
        },
        {
            "Asin": "B0GRPH0002", "ParentAsin": "B0PARGRPH2",
            "Title": "Cute Retro Cat Graphic T-Shirt", "Brand": "FreshPrints",
            "SalesPrice": 2199, "ListingSalesVolumeOfMonth": 1800,
            "Ratings": 4.6, "RatingsCount": 15, "Rank": 120, "OnlineDate": "2026-10-20",
            "Photo": ["https://example.com/B0GRPH0002.jpg"],
            "BsrCategory": [["T-Shirts", "9056923011", "5"]],
        },
        {
            "Asin": "B0ZERO0001", "ParentAsin": "B0PARZERO1",
            "Title": "Vintage Floral Graphic Tee Zero Sales", "Brand": "FreshPrints",
            "SalesPrice": 1899, "ListingSalesVolumeOfMonth": 0,
            "Ratings": 4.5, "RatingsCount": 40, "Rank": 999, "OnlineDate": "2026-08-01",
            "Photo": ["https://example.com/B0ZERO0001.jpg"],
            "BsrCategory": [],
        },
    ]

    sweat_products = [
        # Cross-category overlap of B0GRPH0001
        tshirt_products[3],
        {
            "Asin": "B0SWET0001", "ParentAsin": "B0SWET0001",
            "Title": "Merry Christmas Embroidered Graphic Sweatshirt", "Brand": "HolidayCo",
            "SalesPrice": 3499, "ListingSalesVolumeOfMonth": 3200,
            "Ratings": 0.0, "RatingsCount": 0, "Rank": 80, "OnlineDate": "",
            "Photo": ["https://example.com/B0SWET0001.jpg"],
            "BsrCategory": [["Sweatshirts", "9056929011", "1"]],
        },
    ]

    for batch_id, node_id, prods in [(1, "9056923011", tshirt_products), (2, "9056929011", sweat_products)]:
        raw_resp = json.dumps({"Code": 0, "Message": "Success", "Data": {"Products": prods}})
        conn.execute(
            """INSERT INTO category_sales_batches
               (id, observed_date, fetched_at, domain, node_id, product_count, request_consumed, raw_response)
               VALUES (?, '2026-11-20', '2026-11-20T02:00:00Z', 1, ?, ?, 25, ?)""",
            (batch_id, node_id, len(prods), raw_resp),
        )
        for pos, p in enumerate(prods, start=1):
            conn.execute(
                """INSERT INTO category_sales_observations
                   (batch_id, result_position, asin, parent_asin, title, listing_sales_30d)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (batch_id, pos, p["Asin"], p["ParentAsin"], p["Title"], p["ListingSalesVolumeOfMonth"]),
            )
    conn.commit()
    conn.close()


class TestBatchReviewAndScoring(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        self.snap_db = self.tmp_dir / "snap.sqlite3"
        self.app_db = self.tmp_dir / "app.sqlite3"
        _build_synthetic_snapshot_db(self.snap_db)
        self.repo = SnapshotRepository(self.snap_db)
        self.prep = PreprocessingService(self.repo, app_db_path=self.app_db)
        self.scoring = ScoringService(self.prep, app_db_path=self.app_db)

    def tearDown(self):
        self._tmp.cleanup()

    # ------------------------------------------------------------------
    # 1. Stage 02/03 Batch Review Tests (asins[], brand, parent_asin)
    # ------------------------------------------------------------------
    def test_batch_review_by_asins_brand_and_parent_with_undo(self):
        self.prep.run_preprocessing("2026-11-20")

        # Verify suspected_non_print is sorted first, never auto-excluded
        q = self.prep.query_products(observed_date="2026-11-20", sort="needs_review_first", page_size=20)
        first_three = q["items"][:3]
        self.assertTrue(all(i["automatic_classification"] == "suspected_non_print" for i in first_three))
        self.assertEqual(self.prep.get_overview("2026-11-20")["counts"]["excluded"], 0)
        # Verify brand_count / parent_count attached
        plain1 = next(i for i in q["items"] if i["asin"] == "B0PLAIN001")
        self.assertEqual(plain1["brand_count"], 3)
        self.assertEqual(plain1["parent_count"], 2)

        # (a) Multi-ASIN batch confirm_exclude -> undo via batch restore
        res_asins = self.prep.submit_batch_review(
            {
                "action": "confirm_exclude",
                "asins": ["B0PLAIN001", "B0PLAIN002"],
                "reason_code": "solid_non_print",
                "reason_note": "多选纯色批量排除",
                "observed_date": "2026-11-20",
            }
        )
        self.assertEqual(res_asins["matched_count"], 2)
        self.assertEqual(res_asins["updated_count"], 2)
        self.assertEqual(res_asins["overview"]["counts"]["excluded"], 2)

        # Undo via batch restore
        res_undo = self.prep.submit_batch_review(
            {
                "action": "restore",
                "asins": ["B0PLAIN001", "B0PLAIN002"],
                "observed_date": "2026-11-20",
            }
        )
        self.assertEqual(res_undo["updated_count"], 2)
        self.assertEqual(res_undo["overview"]["counts"]["excluded"], 0)

        # (b) Same-Brand dry_run preview + actual batch exclude (3 PlainBrand items)
        preview_brand = self.prep.submit_batch_review(
            {
                "action": "confirmed_exclude",
                "brand": "plainbrand",
                "observed_date": "2026-11-20",
                "dry_run": True,
            }
        )
        self.assertTrue(preview_brand["dry_run"])
        self.assertEqual(preview_brand["matched_count"], 3)
        self.assertEqual(self.prep.get_overview("2026-11-20")["counts"]["excluded"], 0)

        res_brand = self.prep.submit_batch_review(
            {
                "action": "confirmed_exclude",
                "brand": "PlainBrand",
                "reason_code": "solid_non_print",
                "observed_date": "2026-11-20",
            }
        )
        self.assertEqual(res_brand["updated_count"], 3)
        self.assertEqual(res_brand["overview"]["counts"]["excluded"], 3)

        # (c) Same-Parent ASIN batch keep (2 items under B0PAR00001)
        res_parent = self.prep.submit_batch_review(
            {
                "action": "confirmed_keep",
                "parent_asin": "B0PAR00001",
                "reason_code": "confirmed_print_visual",
                "observed_date": "2026-11-20",
            }
        )
        self.assertEqual(res_parent["matched_count"], 2)
        self.assertEqual(res_parent["updated_count"], 2)
        # B0PLAIN001 & B0PLAIN002 became ready; B0PLAIN003 remains excluded
        self.assertEqual(res_parent["overview"]["counts"]["excluded"], 1)

        # Audit history recorded for individual items
        detail = self.prep.get_product_detail("B0PLAIN001", "2026-11-20")
        self.assertGreaterEqual(len(detail["review_history"]), 4)

    # ------------------------------------------------------------------
    # 2. Stage 04 Pure Scoring Algorithm Boundary Tests
    # ------------------------------------------------------------------
    def test_scoring_missing_value_renormalization_never_zero_fills(self):
        # Two products with identical sales & freshness; B0MISS is missing rating & price!
        # Under missing-value re-normalization, B0MISS MUST NOT be penalized with 0 for rating/price!
        candidates = [
            {
                "asin": "B0FULL0001",
                "parent_asin": "B0PARF0001",
                "title": "Full Data Tee",
                "brand": "A",
                "source_categories": [{"node_id": "9056923011", "category_key": "womens_novelty_tshirts", "garment_type": "t_shirt"}],
                "garment_types": ["t_shirt"],
                "listing_sales_30d": 1000,
                "price": 20.0,
                "price_valid": True,
                "rating_stars": 3.5,  # Maps to 0 after Bayesian? Wait v=1000 pushes close to 3.5 -> low rating!
                "ratings_count": 1000,
                "online_date": "2026-10-20",
            },
            {
                "asin": "B0MISS0001",
                "parent_asin": "B0PARM0001",
                "title": "Missing Rating & Price Tee",
                "brand": "B",
                "source_categories": [{"node_id": "9056923011", "category_key": "womens_novelty_tshirts", "garment_type": "t_shirt"}],
                "garment_types": ["t_shirt"],
                "listing_sales_30d": 1000,
                "price": 0.0,
                "price_valid": False,
                "rating_stars": None,
                "ratings_count": None,
                "online_date": "2026-10-20",
            },
        ]
        # Default filter_incomplete=True moves B0MISS0001 (missing 3 dims) to unranked_incomplete while preserving its re-normalized math!
        res_default = compute_candidate_scores(candidates, observed_date="2026-11-20")
        self.assertEqual([i["asin"] for i in res_default["items"]], ["B0FULL0001"])
        self.assertEqual([i["asin"] for i in res_default["unranked_incomplete"]], ["B0MISS0001"])
        self.assertEqual(res_default["unranked_incomplete"][0]["incomplete_tag"], "缺 3 维")

        res = compute_candidate_scores(candidates, observed_date="2026-11-20", filter_incomplete=False)
        by_asin = {i["asin"]: i for i in res["items"]}
        miss = by_asin["B0MISS0001"]

        # Rating, opportunity (ratings_count=None), price are missing -> only sales(35) + freshness(15) remain!
        self.assertEqual(set(miss["missing_dimensions"]), {"opportunity", "rating", "price"})
        self.assertTrue(miss["dimensions"]["rating"]["missing"])
        self.assertTrue(miss["dimensions"]["price"]["missing"])
        self.assertTrue(miss["dimensions"]["opportunity"]["missing"])
        # V2: Missing dimensions use conservative prior (45.0) at full weight (never free-ride via denominator exclusion!)
        self.assertAlmostEqual(miss["dimensions"]["sales"]["effective_weight"], 35.0, places=1)
        self.assertAlmostEqual(miss["dimensions"]["freshness"]["effective_weight"], 15.0, places=1)
        expected_total = (
            miss["dimensions"]["sales"]["score"] * 0.35
            + miss["dimensions"]["freshness"]["score"] * 0.15
            + 45.0 * 0.50
        )
        self.assertAlmostEqual(miss["total_score"], expected_total, places=1)
        # Confidence reflects missing rating (-15) and invalid price (-15) -> 70 (medium)
        self.assertEqual(miss["confidence_score"], 70)
        self.assertEqual(miss["confidence_level"], "medium")

    def test_scoring_cross_category_dedup_zero_sales_and_bayesian_rating(self):
        candidates = [
            # Appears TWICE (two categories) -> must deduplicate to 1 record, never sum sales (1000, NOT 2000)!
            {
                "asin": "B0CROSS001",
                "parent_asin": "B0PARC0001",
                "title": "Cross Category Tee",
                "source_categories": [{"node_id": "1044544", "category_key": "womens_tshirts", "garment_type": "t_shirt"}],
                "garment_types": ["t_shirt"],
                "listing_sales_30d": 1000,
                "price": 20.0,
                "price_valid": True,
                "rating_stars": 4.7,
                "ratings_count": 500,
                "online_date": "2026-06-01",
            },
            {
                "asin": "B0CROSS001",
                "parent_asin": "B0PARC0001",
                "title": "Cross Category Tee",
                "source_categories": [{"node_id": "9056923011", "category_key": "womens_novelty_tshirts", "garment_type": "t_shirt"}],
                "garment_types": ["t_shirt"],
                "listing_sales_30d": 1000,
                "price": 20.0,
                "price_valid": True,
                "rating_stars": 4.7,
                "ratings_count": 500,
                "online_date": "2026-06-01",
            },
            # High-volume T-Shirt in 1044544 so B0CROSS001 is low pct in 1044544 but 100% pct in 9056923011!
            {
                "asin": "B0HIGH0001",
                "parent_asin": "B0PARH0001",
                "title": "Mega T-Shirt",
                "source_categories": [{"node_id": "1044544", "category_key": "womens_tshirts", "garment_type": "t_shirt"}],
                "garment_types": ["t_shirt"],
                "listing_sales_30d": 50000,
                "price": 15.0,
                "price_valid": True,
                "rating_stars": 4.2,
                "ratings_count": 200,
                "online_date": "2024-01-01",
            },
            # Median anchor item (4.3★) so pool median C = 4.3, making Bayesian smoothing crystal clear
            {
                "asin": "B0MEDI0001",
                "parent_asin": "B0PARM0002",
                "title": "Baseline Sweatshirt",
                "source_categories": [{"node_id": "9056929011", "category_key": "womens_novelty_sweatshirts", "garment_type": "sweatshirt"}],
                "garment_types": ["sweatshirt"],
                "listing_sales_30d": 800,
                "price": 67.5,  # 50% above $45 sweatshirt max -> 0.0 price score!
                "price_valid": True,
                "rating_stars": 4.2,
                "ratings_count": 100,
                "online_date": "2025-01-01",
            },
            # Small-sample 5.0-star (1 review) -> Bayesian m=30 pulls it down below 4.7* (500 reviews)!
            {
                "asin": "B0TINY0001",
                "parent_asin": "B0TINY0001",  # same parent -> -15 confidence; <10 reviews -> -10 confidence
                "title": "One Review 5-Star Tee",
                "source_categories": [{"node_id": "9056923011", "category_key": "womens_novelty_tshirts", "garment_type": "t_shirt"}],
                "garment_types": ["t_shirt"],
                "listing_sales_30d": 25,  # Freshness sales gate = 25/50 = 0.5!
                "price": 11.25,  # 25% below $15 min -> exactly 50.0 price score!
                "price_valid": True,
                "rating_stars": 5.0,
                "ratings_count": 1,
                "online_date": "2026-11-20",  # d=0 -> base freshness 100 * 0.5 gate = 50.0!
            },
            # Zero sales item -> MUST be isolated in unranked_no_sales!
            {
                "asin": "B0ZERO9999",
                "parent_asin": "B0PARZ9999",
                "title": "Zero Sales Tee",
                "source_categories": [{"node_id": "9056923011", "category_key": "womens_novelty_tshirts", "garment_type": "t_shirt"}],
                "garment_types": ["t_shirt"],
                "listing_sales_30d": 0,
                "price": 20.0,
                "price_valid": True,
                "rating_stars": 4.9,
                "ratings_count": 100,
                "online_date": "2026-11-19",
            },
        ]
        res = compute_candidate_scores(candidates, observed_date="2026-11-20")

        # Zero-sales excluded from ranked pool
        self.assertEqual(len(res["items"]), 4)
        self.assertEqual(len(res["unranked_no_sales"]), 1)
        self.assertEqual(res["unranked_no_sales"][0]["asin"], "B0ZERO9999")

        by_asin = {i["asin"]: i for i in res["items"]}

        # V2 asymmetric POD price curve: Sweatshirt 50% above $45 ($67.5) gently decays to 70.0 (protects high-margin POD)
        self.assertAlmostEqual(by_asin["B0MEDI0001"]["dimensions"]["price"]["score"], 70.0, places=2)

        # Cross-category dedup: 1 record, 2 source categories, sales NOT doubled
        cross = by_asin["B0CROSS001"]
        self.assertEqual(cross["listing_sales_30d"], 1000)
        self.assertEqual(len(cross["source_categories"]), 2)
        # In 9056923011 (1000 vs 25), B0CROSS001 is 100th percentile!
        self.assertIn("类目分位 100.0", cross["dimensions"]["sales"]["detail"])

        # Bayesian smoothing: 4.7★ (500 reviews) > 5.0★ (1 review)
        tiny = by_asin["B0TINY0001"]
        self.assertGreater(cross["bayesian_rating"], tiny["bayesian_rating"])
        self.assertGreater(cross["dimensions"]["rating"]["score"], tiny["dimensions"]["rating"]["score"])

        # Freshness sales gate: d=0 (base 100) * (25/50) = 50.0
        self.assertAlmostEqual(tiny["dimensions"]["freshness"]["score"], 50.0, places=2)
        # Price boundary linear deduction: $11.25 is 25% below $15 -> 50.0
        # V2 asymmetric POD price floor: 25% below $15 ($11.25) decays steeper (dev/0.35 -> 28.57)
        self.assertAlmostEqual(tiny["dimensions"]["price"]["score"], 28.57, places=2)
        # Confidence: 100 - 15 (parent==asin) - 10 (ratings_count<10) = 75 (medium)
        self.assertEqual(tiny["confidence_score"], 75)
        self.assertEqual(tiny["confidence_level"], "medium")

    def test_scoring_presets_persistence_and_http_api(self):
        # Start server on ephemeral port with synthetic DB
        server, _ = create_server(
            host="127.0.0.1",
            port=0,
            db_path=self.snap_db,
            app_db_path=self.app_db,
        )
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        base = f"http://127.0.0.1:{port}"
        try:
            # 1. Batch review HTTP endpoint (/api/preprocess/batch-review)
            req_batch = Request(
                f"{base}/api/preprocess/batch-review",
                data=json.dumps({"action": "confirm_exclude", "brand": "PlainBrand", "observed_date": "2026-11-20"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req_batch, timeout=5) as r:
                bdata = json.loads(r.read())
            self.assertEqual(bdata["updated_count"], 3)

            # 2. Evaluate scoring HTTP endpoint -> only ready non-excluded items (B0GRPH0001, B0GRPH0002, B0SWET0001) + B0ZERO0001 unranked
            req_eval = Request(
                f"{base}/api/scoring/evaluate",
                data=json.dumps({"observed_date": "2026-11-20"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req_eval, timeout=5) as r:
                edata = json.loads(r.read())
            self.assertTrue(edata["ready"])
            self.assertEqual(edata["summary"]["scored_count"], 3)
            self.assertEqual(edata["summary"]["unranked_no_sales_count"], 1)
            self.assertEqual(edata["unranked_no_sales"][0]["asin"], "B0ZERO0001")

            # 3. Save custom preset -> list -> delete
            req_save = Request(
                f"{base}/api/scoring/presets",
                data=json.dumps(
                    {
                        "name": "新品高动销测试方案",
                        "weights": {"sales": 10, "opportunity": 45, "rating": 10, "freshness": 30, "price": 5},
                        "price_ranges": {"t_shirt": {"min": 14, "max": 28}, "sweatshirt": {"min": 25, "max": 48}},
                    }
                ).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req_save, timeout=5) as r:
                sdata = json.loads(r.read())
            pid = sdata["preset"]["preset_id"]
            self.assertTrue(any(p["preset_id"] == pid for p in sdata["presets"]))
            self.assertTrue(any(p["preset_id"] == "system_default" for p in sdata["presets"]))

            # System default cannot be deleted
            with self.assertRaises(HTTPError) as ctx:
                urlopen(Request(f"{base}/api/scoring/presets/system_default", method="DELETE"), timeout=5)
            self.assertEqual(ctx.exception.code, 400)

            # Delete custom preset succeeds
            with urlopen(Request(f"{base}/api/scoring/presets/{pid}", method="DELETE"), timeout=5) as r:
                ddata = json.loads(r.read())
            self.assertFalse(any(p["preset_id"] == pid for p in ddata["presets"]))
        finally:
            server.shutdown()
            server.server_close()

    def test_real_snapshot_weight_adjustment_changes_top10_and_keeps_db_immutable(self):
        if not DEFAULT_DB_PATH.exists():
            self.skipTest("Original Sorftime SQLite snapshot not present")
        hash_before = hashlib.sha256(DEFAULT_DB_PATH.read_bytes()).hexdigest()
        real_repo = SnapshotRepository(DEFAULT_DB_PATH)
        real_prep = PreprocessingService(real_repo, app_db_path=self.tmp_dir / "real_app.sqlite3")
        real_scoring = ScoringService(real_prep, app_db_path=self.tmp_dir / "real_app.sqlite3")

        res_default = real_scoring.evaluate()
        self.assertTrue(res_default["ready"])
        self.assertGreater(len(res_default["items"]), 20)
        top10_default = [i["asin"] for i in res_default["items"][:10]]

        # Shift weights heavily toward freshness & opportunity
        res_fresh = real_scoring.evaluate(
            weights={"sales": 5, "opportunity": 45, "rating": 5, "freshness": 40, "price": 5}
        )
        top10_fresh = [i["asin"] for i in res_fresh["items"][:10]]
        self.assertNotEqual(top10_default, top10_fresh, "Weight shift should reorder Top 10 on real data")

        # Verify rank_delta_vs_default arithmetic invariant
        for item in res_fresh["items"][:20]:
            self.assertEqual(item["rank_delta_vs_default"], item["default_rank"] - item["rank"])

        hash_after = hashlib.sha256(DEFAULT_DB_PATH.read_bytes()).hexdigest()
        self.assertEqual(hash_before, hash_after)

    def test_scoring_tier2_same_image_and_brand_title_folding_and_basic_triage(self):
        from backend.preprocessing import classify_product_text, derive_eligibility

        # 1. Three orphan ASINs with same brand + same Amazon image asset ID (or exact title) fold to 1!
        dups = [
            {
                "asin": "B0DUP00001", "parent_asin": "B0DUP00001", "brand": "Shiketong",
                "title": "Womens Christmas Sweatshirts Merry Graphic Mock Neck Pullover",
                "image_url": "https://m.media-amazon.com/images/I/71YiI5x7TmL.jpg",
                "source_categories": [{"node_id": "9056929011", "garment_type": "sweatshirt"}],
                "garment_types": ["sweatshirt"], "listing_sales_30d": 10000, "price": 23.99, "price_valid": True,
                "rating_stars": 4.5, "ratings_count": 50, "online_date": "2026-11-01",
            },
            {
                "asin": "B0DUP00002", "parent_asin": "B0DUP00002", "brand": "Shiketong",
                "title": "Womens Christmas Sweatshirts Merry Graphic Mock Neck Pullover",
                "image_url": "https://m.media-amazon.com/images/I/71YiI5x7TmL.jpg",
                "source_categories": [{"node_id": "9056993011", "garment_type": "sweatshirt"}],
                "garment_types": ["sweatshirt"], "listing_sales_30d": 5600, "price": 0.0, "price_valid": False,
                "rating_stars": None, "ratings_count": 0, "online_date": "2026-11-15",
            },
            {
                "asin": "B0DUP00003", "parent_asin": "B0DUP00003", "brand": "Shiketong",
                "title": "Womens Christmas Sweatshirts Merry Graphic Mock Neck Pullover",
                "image_url": "https://m.media-amazon.com/images/I/71YiI5x7TmL.jpg",
                "source_categories": [{"node_id": "9056929011", "garment_type": "sweatshirt"}],
                "garment_types": ["sweatshirt"], "listing_sales_30d": 5500, "price": 0.0, "price_valid": False,
                "rating_stars": None, "ratings_count": 0, "online_date": "2026-11-15",
            },
        ]
        res = compute_candidate_scores(dups, observed_date="2026-11-20")
        self.assertEqual(len(res["items"]), 1)
        rep = res["items"][0]
        self.assertEqual(rep["asin"], "B0DUP00001")
        self.assertEqual(rep["listing_sales_30d"], 10000)  # Never summed!
        self.assertEqual(rep["collapsed_count"], 2)
        self.assertEqual(len(rep["source_categories"]), 2)

        # 2. Plain solid fashion/workout tops & general-category uncertain items route to needs_review (never auto-excluded!)
        for plain_title in (
            "ZESICA Womens Asymmetrical Long Sleeve Fall Shirt Going Out Tops",
            "YEOREO Ease Women Fitted Workout Tops",
            "Amazon Essentials Women's Regular-Fit Long-Sleeve Crewneck T-Shirt",
        ):
            cls, _ = classify_product_text(plain_title)
            self.assertEqual(cls, "suspected_non_print", plain_title)
            elig, _ = derive_eligibility(cls, [], "unreviewed", False)
            self.assertEqual(elig, "needs_review")

        # General T-Shirt category (1044544) uncertain item -> needs_review, NEVER excluded
        cls_u, _ = classify_product_text("Zeagoo Womens V-Neck Tops Summer Casual Short Sleeve Tshirts")
        self.assertEqual(cls_u, "uncertain")
        elig_gen, _ = derive_eligibility(
            cls_u, [], "unreviewed", False, source_categories=[{"node_id": "1044544"}]
        )
        self.assertEqual(elig_gen, "needs_review")


if __name__ == "__main__":
    unittest.main()
