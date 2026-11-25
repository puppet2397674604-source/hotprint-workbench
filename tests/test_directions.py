"""Unit & HTTP API tests for Stage 05 Print Pattern Recognition & Direction Grouping."""

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
from backend.directions import (
    DirectionsService,
    aggregate_direction_metrics,
)
from backend.preprocessing import PreprocessingService
from backend.repository import SnapshotRepository
from backend.scoring import ScoringService
from backend.server import create_server
from tests.test_batch_and_scoring import _build_synthetic_snapshot_db


class TestStage05Directions(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        self.snap_db = self.tmp_dir / "snap.sqlite3"
        self.app_db = self.tmp_dir / "app.sqlite3"
        _build_synthetic_snapshot_db(self.snap_db)
        self.repo = SnapshotRepository(self.snap_db)
        self.prep = PreprocessingService(self.repo, app_db_path=self.app_db)
        self.scoring = ScoringService(self.prep, app_db_path=self.app_db)
        self.directions = DirectionsService(self.scoring, app_db_path=self.app_db)

    def tearDown(self):
        self._tmp.cleanup()

    # ------------------------------------------------------------------
    # 1. Pure Aggregation Algorithm Boundary Tests
    # ------------------------------------------------------------------
    def test_aggregation_same_parent_folding_no_sales_summing_and_missing_parent(self):
        # 3 ASINs sharing the SAME parent_asin -> parent_asin_count MUST be 1, breadth_bonus = 0!
        same_parent_items = [
            {
                "asin": "B0SIB00001", "parent_asin": "B0PAR00001", "brand": "BrandA",
                "source_categories": [{"node_id": "9056923011", "source_category": "女式趣味 T 恤", "garment_type": "t_shirt", "gender": "women"}],
                "garment_types": ["t_shirt"], "genders": ["women"],
                "listing_sales_30d": 3000, "price": 19.99, "price_valid": True, "total_score": 72.0,
            },
            {
                "asin": "B0SIB00002", "parent_asin": "B0PAR00001", "brand": "BrandA",
                "source_categories": [{"node_id": "9056923011", "source_category": "女式趣味 T 恤", "garment_type": "t_shirt", "gender": "women"}],
                "garment_types": ["t_shirt"], "genders": ["women"],
                "listing_sales_30d": 2000, "price": 21.99, "price_valid": True, "total_score": 65.0,
            },
            {
                "asin": "B0SIB00003", "parent_asin": "B0PAR00001", "brand": "BrandA",
                "source_categories": [{"node_id": "9056923011", "source_category": "女式趣味 T 恤", "garment_type": "t_shirt", "gender": "women"}],
                "garment_types": ["t_shirt"], "genders": ["women"],
                "listing_sales_30d": 1000, "price": 0.0, "price_valid": False, "total_score": 55.0,
            },
        ]
        m1 = aggregate_direction_metrics(same_parent_items, feasibility_rating=3)
        self.assertEqual(m1["asin_count"], 3)
        self.assertEqual(m1["parent_asin_count"], 1)
        self.assertEqual(m1["brand_count"], 1)
        # Never sums sales (3000+2000+1000 = 6000 is FORBIDDEN!)
        self.assertEqual(m1["max_sales_30d"], 3000)
        self.assertEqual(m1["median_sales_30d"], 2000)
        # $0 invalid price excluded from price_range
        self.assertEqual(m1["price_range"], {"min": 19.99, "max": 21.99})
        self.assertEqual(m1["peak_product_score"], 72.0)
        self.assertEqual(m1["score_breakdown"]["breadth_bonus"], 0.0)
        self.assertEqual(m1["score_breakdown"]["cross_category_bonus"], 0.0)
        self.assertEqual(m1["score_breakdown"]["feasibility_delta"], 0.0)
        self.assertEqual(m1["direction_score"], 72.0)

        # Now compare with 3 INDEPENDENT parents (including 1 missing parent_asin -> own ASIN) + cross-category!
        multi_parent_items = [
            same_parent_items[0],
            {
                "asin": "B0IND00002", "parent_asin": None, "brand": "BrandB",  # missing parent -> counts as own unit
                "source_categories": [{"node_id": "9056929011", "source_category": "女式趣味卫衣", "garment_type": "sweatshirt", "gender": "women"}],
                "garment_types": ["sweatshirt"], "genders": ["women"],
                "listing_sales_30d": 2500, "price": 32.99, "price_valid": True, "total_score": 68.0,
            },
            {
                "asin": "B0IND00003", "parent_asin": "B0PAR00003", "brand": "BrandC",
                "source_categories": [{"node_id": "9056987011", "source_category": "男式趣味 T 恤", "garment_type": "t_shirt", "gender": "men"}],
                "garment_types": ["t_shirt"], "genders": ["men"],
                "listing_sales_30d": 1500, "price": 17.50, "price_valid": True, "total_score": 60.0,
            },
        ]
        m2 = aggregate_direction_metrics(multi_parent_items, feasibility_rating=5)
        self.assertEqual(m2["asin_count"], 3)
        self.assertEqual(m2["parent_asin_count"], 3)
        self.assertEqual(m2["brand_count"], 3)
        self.assertEqual(m2["max_sales_30d"], 3000)
        self.assertEqual(m2["median_sales_30d"], 2500)
        self.assertEqual(m2["price_range"], {"min": 17.50, "max": 32.99})
        expected_breadth = round(min(20.0, 10.0 * math.log(3)), 1)
        self.assertEqual(m2["score_breakdown"]["breadth_bonus"], expected_breadth)
        self.assertEqual(m2["score_breakdown"]["cross_category_bonus"], 8.0)
        self.assertEqual(m2["score_breakdown"]["feasibility_delta"], 6.0)
        expected_total = round(72.0 + min(20.0, 10.0 * math.log(3)) + 8.0 + 6.0, 1)
        self.assertEqual(m2["direction_score"], expected_total)
        self.assertGreater(m2["direction_score"], m1["direction_score"])

        # Feasibility 1 star -> -6 adjustment
        m_low = aggregate_direction_metrics([same_parent_items[0]], feasibility_rating=1)
        self.assertEqual(m_low["score_breakdown"]["feasibility_delta"], -6.0)
        self.assertEqual(m_low["direction_score"], 66.0)

    # ------------------------------------------------------------------
    # 2. Schema, Many-to-Many, Duplicate Guard & Cascade Delete Tests
    # ------------------------------------------------------------------
    def test_db_tables_many_to_many_duplicate_guard_and_cascade_delete(self):
        # Create two directions, link same ASIN B0GRPH0001 to BOTH (allowed many-to-many)
        c1 = self.directions.create_direction(
            {
                "name": "复古万圣节南瓜梗",
                "style_tags": "复古做旧, 手绘插画, 节日梗",
                "target_audience": "年轻女性",
                "feasibility_rating": 4,
                "asins": ["B0GRPH0001", "B0GRPH0002"],
                "observed_date": "2026-11-20",
            }
        )
        d1 = c1["direction"]
        self.assertEqual(d1["metrics"]["asin_count"], 2)
        self.assertEqual(d1["style_tags"], ["复古做旧", "手绘插画", "节日梗"])

        # Duplicate link inside SAME direction is blocked/deduplicated by UNIQUE(direction_id, asin)
        dup_res = self.directions.add_links(d1["id"], ["B0GRPH0001", "B0SWET0001"], observed_date="2026-11-20")
        self.assertEqual(dup_res["added_count"], 1)
        self.assertEqual(dup_res["duplicate_count"], 1)
        self.assertEqual(dup_res["duplicate_asins"], ["B0GRPH0001"])
        self.assertEqual(dup_res["direction"]["metrics"]["asin_count"], 3)

        # Verify SQLite UNIQUE constraint rejects raw duplicate insert too
        conn = sqlite3.connect(self.app_db)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO direction_asin_links (direction_id, asin, added_at) VALUES (?, ?, ?)",
                (d1["id"], "B0GRPH0001", "2026-11-20T00:00:00Z"),
            )
        conn.close()

        # Create 2nd direction with same ASIN B0GRPH0001 (cross-theme many-to-many allowed)
        c2 = self.directions.create_direction(
            {
                "name": "跨节庆通用幽默文案",
                "asins": ["B0GRPH0001"],
                "observed_date": "2026-11-20",
            }
        )
        d2 = c2["direction"]
        self.assertEqual(d2["metrics"]["asin_count"], 1)

        # Candidates endpoint reports BOTH assigned directions for B0GRPH0001
        cands = self.directions.get_candidates(observed_date="2026-11-20", assigned="all")
        g1 = next(i for i in cands["items"] if i["asin"] == "B0GRPH0001")
        self.assertEqual(len(g1["assigned_directions"]), 2)

        # Delete d1 -> cascades its links, leaves d2 AND preprocessed_products intact!
        del_res = self.directions.delete_direction(d1["id"])
        self.assertEqual(del_res["removed_links_count"], 3)
        conn = sqlite3.connect(self.app_db)
        rem_d1 = conn.execute("SELECT COUNT(*) FROM direction_asin_links WHERE direction_id = ?", (d1["id"],)).fetchone()[0]
        rem_d2 = conn.execute("SELECT COUNT(*) FROM direction_asin_links WHERE direction_id = ?", (d2["id"],)).fetchone()[0]
        prep_cnt = conn.execute("SELECT COUNT(*) FROM preprocessed_products").fetchone()[0]
        conn.close()
        self.assertEqual(rem_d1, 0)
        self.assertEqual(rem_d2, 1)
        self.assertGreaterEqual(prep_cnt, 6)

    # ------------------------------------------------------------------
    # 3. Full HTTP API Lifecycle (GET/POST/PATCH/DELETE + Real DB Check)
    # ------------------------------------------------------------------
    def test_http_directions_full_lifecycle(self):
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
            # 1. GET /api/directions/candidates
            with urlopen(f"{base}/api/directions/candidates?top_n=50&assigned=unassigned", timeout=5) as r:
                cands = json.loads(r.read())
            self.assertTrue(cands["ready"])
            self.assertEqual(cands["counts"]["unassigned_count"], 3)

            # 2. POST /api/directions (create with 2 initial ASINs)
            req_create = Request(
                f"{base}/api/directions",
                data=json.dumps(
                    {
                        "name": "复古猫咪与南瓜印花",
                        "style_tags": ["复古", "手绘"],
                        "target_audience": "年轻女性",
                        "feasibility_rating": 4,
                        "asins": ["B0GRPH0001", "B0GRPH0002"],
                    }
                ).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req_create, timeout=5) as r:
                self.assertEqual(r.status, 201)
                created = json.loads(r.read())
            did = created["direction"]["id"]
            self.assertEqual(created["direction"]["metrics"]["asin_count"], 2)

            # 3. POST /api/directions/<id>/links (add 3rd ASIN + duplicate)
            req_link = Request(
                f"{base}/api/directions/{did}/links",
                data=json.dumps({"asins": ["B0GRPH0001", "B0SWET0001"]}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req_link, timeout=5) as r:
                linked = json.loads(r.read())
            self.assertEqual(linked["added_count"], 1)
            self.assertEqual(linked["duplicate_count"], 1)
            self.assertEqual(linked["direction"]["metrics"]["asin_count"], 3)

            # 4. PATCH /api/directions/<id> (shortlist + 5 stars)
            req_patch = Request(
                f"{base}/api/directions/{did}",
                data=json.dumps({"status": "shortlisted", "feasibility_rating": 5}).encode(),
                headers={"Content-Type": "application/json"},
                method="PATCH",
            )
            with urlopen(req_patch, timeout=5) as r:
                patched = json.loads(r.read())
            self.assertEqual(patched["direction"]["status"], "shortlisted")
            self.assertEqual(patched["direction"]["feasibility_rating"], 5)

            # 5. DELETE /api/directions/<id>/links (unlink 1 ASIN)
            req_unlink = Request(
                f"{base}/api/directions/{did}/links",
                data=json.dumps({"asins": ["B0SWET0001"]}).encode(),
                headers={"Content-Type": "application/json"},
                method="DELETE",
            )
            with urlopen(req_unlink, timeout=5) as r:
                unlinked = json.loads(r.read())
            self.assertEqual(unlinked["removed_count"], 1)
            self.assertEqual(unlinked["direction"]["metrics"]["asin_count"], 2)

            # 6. GET /api/directions -> verify persisted list
            with urlopen(f"{base}/api/directions", timeout=5) as r:
                listed = json.loads(r.read())
            self.assertEqual(len(listed["items"]), 1)
            self.assertEqual(listed["status_counts"]["shortlisted"], 1)

            # 7. DELETE /api/directions/<id>
            with urlopen(Request(f"{base}/api/directions/{did}", method="DELETE"), timeout=5) as r:
                deleted = json.loads(r.read())
            self.assertEqual(deleted["deleted_id"], did)

            with urlopen(f"{base}/api/directions", timeout=5) as r:
                listed_after = json.loads(r.read())
            self.assertEqual(len(listed_after["items"]), 0)
        finally:
            server.shutdown()
            server.server_close()

    def test_real_snapshot_candidates_and_immutability(self):
        if not DEFAULT_DB_PATH.exists():
            self.skipTest("Original Sorftime SQLite snapshot not present")
        hash_before = hashlib.sha256(DEFAULT_DB_PATH.read_bytes()).hexdigest()
        real_repo = SnapshotRepository(DEFAULT_DB_PATH)
        real_prep = PreprocessingService(real_repo, app_db_path=self.tmp_dir / "real_app.sqlite3")
        real_score = ScoringService(real_prep, app_db_path=self.tmp_dir / "real_app.sqlite3")
        real_dirs = DirectionsService(real_score, app_db_path=self.tmp_dir / "real_app.sqlite3")

        cands = real_dirs.get_candidates(top_n=80, assigned="unassigned")
        self.assertTrue(cands["ready"])
        self.assertEqual(len(cands["items"]), 80)

        top3 = [c["asin"] for c in cands["items"][:3]]
        created = real_dirs.create_direction(
            {"name": "真实数据方向验证", "asins": top3, "status": "shortlisted", "feasibility_rating": 4}
        )
        self.assertEqual(created["direction"]["metrics"]["asin_count"], 3)
        self.assertGreater(created["direction"]["direction_score"], 0)

        # Unassigned count drops by 3
        cands_after = real_dirs.get_candidates(top_n=80, assigned="unassigned")
        self.assertEqual(cands_after["counts"]["assigned_count"], 3)
        self.assertEqual(cands_after["counts"]["unassigned_count"], 77)

        hash_after = hashlib.sha256(DEFAULT_DB_PATH.read_bytes()).hexdigest()
        self.assertEqual(hash_before, hash_after)

    # ------------------------------------------------------------------
    # 4. Stage 06 Top 10 Output, Boundary States (0 / <10 / >10) & JSON/CSV Export
    # ------------------------------------------------------------------
    def test_stage06_top10_boundaries_and_json_csv_exports(self):
        import csv
        import io

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
            # (a) 0 shortlisted -> empty list, never pads fake directions
            with urlopen(f"{base}/api/directions/top10", timeout=5) as r:
                t0 = json.loads(r.read())
            self.assertEqual(t0["summary"]["shortlisted_count"], 0)
            self.assertFalse(t0["summary"]["exceeds_limit"])
            self.assertEqual(t0["items"], [])

            # (b) Create 2 shortlisted + 1 draft (draft must NOT appear in top10!)
            # Note B0PLAIN001 & B0PLAIN002 share parent B0PAR00001 -> tests same-parent folding in export!
            self.directions.create_direction(
                {
                    "name": "方向A-高分跨类目印花",
                    "style_tags": ["复古", "节日"],
                    "target_audience": "年轻女性",
                    "feasibility_rating": 5,
                    "notes": "保留复古做旧字体，可替换为冬季宠物元素",
                    "status": "shortlisted",
                    "asins": ["B0GRPH0001", "B0GRPH0002", "B0SWET0001"],
                }
            )
            self.directions.create_direction(
                {
                    "name": "方向B-同父体折叠验证",
                    "style_tags": ["极简"],
                    "target_audience": "通用",
                    "feasibility_rating": 3,
                    "notes": "核对父体折叠",
                    "status": "shortlisted",
                    "asins": ["B0PLAIN001", "B0PLAIN002"],
                }
            )
            self.directions.create_direction(
                {
                    "name": "方向C-仅草稿不入选",
                    "status": "draft",
                    "asins": ["B0GRPH0001"],
                }
            )

            with urlopen(f"{base}/api/directions/top10", timeout=5) as r:
                t2 = json.loads(r.read())
            self.assertEqual(t2["summary"]["shortlisted_count"], 2)
            self.assertFalse(t2["summary"]["exceeds_limit"])
            self.assertEqual(len(t2["items"]), 2)
            # Sorted by direction_score DESC -> 方向A ranks #1
            self.assertEqual(t2["items"][0]["rank"], 1)
            self.assertEqual(t2["items"][0]["name"], "方向A-高分跨类目印花")
            self.assertGreaterEqual(t2["items"][0]["direction_score"], t2["items"][1]["direction_score"])
            # Supporting products include required handoff fields
            sp0 = t2["items"][0]["supporting_products"][0]
            for field in ("asin", "parent_asin", "title", "main_image_url", "amazon_url", "sales_price_usd", "listing_sales_30d", "covered_categories", "confidence_level"):
                self.assertIn(field, sp0)

            # (c) Export JSON -> verify Content-Disposition & exact parity with /api/directions/top10
            with urlopen(f"{base}/api/directions/export?format=json", timeout=5) as r:
                self.assertIn("attachment;", r.headers.get("Content-Disposition", ""))
                self.assertIn(".json", r.headers.get("Content-Disposition", ""))
                exp_json = json.loads(r.read().decode("utf-8"))
            self.assertEqual(exp_json["total_directions"], 2)
            self.assertEqual(exp_json["data_snapshot_date"], "2026-11-20")
            self.assertEqual(exp_json["directions"][0]["name"], "方向A-高分跨类目印花")
            # Confirm no sales summing (5000, 3200, 1800 -> max=5000, median=3200, NEVER 10000!)
            self.assertEqual(exp_json["directions"][0]["max_sales_30d"], 5000)
            self.assertEqual(exp_json["directions"][0]["median_sales_30d"], 3200)
            # Confirm same-parent folding in direction B (B0PLAIN001 + B0PLAIN002 share B0PAR00001 -> parent_asin_count=1!)
            self.assertEqual(exp_json["directions"][1]["asin_count"], 2)
            self.assertEqual(exp_json["directions"][1]["parent_asin_count"], 1)

            # (d) Export CSV -> verify UTF-8 BOM, headers, and row parity
            with urlopen(f"{base}/api/directions/export?format=csv", timeout=5) as r:
                self.assertIn("attachment;", r.headers.get("Content-Disposition", ""))
                self.assertIn(".csv", r.headers.get("Content-Disposition", ""))
                raw_csv = r.read()
            self.assertTrue(raw_csv.startswith(b"\xef\xbb\xbf"), "CSV must start with UTF-8 BOM for Excel")
            rows = list(csv.reader(io.StringIO(raw_csv.decode("utf-8-sig"))))
            self.assertEqual(len(rows), 3)  # header + 2 shortlisted directions
            self.assertEqual(rows[1][2], "方向A-高分跨类目印花")
            self.assertEqual(rows[1][11], "5000")  # max_sales_30d (not summed!)
            self.assertEqual(rows[2][7], "1")  # parent_asin_count folded to 1!
            self.assertEqual(rows[2][8], "2")  # asin_count = 2

            # Invalid export format -> 400
            with self.assertRaises(HTTPError) as ctx:
                urlopen(f"{base}/api/directions/export?format=xml", timeout=5)
            self.assertEqual(ctx.exception.code, 400)

            # (e) >10 shortlisted -> exceeds_limit == True
            for i in range(9):
                self.directions.create_direction(
                    {"name": f"扩展入选方向-{i+3}", "status": "shortlisted", "asins": ["B0GRPH0002"]}
                )
            with urlopen(f"{base}/api/directions/top10", timeout=5) as r:
                t11 = json.loads(r.read())
            self.assertEqual(t11["summary"]["shortlisted_count"], 11)
            self.assertTrue(t11["summary"]["exceeds_limit"])
        finally:
            server.shutdown()
            server.server_close()

    def test_direction_score_trims_price_anomaly_and_sales_suspect_outliers(self):
        """Mixed direction ($1.00 anomalous high-sales + normal product): peak_product_score excludes $1.00 item."""
        mixed_products = [
            {
                "asin": "B0ANOM0001",
                "parent_asin": "B0PARA0001",
                "brand": "StripeBrand",
                "source_categories": [
                    {"node_id": "9056929011", "garment_type": "sweatshirt", "gender": "women", "short_name": "女趣卫衣"},
                ],
                "garment_types": ["sweatshirt"],
                "genders": ["women"],
                "total_score": 92.5,
                "listing_sales_30d": 169328,
                "price": 1.00,
                "price_valid": True,
                "price_anomaly": True,
                "sales_suspect": True,
            },
            {
                "asin": "B0NORM0002",
                "parent_asin": "B0PARN0002",
                "brand": "HonestBrand",
                "source_categories": [
                    {"node_id": "9056929011", "garment_type": "sweatshirt", "gender": "women", "short_name": "女趣卫衣"},
                ],
                "garment_types": ["sweatshirt"],
                "genders": ["women"],
                "total_score": 54.0,
                "listing_sales_30d": 2155,
                "price": 24.99,
                "price_valid": True,
                "price_anomaly": False,
                "sales_suspect": False,
            },
        ]
        m = aggregate_direction_metrics(mixed_products, feasibility_rating=3)
        self.assertEqual(m["peak_product_score"], 54.0)
        self.assertEqual(m["raw_peak_product_score"], 92.5)
        self.assertFalse(m["all_anomalous"])
        self.assertEqual(m["excluded_anomaly_count"], 1)
        self.assertEqual(m["anomalous_asins"], ["B0ANOM0001"])
        expected = round(54.0 + 10.0 * math.log(2), 1)
        self.assertAlmostEqual(m["direction_score"], expected, places=1)
        self.assertLess(m["direction_score"], 70.0)

        # Defensive check: even without explicit price_anomaly flag, $1.00 is still caught and trimmed
        implicit_price_anom = [
            {**mixed_products[0], "price_anomaly": False, "sales_suspect": False},
            mixed_products[1],
        ]
        m2 = aggregate_direction_metrics(implicit_price_anom, feasibility_rating=3)
        self.assertEqual(m2["peak_product_score"], 54.0)
        self.assertEqual(m2["excluded_anomaly_count"], 1)
        self.assertFalse(m2["all_anomalous"])

    def test_direction_score_all_anomalous_fallback_no_crash(self):
        """All-anomalous direction falls back to raw max score without crashing and returns all_anomalous=True."""
        all_bad = [
            {
                "asin": "B0BAD00001",
                "parent_asin": "B0PBD00001",
                "brand": "BadA",
                "source_categories": [
                    {"node_id": "9056929011", "garment_type": "sweatshirt", "gender": "women", "short_name": "女趣卫衣"},
                ],
                "garment_types": ["sweatshirt"],
                "genders": ["women"],
                "total_score": 88.4,
                "listing_sales_30d": 120000,
                "price": 1.00,
                "price_valid": True,
                "price_anomaly": True,
                "sales_suspect": False,
            },
            {
                "asin": "B0BAD00002",
                "parent_asin": "B0PBD00002",
                "brand": "BadB",
                "source_categories": [
                    {"node_id": "9056929011", "garment_type": "sweatshirt", "gender": "women", "short_name": "女趣卫衣"},
                ],
                "garment_types": ["sweatshirt"],
                "genders": ["women"],
                "total_score": 76.2,
                "listing_sales_30d": 80000,
                "price": 29.99,
                "price_valid": True,
                "price_anomaly": False,
                "sales_suspect": True,
            },
        ]
        m = aggregate_direction_metrics(all_bad, feasibility_rating=3)
        self.assertTrue(m["all_anomalous"])
        self.assertEqual(m["peak_product_score"], 88.4)
        self.assertEqual(m["raw_peak_product_score"], 88.4)
        self.assertEqual(m["excluded_anomaly_count"], 0)
        self.assertEqual(set(m["anomalous_asins"]), {"B0BAD00001", "B0BAD00002"})
        self.assertTrue(m["score_breakdown"]["all_anomalous"])
        expected = round(88.4 + 10.0 * math.log(2), 1)
        self.assertAlmostEqual(m["direction_score"], expected, places=1)

    def test_direction_season_majority_vote_ties_and_holiday_countdown(self):
        """P3: Majority vote season classification (ties & no-cache -> evergreen) + holiday countdown."""
        from datetime import date
        from backend.directions import compute_days_until_holiday, compute_direction_season

        ref = date(2026, 11, 22)

        # 1) Clear seasonal majority (2 christmas vs 1 evergreen)
        s_xmas = compute_direction_season(
            [
                {"asin": "A1", "ai_analysis": {"season_tag": "christmas"}},
                {"asin": "A2", "ai_analysis": {"season_tag": "christmas"}},
                {"asin": "A3", "ai_analysis": {"season_tag": "evergreen"}},
            ],
            reference_date=ref,
        )
        self.assertEqual(s_xmas["season_tag"], "christmas")
        self.assertTrue(s_xmas["is_seasonal"])
        self.assertEqual(s_xmas["season_bucket"], "seasonal")
        self.assertEqual(s_xmas["days_until_holiday"], 33)  # 2026-12-25 - 2026-11-22

        # 2) Thanksgiving countdown (2026-11-26 - 2026-11-22 = 4 days)
        self.assertEqual(compute_days_until_holiday("thanksgiving", reference_date=ref), 4)
        self.assertEqual(compute_days_until_holiday("valentines", reference_date=ref), 84)
        self.assertIsNone(compute_days_until_holiday("evergreen", reference_date=ref))

        # 3) Tie between christmas (1) and evergreen (1) -> evergreen fallback
        s_tie_ev = compute_direction_season(
            [
                {"asin": "B1", "ai_analysis": {"season_tag": "christmas"}},
                {"asin": "B2", "ai_analysis": {"season_tag": "evergreen"}},
            ],
            reference_date=ref,
        )
        self.assertEqual(s_tie_ev["season_tag"], "evergreen")
        self.assertFalse(s_tie_ev["is_seasonal"])
        self.assertIsNone(s_tie_ev["days_until_holiday"])

        # 4) Tie between two different seasonal tags -> evergreen fallback
        s_tie_two = compute_direction_season(
            [
                {"asin": "C1", "ai_analysis": {"season_tag": "christmas"}},
                {"asin": "C2", "ai_analysis": {"season_tag": "thanksgiving"}},
            ],
            reference_date=ref,
        )
        self.assertEqual(s_tie_two["season_tag"], "evergreen")
        self.assertFalse(s_tie_two["is_seasonal"])

        # 5) No AI cache / empty season_tag -> evergreen fallback
        s_empty = compute_direction_season(
            [
                {"asin": "D1", "ai_analysis": None},
                {"asin": "D2", "ai_analysis": {"season_tag": ""}},
            ],
            reference_date=ref,
        )
        self.assertEqual(s_empty["season_tag"], "evergreen")
        self.assertFalse(s_empty["is_seasonal"])
        self.assertIsNone(s_empty["days_until_holiday"])

    def test_export_backward_compatibility_and_p3_appended_fields(self):
        """P3: JSON & CSV export preserve all legacy fields/columns and append seasonal + variants at the end."""
        import csv
        import io
        import sqlite3
        from backend.ai_service import AIService, compute_evidence_hash

        # Ensure preprocessed rows & product_ai_cache exist, then seed seasonal v2 cache for B0GRPH0001
        AIService(app_db_path=self.app_db, config_path=self.tmp_dir / "ai.local")
        self.prep._ensure_preprocessed("2026-11-20")
        hydrated = {i["asin"]: i for i in self.prep._hydrate_rows("2026-11-20")}
        p1 = hydrated["B0GRPH0001"]
        with sqlite3.connect(self.app_db) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO product_ai_cache
                   (asin, evidence_hash, model, prompt_version, is_graphic_print, pattern_type, confidence,
                    filter_reason, print_subject, slogan_text, visual_style, theme_tags_json,
                    season_tag, motif, transferability, raw_json, updated_at)
                   VALUES (?, ?, 'deepseek-flash', 'v2', 1, 'graphic_print', 0.95,
                           '圣诞主题印花', '圣诞复古标语', 'MERRY', '复古', '["christmas"]',
                           'christmas', 'santa', 'pure_graphic', '{}', '2026-11-22T00:00:00Z')""",
                ("B0GRPH0001", compute_evidence_hash(p1["title"], p1["image_url"])),
            )
            conn.commit()

        self.directions.create_direction(
            {
                "name": "圣诞复古标语方向",
                "status": "shortlisted",
                "asins": ["B0GRPH0001"],
                "variant_suggestions": [
                    "【构图】改用徽章圆环构图适配胸口小图",
                    "【配色】替换为酒红+奶油白复古做旧配色",
                    "【文案】将标语延展为宠物主题节日双关梗",
                ],
            }
        )
        self.directions.create_direction(
            {
                "name": "日常常青印花方向",
                "status": "shortlisted",
                "asins": ["B0GRPH0002"],
            }
        )

        top10 = self.directions.get_top10()
        self.assertEqual(top10["summary"]["shortlisted_count"], 2)
        self.assertEqual(top10["summary"]["seasonal_count"], 1)
        self.assertEqual(top10["summary"]["evergreen_count"], 1)

        pkg = self.directions.build_export_package()
        by_name = {d["name"]: d for d in pkg["directions"]}
        xmas_d = by_name["圣诞复古标语方向"]
        ev_d = by_name["日常常青印花方向"]

        # Legacy JSON keys intact + new P3 fields present
        for legacy_key in ("rank", "id", "name", "direction_score", "parent_asin_count", "supporting_products", "direction_type", "source"):
            self.assertIn(legacy_key, xmas_d)
        self.assertEqual(xmas_d["season_tag"], "christmas")
        self.assertIsNotNone(xmas_d["days_until_holiday"])
        self.assertEqual(len(xmas_d["variant_suggestions"]), 3)
        self.assertEqual(ev_d["season_tag"], "evergreen")
        self.assertIsNone(ev_d["days_until_holiday"])
        self.assertEqual(ev_d["variant_suggestions"], [])

        # CSV backward compatibility: columns 0..21 identical, P3 appended at 22..24
        _, csv_bytes = self.directions.build_export_csv()
        rows = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8-sig"))))
        headers = rows[0]
        self.assertEqual(
            headers[:22],
            [
                "排名", "方向ID", "方向主题名称", "方向综合分", "可行性星级", "风格标签", "目标受众",
                "独立父体数", "关联ASIN数", "独立品牌数", "覆盖类目", "最高30天估算销量", "中位30天估算销量",
                "有效价格带(USD)", "整体可信度", "设计交接备注", "代表性主图URL", "关联ASIN列表",
                "数据快照日期", "导出时间", "方向类型", "创建来源",
            ],
        )
        self.assertEqual(headers[22:25], ["季节归属", "距节日天数", "二创变体建议"])
        xmas_row = next(r for r in rows[1:] if r[2] == "圣诞复古标语方向")
        self.assertEqual(xmas_row[22], "christmas")
        self.assertNotEqual(xmas_row[23], "")
        self.assertIn("徽章圆环构图", xmas_row[24])

    def test_generate_variants_http_dry_run_and_mock_generation(self):
        """P3: POST /api/directions/<id>/generate-variants supports dry_run (0 calls) + mocked generation."""
        from backend.ai_service import AIService
        from urllib.error import HTTPError

        calls = []

        def mock_http(url, headers, payload, timeout):
            calls.append(payload)
            content = json.dumps(
                {
                    "variant_suggestions": [
                        "【构图】将中心大图改为复古邮票边框布局",
                        "【配色】推出复古褪色炭黑底+奶黄字的高对比版本",
                        "【文案】把节日词替换为咖啡+猫咪日常双关句延长售卖期",
                        "【受众】衍生出教师/护士职业专属细分款式",
                    ]
                },
                ensure_ascii=False,
            )
            return 200, {"choices": [{"message": {"role": "assistant", "content": content}}]}

        ai_cfg = self.tmp_dir / "ai_p3.local"
        ai_svc = AIService(app_db_path=self.app_db, config_path=ai_cfg, http_requester=mock_http, retry_delay=0.0)
        ai_svc.save_config({"api_key": "sk-p3-variants-test-9999", "model": "deepseek-flash"})

        created = self.directions.create_direction(
            {"name": "待补齐变体方向", "status": "shortlisted", "asins": ["B0GRPH0001"]}
        )["direction"]
        dir_id = created["id"]

        server, ctx = create_server(
            host="127.0.0.1",
            port=0,
            db_path=self.snap_db,
            app_db_path=self.app_db,
            ai_config_path=ai_cfg,
        )
        ctx.ai.http_requester = mock_http
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        base = f"http://127.0.0.1:{port}"
        try:
            # 1) dry_run=true -> 0 external calls, 0 DB writes
            req_dry = Request(
                f"{base}/api/directions/{dir_id}/generate-variants",
                data=json.dumps({"dry_run": True}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req_dry, timeout=5) as r:
                dry_res = json.loads(r.read())
            self.assertTrue(dry_res["dry_run"])
            self.assertEqual(dry_res["api_calls"], 1)
            self.assertEqual(len(calls), 0)
            self.assertEqual(self.directions.get_direction(dir_id)["variant_suggestions"], [])

            # 2) dry_run=false -> executes 1 mocked call and persists 4 variants
            req_real = Request(
                f"{base}/api/directions/{dir_id}/generate-variants",
                data=json.dumps({"dry_run": False}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req_real, timeout=5) as r:
                real_res = json.loads(r.read())
            self.assertFalse(real_res["dry_run"])
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(real_res["variant_suggestions"]), 4)
            self.assertEqual(len(real_res["direction"]["variant_suggestions"]), 4)

            # Persisted in SQLite and returned in Stage 06 Top 10
            with urlopen(f"{base}/api/directions/top10", timeout=5) as r:
                t10 = json.loads(r.read())
            found = next(d for d in t10["items"] if d["id"] == dir_id)
            self.assertEqual(len(found["variant_suggestions"]), 4)

            # 3) Non-existent direction ID -> 404
            req_404 = Request(
                f"{base}/api/directions/999999/generate-variants",
                data=json.dumps({"dry_run": True}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as err_ctx:
                urlopen(req_404, timeout=5)
            self.assertEqual(err_ctx.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
