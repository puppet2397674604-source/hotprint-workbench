"""Tests for analytics metrics, anomaly isolation, category switching, search, and sorting."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from backend.analytics import build_market_overview, query_products
from backend.repository import SnapshotRepository
from tests.fixtures import create_sample_db


class TestAnalyticsAndQueries(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = create_sample_db(Path(self._tmp.name) / "sample.sqlite3")
        self.repo = SnapshotRepository(self.db_path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_zero_price_and_missing_rating_are_isolated(self):
        scope = self.repo.get_scope_data(category="all")
        overview = build_market_overview(scope)

        self.assertTrue(overview["ready"])
        kpis = overview["kpis"]
        self.assertEqual(kpis["observation_count"], 5)
        self.assertEqual(kpis["unique_asin_count"], 4)
        # Valid prices: 19.99, 24.99, 31.99 => median 24.99, excluded 1 ($0)
        self.assertEqual(kpis["price_valid_count"], 3)
        self.assertEqual(kpis["price_invalid_count"], 1)
        self.assertEqual(kpis["price_median"], 24.99)
        self.assertEqual(overview["price_analysis"]["excluded_invalid_count"], 1)
        self.assertEqual(len(overview["price_analysis"]["scatter"]), 3)

        # Valid ratings: 4.2, 4.6, 4.8 => median 4.6, missing 1
        self.assertEqual(kpis["rating_valid_count"], 3)
        self.assertEqual(kpis["rating_median"], 4.6)

        # Verify quality checks match
        checks = {c["key"]: c["count"] for c in overview["data_quality"]["checks"]}
        self.assertEqual(checks["cross_category"], 1)
        self.assertEqual(checks["invalid_price"], 1)
        self.assertEqual(checks["missing_rating"], 1)
        self.assertEqual(checks["same_parent_asin"], 1)
        self.assertEqual(checks["missing_source_bsr"], 1)

    def test_category_switch_and_bsr_sort_guard(self):
        scope_all = self.repo.get_scope_data(category="all")
        res_all = query_products(scope_all, sort="source_category_bsr", order="asc")
        self.assertFalse(res_all["can_sort_source_bsr"])
        self.assertEqual(res_all["sort"], "listing_sales_30d")
        self.assertIsNotNone(res_all["sort_warning"])

        scope_women = self.repo.get_scope_data(category="9056929011")
        res_women = query_products(scope_women, sort="source_category_bsr", order="asc")
        self.assertTrue(res_women["can_sort_source_bsr"])
        self.assertEqual(res_women["sort"], "source_category_bsr")
        self.assertIsNone(res_women["sort_warning"])
        # Expected order: BSR #5 (B0OVERLAP1), BSR #18 (B0ZEROPRC1), Missing BSR last (B0WOMEN003)
        self.assertEqual(
            [p["asin"] for p in res_women["items"]],
            ["B0OVERLAP1", "B0ZEROPRC1", "B0WOMEN003"],
        )

    def test_missing_values_always_sort_last_in_both_orders(self):
        scope = self.repo.get_scope_data(category="all")
        asc = query_products(scope, sort="sales_price_usd", order="asc")
        desc = query_products(scope, sort="sales_price_usd", order="desc")
        self.assertEqual(asc["items"][-1]["asin"], "B0ZEROPRC1")
        self.assertEqual(desc["items"][-1]["asin"], "B0ZEROPRC1")
        self.assertEqual(asc["items"][0]["asin"], "B0WOMEN003")  # $19.99
        self.assertEqual(desc["items"][0]["asin"], "B0MEN00002")  # $31.99

    def test_search_anomaly_filter_and_detail(self):
        scope = self.repo.get_scope_data(category="all")
        search_res = query_products(scope, q="dadHumor")
        self.assertEqual(search_res["total"], 1)
        self.assertEqual(search_res["items"][0]["asin"], "B0MEN00002")

        same_parent_res = query_products(scope, anomaly="same_parent_asin")
        self.assertEqual(same_parent_res["total"], 1)
        self.assertEqual(same_parent_res["items"][0]["asin"], "B0ZEROPRC1")
        self.assertIsNone(same_parent_res["items"][0]["amazon_url"])

        detail = self.repo.get_product_detail("B0OVERLAP1")
        self.assertIsNotNone(detail)
        self.assertEqual(len(detail["photos"]), 2)
        self.assertEqual(detail["ebc_photo_count"], 1)
        self.assertEqual(detail["preprocessing_placeholder"]["visual_print_status"], "pending_manual_review")

    def test_keyword_cloud_cleans_garment_noise_and_extracts_themes(self):
        scope = self.repo.get_scope_data(category="all")
        overview = build_market_overview(scope)
        cloud = overview["keyword_cloud"]
        self.assertGreater(len(cloud["keywords"]), 0)
        tokens = {k["token"] for k in cloud["keywords"]}
        # Garment / stopword noise must be removed
        for noise in ("sweatshirt", "women", "pullover", "for", "long", "sleeve", "printcraft"):
            self.assertNotIn(noise, tokens)
        # Real print / theme words must be preserved
        self.assertTrue({"christmas", "cat", "vintage"} & tokens)

    def test_ngram_phrases_punctuation_boundary_and_subword_suppression(self):
        from backend.keywords import extract_keyword_cloud

        synthetic = [
            # 3 products with "Thanksgiving Day" + punctuation barrier before "Ghost"
            {
                "asin": "B0KW000001", "brand": "StudioA", "listing_sales_30d": 2000,
                "title": "Happy Thanksgiving Day, Ghost Pumpkin Graphic Tee",
                "subtitle": "", "description": "Thanksgiving Day Thanksgiving Day celebration",
            },
            {
                "asin": "B0KW000002", "brand": "StudioB", "listing_sales_30d": 1800,
                "title": "Cute Thanksgiving Day | Ghost Pumpkin Sweatshirt",
                "subtitle": "", "description": "",
            },
            {
                "asin": "B0KW000003", "brand": "StudioC", "listing_sales_30d": 1500,
                "title": "Funny Thanksgiving Day - Cat Mom Pullover",
                "subtitle": "", "description": "",
            },
            # 3 products with "Super Hero" + "Cat, Mom" separated by comma (MUST NOT form "cat mom" across comma!)
            {
                "asin": "B0KW000004", "brand": "StudioD", "listing_sales_30d": 2500,
                "title": "Vintage Super Hero Comic T-Shirt, Cat, Mom Gift",
                "subtitle": "", "description": "",
            },
            {
                "asin": "B0KW000005", "brand": "StudioE", "listing_sales_30d": 2200,
                "title": "Retro Super-Hero Graphic Tee / Cat Mom Shirt",
                "subtitle": "", "description": "",
            },
            {
                "asin": "B0KW000006", "brand": "StudioF", "listing_sales_30d": 2100,
                "title": "Classic Super Hero Hoodie",
                "subtitle": "", "description": "",
            },
            # 4 products with "Ugly Christmas Sweater" + 4 extra standalone "Christmas" products
            {
                "asin": "B0KW000007", "brand": "StudioG", "listing_sales_30d": 3000,
                "title": "Funny Ugly Christmas Sweater Party Pullover",
                "subtitle": "", "description": "",
            },
            {
                "asin": "B0KW000008", "brand": "StudioH", "listing_sales_30d": 2900,
                "title": "Mens Ugly Xmas Sweater Holiday Tee",
                "subtitle": "", "description": "",
            },
            {
                "asin": "B0KW000009", "brand": "StudioI", "listing_sales_30d": 2800,
                "title": "Womens Ugly Christmas Sweater Festive Sweatshirt",
                "subtitle": "", "description": "",
            },
            {
                "asin": "B0KW000010", "brand": "StudioJ", "listing_sales_30d": 2700,
                "title": "Cute Ugly Christmas Sweater Reindeer Shirt",
                "subtitle": "", "description": "",
            },
            # Extra standalone Christmas items so Christmas ratio 4/8 = 0.5 < 0.6 -> deducted to 4, NOT suppressed!
            {"asin": "B0KW000011", "brand": "S1", "listing_sales_30d": 1200, "title": "Merry Christmas Tree Tee", "subtitle": "", "description": ""},
            {"asin": "B0KW000012", "brand": "S2", "listing_sales_30d": 1100, "title": "Merry Christmas Tree Sweatshirt", "subtitle": "", "description": ""},
            {"asin": "B0KW000013", "brand": "S3", "listing_sales_30d": 1000, "title": "Santa Christmas Bell Shirt", "subtitle": "", "description": ""},
            {"asin": "B0KW000014", "brand": "S4", "listing_sales_30d": 900, "title": "Elf Christmas Bell Shirt", "subtitle": "", "description": ""},
        ]

        cloud = extract_keyword_cloud(synthetic)
        by_tok = {k["token"]: k for k in cloud["keywords"]}

        # 1. Exact 2-gram and 3-gram phrases extracted with Document Frequency (no stuffing inflation)
        self.assertIn("thanksgiving day", by_tok)
        self.assertEqual(by_tok["thanksgiving day"]["doc_count"], 3)
        self.assertEqual(by_tok["thanksgiving day"]["word"], "Thanksgiving Day")
        self.assertEqual(by_tok["thanksgiving day"]["theme"], "holiday")

        self.assertIn("super hero", by_tok)
        self.assertEqual(by_tok["super hero"]["doc_count"], 3)
        self.assertEqual(by_tok["super hero"]["theme"], "motif")

        self.assertIn("ugly christmas sweater", by_tok)
        self.assertEqual(by_tok["ugly christmas sweater"]["doc_count"], 4)
        self.assertEqual(by_tok["ugly christmas sweater"]["theme"], "holiday")

        # 2. Sub-word suppression: ratio >= 0.6 hides sub-words ("thanksgiving", "day", "super", "hero", "ugly")
        for suppressed_tok in ("thanksgiving", "day", "super", "hero", "ugly", "ugly christmas"):
            self.assertNotIn(suppressed_tok, by_tok, f"{suppressed_tok} should be suppressed by parent phrase")

        # 3. Punctuation barrier: "day ghost" NEVER formed across comma/pipe; "cat mom" only counts 2 (NOT B0KW000004 with "Cat, Mom"!)
        self.assertNotIn("day ghost", by_tok)
        self.assertIn("cat mom", by_tok)
        self.assertEqual(by_tok["cat mom"]["doc_count"], 2)

        # 4. Multi-word phrase search in query_products matches case-insensitively & across hyphens/synonyms!
        mock_scope = {
            "status": type("S", (), {"state": "ready", "to_dict": lambda self: {"state": "ready"}})(),
            "selected_date": "2026-11-20",
            "category_filter": "all",
            "products": [
                {
                    **p,
                    "parent_asin": p["asin"],
                    "folded_asins": [],
                    "anomaly_count": 0,
                    "anomalies": [],
                    "price_valid": True,
                    "sales_price_usd": 19.99,
                    "rating_stars": 4.5,
                    "ratings_count": 50,
                    "main_category_bsr": 100,
                    "source_categories": [],
                    "is_cross_category": False,
                }
                for p in synthetic
            ],
        }
        q_hero = query_products(mock_scope, q="super hero")
        self.assertEqual(q_hero["total"], 3)  # matches "Super Hero" AND "Super-Hero"!
        q_ugly = query_products(mock_scope, q="ugly christmas sweater")
        self.assertEqual(q_ugly["total"], 4)  # matches "Ugly Christmas Sweater" AND "Ugly Xmas Sweater"!
        q_thanks = query_products(mock_scope, q="thanksgiving day")
        self.assertEqual(q_thanks["total"], 3)


if __name__ == "__main__":
    unittest.main()
