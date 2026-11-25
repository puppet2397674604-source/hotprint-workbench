"""Tests for read-only SQLite access, missing/empty DB handling, cross-category dedup, and real DB immutability."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from backend.config import DEFAULT_DB_PATH
from backend.db import inspect_database, open_readonly
from backend.repository import SnapshotRepository, collapse_duplicate_listings, extract_image_asset_key
from tests.fixtures import create_empty_schema_db, create_sample_db


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class TestDatabaseAndDedup(unittest.TestCase):
    def test_readonly_connection_blocks_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = create_sample_db(Path(tmp) / "sample.sqlite3")
            with open_readonly(db_path) as conn:
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute("DELETE FROM category_sales_observations")

    def test_missing_and_empty_databases_handled_gracefully(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does_not_exist.sqlite3"
            st_missing = inspect_database(missing)
            self.assertEqual(st_missing.state, "missing")
            self.assertFalse(st_missing.exists)

            blank_file = Path(tmp) / "blank.sqlite3"
            sqlite3.connect(blank_file).close()
            st_schema = inspect_database(blank_file)
            self.assertEqual(st_schema.state, "empty_schema")
            self.assertIn("category_sales_batches", st_schema.missing_objects)

            empty_data = create_empty_schema_db(Path(tmp) / "empty_data.sqlite3")
            st_empty = inspect_database(empty_data)
            self.assertEqual(st_empty.state, "empty_data")
            self.assertEqual(st_empty.batch_count, 0)

    def test_cross_category_deduplication_never_doubles_sales(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = create_sample_db(Path(tmp) / "sample.sqlite3")
            repo = SnapshotRepository(db_path)
            scope = repo.get_scope_data(category="all")

            # 3 women + 2 men = 5 observations, but 1 overlap ASIN => 4 unique ASINs
            self.assertEqual(len(scope["observations"]), 5)
            self.assertEqual(len(scope["products"]), 4)

            overlap = next(p for p in scope["products"] if p["asin"] == "B0OVERLAP1")
            self.assertTrue(overlap["is_cross_category"])
            # Sales must remain 3000 (NOT 6000!)
            self.assertEqual(overlap["listing_sales_30d"], 3000)
            self.assertEqual(len(overlap["source_categories"]), 2)
            node_bsrs = {s["node_id"]: s["source_category_bsr"] for s in overlap["source_categories"]}
            self.assertEqual(node_bsrs, {"9056929011": 5, "9056993011": 12})

    def test_tier2_listing_dedup_rules_and_guards(self):
        # 1. Same brand + exact same Description (or exact same Title) -> collapse under real parent listing without summing sales
        # 2. Different brands sharing Merch base image or same description -> MUST NOT collapse across brands
        def make_prod(asin, parent, brand, title, img, sales, desc="", vars_cnt=0, price_valid=False):
            return {
                "asin": asin,
                "parent_asin": parent,
                "asin_status": "父子编号相同，需核对" if asin == parent else "父子编号不同",
                "needs_parent_verification": asin == parent,
                "title": title,
                "description": desc,
                "brand": brand,
                "main_image_url": img,
                "image_asset_key": extract_image_asset_key(img),
                "listing_sales_30d": sales,
                "sales_price_usd": 20.0 if price_valid else 0.0,
                "price_valid": price_valid,
                "rating_stars": None,
                "ratings_count": 0,
                "main_category_bsr": 40000,
                "online_date": "2026-11-17",
                "variation_asin_count": vars_cnt,
                "amazon_url": None if asin == parent else f"https://www.amazon.com/dp/{asin}",
                "source_categories": [],
                "anomalies": ["same_parent_asin"] if asin == parent else [],
                "anomaly_count": 1 if asin == parent else 0,
            }

        shared_desc = "Oversized Tunic Fit: A boxy oversized cut through the chest."
        items = [
            make_prod("B0MAIN0001", "B0PARENT09", "Shiketong", "Womens Christmas Sweatshirts Merry Graphic Mock Neck Pullover", "https://m.media-amazon.com/images/I/61o9hPlZXHL.jpg", 10529, desc=shared_desc, vars_cnt=92),
            make_prod("B0JOLLY001", "B0JOLLY001", "Shiketong", "Womens Christmas Sweatshirts Merry Graphic Mock Neck Pullover", "https://m.media-amazon.com/images/I/71YiI5x7TmL.jpg", 5682, desc=shared_desc),
            make_prod("B0JOLLY002", "B0JOLLY002", "Shiketong", "Womens Christmas Sweatshirts Merry Graphic Mock Neck Pullover", "https://m.media-amazon.com/images/I/71YiI5x7TmL.jpg", 5678, desc=shared_desc),
            # Different brands sharing Merch base image: must stay separate!
            make_prod("B0MERCH001", "B0MERCH001", "BrandA", "Funny Halloween Sweatshirt", "https://m.media-amazon.com/images/I/A1mN82gBRyL.png", 773),
            make_prod("B0MERCH002", "B0MERCH002", "BrandB", "Funny Halloween Sweatshirt", "https://m.media-amazon.com/images/I/A1mN82gBRyL.png", 730),
        ]

        collapsed, meta = collapse_duplicate_listings(items)
        self.assertEqual(len(collapsed), 3)
        self.assertEqual(meta["cluster_count"], 1)
        self.assertEqual(meta["collapsed_asin_count"], 2)

        rep = next(p for p in collapsed if p["asin"] == "B0MAIN0001")
        self.assertEqual(rep["duplicate_cluster_size"], 3)
        self.assertEqual(rep["listing_sales_30d"], 10529)  # Never summed!
        self.assertEqual(set(rep["folded_asins"]), {"B0JOLLY001", "B0JOLLY002"})
        self.assertIn("B0MERCH001", [p["asin"] for p in collapsed])
        self.assertIn("B0MERCH002", [p["asin"] for p in collapsed])

    def test_real_snapshot_counts_and_immutability(self):
        if not DEFAULT_DB_PATH.exists():
            self.skipTest("Real SQLite snapshot not present in this checkout")
        before_hash = sha256_file(DEFAULT_DB_PATH)
        repo = SnapshotRepository(DEFAULT_DB_PATH)
        status, batches = repo.get_batches()
        self.assertEqual(status.state, "ready")
        self.assertEqual(len(batches), 2)

        # Tier-1 ASIN mode: 200 obs -> 155 ASINs
        scope_asin = repo.get_scope_data(category="all", dedup_mode="asin")
        self.assertEqual(len(scope_asin["observations"]), 200)
        self.assertEqual(len(scope_asin["products"]), 155)
        self.assertEqual(sum(1 for p in scope_asin["products"] if p["is_cross_category"]), 45)
        self.assertEqual(sum(1 for p in scope_asin["products"] if p["needs_parent_verification"]), 17)
        self.assertEqual(sum(1 for p in scope_asin["products"] if not p["price_valid"]), 31)
        self.assertEqual(sum(1 for p in scope_asin["products"] if p["rating_status"] != "valid"), 24)

        # Tier-2 listing clean mode (same brand + exact description/title/image):
        # 155 ASINs -> 142 unique listings (2 clusters: Shiketong 10 & Bwaydeg 5; 13 folded ASINs)
        scope_clean = repo.get_scope_data(category="all", dedup_mode="listing")
        self.assertEqual(len(scope_clean["products"]), 142)
        self.assertEqual(scope_clean["duplicate_meta"]["cluster_count"], 2)
        self.assertEqual(scope_clean["duplicate_meta"]["collapsed_asin_count"], 13)
        # Verify all 10 Shiketong exact-description/title items fold under real parent listing B0HQC86BJX
        shiketong_rep = next(p for p in scope_clean["products"] if p["asin"] == "B0HQC86BJX")
        self.assertEqual(shiketong_rep["duplicate_cluster_size"], 10)
        self.assertEqual(shiketong_rep["listing_sales_30d"], 10529)
        for jolly_asin in ("B0HQCLVVDK", "B0HQC3YX3Q", "B0HQCVJZK3", "B0HQCG8RFQ"):
            self.assertIn(jolly_asin, shiketong_rep["folded_asins"])
            self.assertNotIn(jolly_asin, [p["asin"] for p in scope_clean["products"]])

        after_hash = sha256_file(DEFAULT_DB_PATH)
        self.assertEqual(before_hash, after_hash)


if __name__ == "__main__":
    unittest.main()
