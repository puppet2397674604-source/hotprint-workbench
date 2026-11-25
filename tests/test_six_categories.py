"""Comprehensive tests for the Six-Category Expansion across Collection, Dashboard, Preprocessing, and Contracts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from backend.analytics import build_market_overview, query_products
from backend.collector import CollectionService, CollectionValidationError
from backend.config import (
    CATEGORIES,
    CATEGORY_BY_KEY,
    CATEGORY_BY_NODE,
    CONFIGURED_CATEGORIES,
    DEFAULT_DB_PATH,
    PENDING_CATEGORIES,
    CategorySpec,
    resolve_category,
    serialize_categories,
)
from backend.preprocessing import PreprocessingService, classify_product_text
from backend.repository import SnapshotRepository
from backend.server import create_server
from tests.fixtures import SCHEMA_SQL, create_sample_db


def create_multi_category_db(db_path: Path) -> Path:
    """Create a synthetic 4-category DB (2 sweatshirts + 2 synthetic t-shirt nodes) matching real Sorftime schema."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.execute("DROP VIEW IF EXISTS category_sales_by_category")
        conn.execute(
            """
            CREATE VIEW category_sales_by_category AS
            WITH observations AS (
                SELECT o.*, b.observed_date, b.domain, b.node_id,
                       json_extract(COALESCE(json_extract(b.raw_response, '$.Data.Products'),
                                             json_extract(b.raw_response, '$.Data.products')),
                                    '$[' || (o.result_position - 1) || ']') AS product
                FROM category_sales_observations AS o
                JOIN category_sales_batches AS b ON b.id = o.batch_id
            )
            SELECT
                o.batch_id,
                o.observed_date,
                o.domain,
                o.node_id,
                CASE o.node_id
                    WHEN '9056929011' THEN '女式趣味卫衣'
                    WHEN '9056993011' THEN '男式趣味卫衣'
                    WHEN '1044544' THEN '女式 T 恤'
                    WHEN '9056987011' THEN '男式趣味 T 恤'
                    ELSE o.node_id
                END AS source_category,
                o.result_position,
                o.asin,
                o.parent_asin,
                CASE
                    WHEN o.asin = o.parent_asin THEN '父子编号相同，需核对'
                    WHEN o.parent_asin IS NULL THEN '未返回父 ASIN'
                    ELSE '父子编号不同'
                END AS asin_status,
                CASE WHEN o.domain = 1 AND o.asin IS NOT o.parent_asin
                    THEN 'https://www.amazon.com/dp/' || o.asin
                END AS amazon_url,
                o.title,
                o.listing_sales_30d,
                NULLIF(json_extract(o.product, '$.Ratings'), 0) AS rating_stars,
                json_extract(o.product, '$.RatingsCount') AS ratings_count,
                CASE WHEN o.domain = 1 THEN json_extract(o.product, '$.SalesPrice') / 100.0 END AS sales_price_usd,
                CASE WHEN o.domain = 1 THEN NULLIF(json_extract(o.product, '$.ListingSalesOfMonth'), -1) / 100.0 END
                    AS listing_revenue_30d_usd,
                json_extract(o.product, '$.Brand') AS brand,
                json_extract(o.product, '$.Photo[0]') AS main_image_url,
                json_extract(o.product, '$.OnlineDate') AS online_date,
                json_extract(o.product, '$.Rank') AS main_category_bsr,
                (
                    SELECT CAST(json_extract(rank.value, '$[2]') AS INTEGER)
                    FROM json_each(o.product, '$.BsrCategory') AS rank
                    WHERE CAST(json_extract(rank.value, '$[1]') AS TEXT) = o.node_id
                    LIMIT 1
                ) AS source_category_bsr
            FROM observations AS o
            """
        )

        # Shared ASIN appearing across 3 categories (must deduplicate to 1, never sum sales!)
        shared_asin = {
            "Asin": "B0MULTI301",
            "ParentAsin": "B0PMULTI01",
            "Title": "Funny Retro Cat Graphic T-Shirt Unisex Vintage Tee",
            "ListingSalesVolumeOfMonth": 2500,
            "Ratings": 4.6,
            "RatingsCount": 420,
            "SalesPrice": 2199,
            "ListingSalesOfMonth": 5497500,
            "Brand": "PrintCraft",
            "Photo": ["https://m.media-amazon.com/images/I/multi301.jpg"],
            "OnlineDate": "2025-07-01",
            "Rank": 1200,
            "BsrCategory": [
                ["Women's Novelty Sweatshirts", "9056929011", "4"],
                ["Men's Novelty Sweatshirts", "9056993011", "9"],
                ["Women's T-Shirts", "1044544", "2"],
            ],
        }
        tshirt_plain = {
            "Asin": "B0TSHIRT01",
            "ParentAsin": "B0PTSHRT01",
            "Title": "Hanes Men 6-Pack Crewneck Plain Solid Cotton T-Shirt",
            "ListingSalesVolumeOfMonth": 4100,
            "Ratings": 4.5,
            "RatingsCount": 980,
            "SalesPrice": 1899,
            "ListingSalesOfMonth": 7785900,
            "Brand": "Hanes",
            "Photo": ["https://m.media-amazon.com/images/I/plain01.jpg"],
            "OnlineDate": "2024-03-10",
            "Rank": 600,
            "BsrCategory": [["Women's T-Shirts", "1044544", "1"]],
        }
        tshirt_graphic = {
            "Asin": "B0TSHIRT02",
            "ParentAsin": "B0PTSHRT02",
            "Title": "Men Funny Sarcastic Coffee Lover Graphic Tee Short Sleeve",
            "ListingSalesVolumeOfMonth": 1800,
            "Ratings": 4.7,
            "RatingsCount": 210,
            "SalesPrice": 1999,
            "ListingSalesOfMonth": 3598200,
            "Brand": "TeeHumor",
            "Photo": ["https://m.media-amazon.com/images/I/coffee02.jpg"],
            "OnlineDate": "2025-06-15",
            "Rank": 3100,
            "BsrCategory": [["Men's Novelty T-Shirts", "9056987011", "7"]],
        }
        tshirt_conflicting = {
            "Asin": "B0TSHIRT03",
            "ParentAsin": "B0PTSHRT03",
            "Title": "Women Funny Cat Print Striped Short Sleeve T-Shirt",
            "ListingSalesVolumeOfMonth": 950,
            "Ratings": 4.3,
            "RatingsCount": 88,
            "SalesPrice": 1799,
            "ListingSalesOfMonth": 1709050,
            "Brand": "StripeCat",
            "Photo": ["https://m.media-amazon.com/images/I/stripecat03.jpg"],
            "OnlineDate": "2025-08-01",
            "Rank": 5200,
            "BsrCategory": [["Women's T-Shirts", "1044544", "11"]],
        }

        batches = [
            ("2026-11-19", "2026-11-19T02:16:03+00:00", 1, "9056929011", [shared_asin], 5, 1343, 28),
            ("2026-11-19", "2026-11-19T02:16:06+00:00", 1, "9056993011", [shared_asin], 5, 1338, 30),
            ("2026-11-19", "2026-11-19T02:16:09+00:00", 1, "1044544", [shared_asin, tshirt_plain, tshirt_conflicting], 5, 1333, 25),
            ("2026-11-19", "2026-11-19T02:16:12+00:00", 1, "9056987011", [tshirt_graphic], 5, 1328, 22),
        ]

        for obs_date, fetched_at, domain, node_id, products, consumed, left, page_count in batches:
            raw = json.dumps(
                {
                    "Code": 0,
                    "Message": "Success",
                    "RequestConsumed": consumed,
                    "RequestLeft": left,
                    "Data": {"Page": 1, "PageCount": page_count, "Products": products},
                }
            )
            cur = conn.execute(
                """
                INSERT INTO category_sales_batches
                (observed_date, fetched_at, domain, node_id, product_count, request_consumed, raw_response)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (obs_date, fetched_at, domain, node_id, len(products), consumed, raw),
            )
            batch_id = cur.lastrowid
            for pos, item in enumerate(products, start=1):
                conn.execute(
                    """
                    INSERT INTO category_sales_observations (
                        batch_id, result_position, asin, parent_asin, title, listing_sales_30d
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_id,
                        pos,
                        item["Asin"],
                        item["ParentAsin"],
                        item["Title"],
                        item["ListingSalesVolumeOfMonth"],
                    ),
                )
        conn.commit()
    finally:
        conn.close()
    return db_path


class TestSixCategoryExpansion(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sample_db = create_sample_db(Path(self._tmp.name) / "sample.sqlite3")
        self.multi_db = create_multi_category_db(Path(self._tmp.name) / "multi.sqlite3")
        self.app_db = Path(self._tmp.name) / "app_prep.sqlite3"

    def tearDown(self):
        self._tmp.cleanup()

    def test_1_six_category_registry_and_metadata(self):
        self.assertEqual(len(CATEGORIES), 6)
        self.assertEqual(len(CONFIGURED_CATEGORIES), 6)
        self.assertEqual(len(PENDING_CATEGORIES), 0)
        self.assertEqual(
            set(CATEGORY_BY_NODE.keys()),
            {"1044544", "1045624", "9056923011", "9056987011", "9056929011", "9056993011"},
        )

        en_names = [c.en_name for c in CATEGORIES]
        self.assertEqual(
            en_names,
            [
                "Women's T-Shirts",
                "Men's T-Shirts",
                "Women's Novelty T-Shirts",
                "Men's Novelty T-Shirts",
                "Women's Novelty Sweatshirts",
                "Men's Novelty Sweatshirts",
            ],
        )

        # Verify each category has verified node_id, garment_type, gender, and status
        for spec in CATEGORIES:
            self.assertTrue(spec.is_configured)
            self.assertEqual(spec.node_status, "configured")
            self.assertIn(spec.garment_type, ("t_shirt", "sweatshirt"))
            self.assertIn(spec.gender, ("women", "men"))

        # Verify serialized structure contains garment_type, gender, status
        serialized = serialize_categories()
        self.assertEqual(len(serialized), 6)
        for item in serialized:
            for field in ("key", "node_id", "name", "en_name", "garment_type", "gender", "is_configured", "node_status"):
                self.assertIn(field, item)

        # Verify legacy aliases and canonical keys resolve seamlessly
        self.assertEqual(resolve_category("women").node_id, "9056929011")
        self.assertEqual(resolve_category("men").node_id, "9056993011")
        self.assertEqual(resolve_category("womens_tshirts").node_id, "1044544")
        self.assertEqual(resolve_category("mens_tshirts").node_id, "1045624")
        self.assertEqual(resolve_category("womens_novelty_tshirts").node_id, "9056923011")
        self.assertEqual(resolve_category("mens_novelty_tshirts").node_id, "9056987011")

    def test_2_unconfigured_node_ids_hard_blocked_from_collection(self):
        repo = SnapshotRepository(self.sample_db)
        svc = CollectionService(repo, allow_live_collection=False)

        # Default categories (None) picks all 6 verified configured categories (6 * 5 = 30 <= default budget_cap 30)
        plan_default = svc.create_plan({"observed_date": "2026-11-20", "mode": "dry_run"})
        self.assertEqual(
            plan_default["request"]["categories"],
            ["1044544", "1045624", "9056923011", "9056987011", "9056929011", "9056993011"],
        )
        self.assertEqual(plan_default["estimated_total_credits"], 30)

        # Attempting to guess an unverified numeric Node ID must fail
        with self.assertRaises(CollectionValidationError):
            svc.create_plan({"categories": ["1234567890"], "observed_date": "2026-11-20", "mode": "dry_run"})

        # Any category whose node_id is None ("待配置") must be hard-blocked with CollectionValidationError
        pending_spec = CategorySpec(
            key="future_hoodies",
            node_id=None,
            name="未来卫衣测试",
            short_name="测试",
            en_name="Future Hoodies",
            garment_type="hoodie",
            gender="women",
            segment="novelty",
            color="#64748B",
            soft_color="#F1F5F9",
        )
        with patch("backend.collector.resolve_category", return_value=pending_spec):
            with self.assertRaises(CollectionValidationError) as ctx:
                svc.create_plan({"categories": ["future_hoodies"], "observed_date": "2026-11-20", "mode": "dry_run"})
            self.assertIn("待配置", str(ctx.exception))

    def test_3_category_filtering_and_bsr_isolation(self):
        repo = SnapshotRepository(self.sample_db)

        # All view: BSR sort disabled & safely falls back
        all_scope = repo.get_scope_data(category="all")
        all_ov = build_market_overview(all_scope)
        self.assertFalse(all_ov["can_sort_source_bsr"])
        self.assertEqual(len(all_ov["categories"]), 6)
        self.assertEqual(len(all_ov["category_comparison"]["active_categories"]), 2)
        self.assertEqual(len(all_ov["category_comparison"]["pending_categories"]), 0)

        all_bsr_query = query_products(all_scope, sort="source_category_bsr", order="asc")
        self.assertEqual(all_bsr_query["sort"], "listing_sales_30d")
        self.assertIsNotNone(all_bsr_query["sort_warning"])

        # Single verified category view: BSR sort enabled & accurate
        w_scope = repo.get_scope_data(category="9056929011")
        w_ov = build_market_overview(w_scope)
        self.assertTrue(w_ov["can_sort_source_bsr"])
        w_bsr_query = query_products(w_scope, sort="source_category_bsr", order="asc")
        self.assertEqual(w_bsr_query["sort"], "source_category_bsr")
        self.assertIsNone(w_bsr_query["sort_warning"])

        # Configured but not-yet-collected category (1044544) returns empty gracefully
        t_scope = repo.get_scope_data(category="1044544")
        self.assertFalse(t_scope["category_pending_config"])
        self.assertEqual(len(t_scope["products"]), 0)
        t_ov = build_market_overview(t_scope)
        self.assertTrue(t_ov["ready"])
        self.assertEqual(t_ov["kpis"]["unique_asin_count"], 0)

    def test_4_multi_category_cross_asin_dedup_and_provenance(self):
        repo = SnapshotRepository(self.multi_db)
        scope = repo.get_scope_data(category="all", dedup_mode="asin")
        # 6 observations across 4 categories -> 4 unique ASINs (B0MULTI301 appears in 3 categories)
        self.assertEqual(len(scope["observations"]), 6)
        self.assertEqual(len(scope["products"]), 4)

        multi_item = next(p for p in scope["products"] if p["asin"] == "B0MULTI301")
        # Sales must NOT be summed (2500, NOT 7500)
        self.assertEqual(multi_item["listing_sales_30d"], 2500)
        self.assertTrue(multi_item["is_cross_category"])
        self.assertEqual(multi_item["cross_category_count"], 3)
        self.assertEqual(
            {s["node_id"]: s["source_category_bsr"] for s in multi_item["source_categories"]},
            {"9056929011": 4, "9056993011": 9, "1044544": 2},
        )
        self.assertEqual(multi_item["garment_types"], ["sweatshirt", "t_shirt"])

        # Market overview handles 4 active categories cleanly
        ov = build_market_overview(scope)
        self.assertEqual(ov["category_comparison"]["overlap"]["active_category_count"], 4)
        self.assertEqual(ov["category_comparison"]["overlap"]["cross_category_asin_count"], 1)
        self.assertEqual(ov["category_comparison"]["overlap"]["total_unique"], 4)

    def test_5_preprocessing_supports_tshirt_and_sweatshirt_with_plan_a(self):
        # Unit check on T-Shirt and Sweatshirt text rules (Plan A: never auto-exclude)
        cases = [
            ("Gildan Men Heavy Cotton Graphic T-Shirt Funny Sarcastic Tee", "possible_print"),
            ("Women Vintage Retro Coffee Lover Graphic Tee Short Sleeve", "possible_print"),
            ("Hanes Men 6-Pack Crewneck Plain Solid Cotton T-Shirt", "suspected_non_print"),
            ("Women Solid Color Basic Crewneck T-Shirt Casual Summer Top", "suspected_non_print"),
            ("Custom T-Shirt with Your Image Design Your Own Personalized Tee", "suspected_non_print"),
            ("Women Funny Cat Print Striped Short Sleeve T-Shirt", "conflicting"),
            ("Carhartt Men K87 Pocket Short Sleeve T-Shirt Black", "uncertain"),
            ("Women's Merry Christmas Graphic Sweatshirt", "possible_print"),
            ("Arach&Cloz Women Basic Tops Sweater", "suspected_non_print"),
        ]
        for title, expected in cases:
            cls, reasons = classify_product_text(title)
            self.assertEqual(cls, expected, f"Mismatch for {title}: got {cls} ({reasons})")

        # End-to-end preprocessing on multi-category T-shirt + Sweatshirt DB
        repo = SnapshotRepository(self.multi_db)
        svc = PreprocessingService(repo, app_db_path=self.app_db)
        ov = svc.get_overview()
        self.assertEqual(ov["counts"]["total"], 4)
        self.assertEqual(ov["counts"]["excluded"], 0)  # Plan A: NEVER auto-excluded!
        self.assertEqual(ov["counts"]["needs_review"], 2)  # B0TSHIRT01 (6-pack plain) + B0TSHIRT03 (conflicting)
        self.assertEqual(ov["counts"]["ready"], 2)  # B0MULTI301 + B0TSHIRT02

        # Human confirms exclude B0TSHIRT01 -> persisted & kept across re-run
        svc.submit_manual_review(
            {
                "asin": "B0TSHIRT01",
                "action": "confirm_exclude",
                "reason_code": "solid_non_print",
                "reason_note": "纯色6件装素T，人工确认排除",
                "request_id": "req_tshirt_ex_1",
            }
        )
        ov2 = svc.get_overview()
        self.assertEqual(ov2["counts"]["excluded"], 1)
        self.assertEqual(ov2["counts"]["needs_review"], 1)

        # Re-run preprocessing -> manual exclusion must stay preserved!
        svc.run_preprocessing()
        ov3 = svc.get_overview()
        self.assertEqual(ov3["counts"]["excluded"], 1)
        cand = svc.get_scoring_candidates(eligibility="ready")
        self.assertEqual(cand["total"], 2)
        self.assertNotIn("B0TSHIRT01", [i["asin"] for i in cand["items"]])
        self.assertFalse(cand["cross_category_quota_rule_defined"])
        self.assertFalse(cand["scoring_formula_implemented"])
        self.assertEqual(len(cand["categories"]), 6)

    def test_6_http_api_blocks_pending_collection_and_serves_multicategory_contracts(self):
        srv, _ = create_server(host="127.0.0.1", port=0, db_path=self.sample_db, app_db_path=self.app_db)
        port = srv.server_address[1]
        base = f"http://127.0.0.1:{port}"
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            # /api/health returns 6 categories
            with urlopen(f"{base}/api/health", timeout=5) as r:
                health = json.loads(r.read().decode("utf-8"))
                self.assertEqual(len(health["categories"]), 6)

            # POST /api/collection/plan with unverified numeric node ID returns HTTP 400
            req = Request(
                f"{base}/api/collection/plan",
                data=json.dumps({"categories": ["9999999999"], "mode": "dry_run"}).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(HTTPError) as ctx:
                urlopen(req, timeout=5)
            self.assertEqual(ctx.exception.code, 400)

            # POST /api/collection/plan with newly verified T-Shirt node IDs succeeds in dry_run
            req_ok = Request(
                f"{base}/api/collection/plan",
                data=json.dumps(
                    {
                        "categories": ["1044544", "1045624", "9056923011", "9056987011"],
                        "observed_date": "2026-11-20",
                        "mode": "dry_run",
                    }
                ).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urlopen(req_ok, timeout=5) as r:
                plan_ok = json.loads(r.read().decode("utf-8"))
                self.assertEqual(plan_ok["pending_request_categories"], 4)
                self.assertEqual(plan_ok["estimated_total_credits"], 20)

            # /api/roadmap includes 6 configured target categories and undecided quota notice
            with urlopen(f"{base}/api/roadmap", timeout=5) as r:
                roadmap = json.loads(r.read().decode("utf-8"))
                self.assertEqual(len(roadmap["target_categories"]), 6)
                self.assertEqual(roadmap["category_readiness"]["configured_count"], 6)
                self.assertEqual(roadmap["category_readiness"]["pending_config_count"], 0)

            # /api/scoring/candidates supports category filter
            with urlopen(f"{base}/api/scoring/candidates?category=9056929011", timeout=5) as r:
                cands = json.loads(r.read().decode("utf-8"))
                self.assertTrue(cands["ready"])
                self.assertEqual(cands["category_filter"], "9056929011")
                self.assertFalse(cands["cross_category_quota_rule_defined"])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_7_real_snapshot_intact_and_immutable(self):
        if not DEFAULT_DB_PATH.is_file():
            self.skipTest("Real Sorftime DB not found")
        before_sha = hashlib.sha256(DEFAULT_DB_PATH.read_bytes()).hexdigest()

        repo = SnapshotRepository(DEFAULT_DB_PATH)
        ov_all = build_market_overview(repo.get_scope_data(category="all", dedup_mode="listing"))
        self.assertEqual(ov_all["kpis"]["observation_count"], 200)
        self.assertEqual(ov_all["kpis"]["raw_asin_count"], 155)
        self.assertEqual(ov_all["kpis"]["unique_asin_count"], 142)
        self.assertEqual(len(ov_all["categories"]), 6)

        svc = PreprocessingService(repo, app_db_path=self.app_db)
        prep_ov = svc.get_overview()
        self.assertEqual(prep_ov["counts"]["total"], 155)
        self.assertEqual(prep_ov["counts"]["excluded"], 0)
        self.assertEqual(
            prep_ov["counts"]["ready"]
            + prep_ov["counts"]["needs_review"]
            + prep_ov["counts"]["excluded"]
            + prep_ov["counts"]["blocked"],
            155,
        )
        self.assertGreaterEqual(prep_ov["counts"]["needs_review"], 5)
        self.assertEqual(len(prep_ov["categories"]), 6)

        after_sha = hashlib.sha256(DEFAULT_DB_PATH.read_bytes()).hexdigest()
        self.assertEqual(before_sha, after_sha)


if __name__ == "__main__":
    unittest.main()
