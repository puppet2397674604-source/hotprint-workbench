"""Comprehensive tests for Stage 03 Data Preprocessing (30+ scenarios + stale-review check)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from backend.config import DEFAULT_DB_PATH
from backend.preprocessing import (
    PreprocessingService,
    PreprocessingValidationError,
    classify_product_text,
    derive_eligibility,
)
from backend.repository import SnapshotRepository
from backend.server import create_server
from tests.fixtures import SCHEMA_SQL, create_empty_schema_db


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def create_preprocessing_fixture_db(db_path: Path, *, mutate_solid_title: bool = False) -> Path:
    """Create isolated synthetic Sorftime SQLite covering all preprocessing edge cases."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        solid_title = (
            "Vintage Graphic Floral Print Sweatshirt"
            if mutate_solid_title
            else "Plain Solid Color Crewneck Sweatshirt"
        )
        women_products = [
            # 1. Cross-category overlap + explicit graphic -> possible_print / ready
            {
                "Asin": "B0PRNT0001",
                "ParentAsin": "B0PAR00001",
                "Title": "Vintage Graphic Sweatshirt Funny Cat Print",
                "ListingSalesVolumeOfMonth": 3200,
                "ListingSalesOfMonth": 7996800,
                "SalesPrice": 2499,
                "Ratings": 4.7,
                "RatingsCount": 180,
                "Brand": "CatStudio",
                "Photo": ["https://m.media-amazon.com/images/I/catprint1.jpg"],
                "OnlineDate": "2025-10-01",
                "Rank": 1800,
                "BsrCategory": [["Sweatshirts", "9056929011", "4"], ["Sweatshirts", "9056993011", "11"]],
            },
            # 2. Plain solid -> suspected_non_print / needs_review (NEVER auto-excluded!)
            {
                "Asin": "B0SOLID002",
                "ParentAsin": "B0SOLID002",  # ParentAsin = Asin!
                "Title": solid_title,
                "ListingSalesVolumeOfMonth": 1500,
                "ListingSalesOfMonth": 0,
                "SalesPrice": 0,  # price_zero
                "Ratings": 0.0,  # rating_missing
                "RatingsCount": 0,
                "Brand": "BasicCo",
                "Photo": ["https://m.media-amazon.com/images/I/solid1.jpg"],
                "OnlineDate": "2026-11-12",
                "Rank": 6200,
                "BsrCategory": [["Sweatshirts", "9056929011", "19"]],
            },
            # 3. Blank sweatshirt for printing -> suspected_non_print / needs_review
            {
                "Asin": "B0BLANK003",
                "ParentAsin": "B0PAR00003",
                "Title": "Blank Sweatshirt for Printing Heavyweight Crewneck",
                "ListingSalesVolumeOfMonth": 900,
                "ListingSalesOfMonth": 1799100,
                "SalesPrice": 1999,
                "Ratings": 4.4,
                "RatingsCount": 52,
                "Brand": "BlankMill",
                "Photo": [],  # image_missing (must not crash!)
                "OnlineDate": "2026-08-05",
                "Rank": 9100,
                "BsrCategory": [],  # bsr_missing
            },
            # 4. Striped pullover -> suspected_non_print / needs_review
            {
                "Asin": "B0STRIP004",
                "ParentAsin": "B0PAR00004",
                "Title": "Striped Pullover Sweatshirt Casual Long Sleeve",
                "ListingSalesVolumeOfMonth": 1100,
                "ListingSalesOfMonth": 2418900,
                "SalesPrice": 2199,
                "Ratings": 4.3,
                "RatingsCount": 40,
                "Brand": "StripeLine",
                "Photo": ["https://m.media-amazon.com/images/I/stripe1.jpg"],
                "OnlineDate": "bad-date-format",  # invalid date
                "Rank": 8400,
                "BsrCategory": [["Sweatshirts", "9056929011", "33"]],
            },
            # 5. Conflicting cues (Graphic + Plain) -> conflicting / needs_review
            {
                "Asin": "B0CNFL0005",
                "ParentAsin": "B0PAR00005",
                "Title": "Graphic Plain Sweatshirt Printed Blank Style",
                "ListingSalesVolumeOfMonth": 1300,
                "ListingSalesOfMonth": 2988700,
                "SalesPrice": 2299,
                "Ratings": 4.5,
                "RatingsCount": 90,
                "Brand": "MixBrand",
                "Photo": ["https://m.media-amazon.com/images/I/mix1.jpg"],
                "OnlineDate": "2026-06-01",
                "Rank": 7300,
                "BsrCategory": [["Sweatshirts", "9056929011", "25"]],
            },
            # 6. Uncertain (missing graphic keyword -> NOT suspected_non_print!)
            {
                "Asin": "B0UNCRT006",
                "ParentAsin": "B0PAR00006",
                "Title": "Women's Vintage Sweatshirt Soft Cozy Pullover",
                "ListingSalesVolumeOfMonth": 1400,
                "ListingSalesOfMonth": 3498600,
                "SalesPrice": 2499,
                "Ratings": 4.6,
                "RatingsCount": 120,
                "Brand": "CozyWear",
                "Photo": ["https://m.media-amazon.com/images/I/cozy1.jpg"],
                "OnlineDate": "2025-11-15",
                "Rank": 5100,
                "BsrCategory": [["Sweatshirts", "9056929011", "16"]],
            },
            # 7. Blocked item (missing title AND missing sales + BSR)
            {
                "Asin": "B0BLOCK007",
                "ParentAsin": "B0PAR00007",
                "Title": "",
                "ListingSalesVolumeOfMonth": -1,  # becomes NULL sales
                "ListingSalesOfMonth": -1,
                "SalesPrice": 0,
                "Ratings": 0.0,
                "RatingsCount": 0,
                "Brand": "",
                "Photo": [],
                "OnlineDate": "",
                "Rank": None,
                "BsrCategory": [],
            },
        ]

        # Men category repeats B0PRNT0001 (cross-category overlap!)
        men_products = [{**women_products[0]}]

        batches = [
            ("2026-11-19", "2026-11-19T02:16:03+00:00", 1, "9056929011", women_products),
            ("2026-11-19", "2026-11-19T02:16:06+00:00", 1, "9056993011", men_products),
        ]

        for obs_date, fetched_at, domain, node_id, items in batches:
            raw = {
                "Code": 0,
                "Message": "ok",
                "RequestConsumed": 5,
                "RequestLeft": 1300,
                "Data": {"Page": 1, "PageCount": 20, "Products": items},
            }
            cur = conn.execute(
                """INSERT INTO category_sales_batches
                   (observed_date, fetched_at, domain, node_id, product_count, request_consumed, raw_response)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (obs_date, fetched_at, domain, node_id, len(items), 5, json.dumps(raw)),
            )
            batch_id = cur.lastrowid
            for pos, p in enumerate(items, start=1):
                sales = p["ListingSalesVolumeOfMonth"]
                conn.execute(
                    """INSERT INTO category_sales_observations
                       (batch_id, result_position, asin, parent_asin, title, listing_sales_30d)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (batch_id, pos, p["Asin"], p["ParentAsin"], p["Title"], sales if sales >= 0 else None),
                )
        conn.commit()
    finally:
        conn.close()
    return db_path


class TestStage03Preprocessing(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        self.raw_db = create_preprocessing_fixture_db(self.tmp_dir / "raw.sqlite3")
        self.app_db = self.tmp_dir / "app.sqlite3"
        self.repo = SnapshotRepository(self.raw_db)
        self.svc = PreprocessingService(self.repo, app_db_path=self.app_db)

    def tearDown(self):
        self._tmp.cleanup()

    # --- 1..7: Automatic text classification rules ---
    def test_automatic_classification_rules_1_to_7(self):
        c1, _ = classify_product_text("Plain Solid Color Sweatshirt")
        self.assertEqual(c1, "suspected_non_print")

        c2, _ = classify_product_text("Blank Sweatshirt for Printing")
        self.assertEqual(c2, "suspected_non_print")

        c3, _ = classify_product_text("Striped Pullover Sweatshirt")
        self.assertEqual(c3, "suspected_non_print")

        c4, _ = classify_product_text("Vintage Graphic Sweatshirt")
        self.assertEqual(c4, "possible_print")

        c5, _ = classify_product_text("Women's Vintage Sweatshirt")
        self.assertEqual(c5, "uncertain")

        c6, _ = classify_product_text("Graphic Plain Sweatshirt")
        self.assertEqual(c6, "conflicting")

        # 7. Absence of 'graphic' keyword alone must NEVER produce suspected_non_print
        c7, _ = classify_product_text("Carhartt Men's Marquette Crewneck Sweatshirt")
        self.assertEqual(c7, "uncertain")
        self.assertNotEqual(c7, "suspected_non_print")

    # --- 8..15: Human confirmation, override, restore, re-run preservation & audit trail ---
    def test_human_confirmation_lifecycle_and_rerun_preservation_8_to_15(self):
        self.svc.run_preprocessing()

        # 8. suspected_non_print MUST NOT become excluded automatically
        solid = self.svc.get_product_detail("B0SOLID002")
        self.assertEqual(solid["automatic_classification"], "suspected_non_print")
        self.assertEqual(solid["manual_review_status"], "unreviewed")
        self.assertEqual(solid["eligibility"], "needs_review")

        # 9. Only after explicit confirm_exclude does it become excluded
        res_excl = self.svc.submit_manual_review(
            {
                "asin": "B0SOLID002",
                "action": "confirm_exclude",
                "reason_code": "solid_non_print",
                "reason_note": "主图确认纯色无印花",
                "request_id": "req-excl-1",
            }
        )
        self.assertEqual(res_excl["item"]["manual_review_status"], "confirmed_exclude")
        self.assertEqual(res_excl["item"]["eligibility"], "excluded")

        # 10 & 12. confirm_keep overrides automatic suspected_non_print -> ready
        res_keep = self.svc.submit_manual_review(
            {
                "asin": "B0STRIP004",
                "action": "confirm_keep",
                "reason_code": "confirmed_print_visual",
                "reason_note": "条纹底衫上有大幅印花字母",
                "request_id": "req-keep-1",
            }
        )
        self.assertEqual(res_keep["item"]["automatic_classification"], "suspected_non_print")
        self.assertEqual(res_keep["item"]["manual_review_status"], "confirmed_keep")
        self.assertEqual(res_keep["item"]["eligibility"], "ready")

        # 13 & 14. Re-running preprocessing preserves BOTH confirmed_exclude and confirmed_keep
        self.svc.run_preprocessing()
        solid_after = self.svc.get_product_detail("B0SOLID002")
        stripe_after = self.svc.get_product_detail("B0STRIP004")
        self.assertEqual(solid_after["manual_review_status"], "confirmed_exclude")
        self.assertEqual(solid_after["eligibility"], "excluded")
        self.assertEqual(stripe_after["manual_review_status"], "confirmed_keep")
        self.assertEqual(stripe_after["eligibility"], "ready")

        # 11. Restoring a confirmed_exclude suspected_non_print returns to needs_review (NOT ready!)
        res_restore = self.svc.submit_manual_review(
            {"asin": "B0SOLID002", "action": "restore", "request_id": "req-rest-1"}
        )
        self.assertEqual(res_restore["item"]["manual_review_status"], "unreviewed")
        self.assertEqual(res_restore["item"]["eligibility"], "needs_review")

        # 15. Full audit history is traceable
        hist = res_restore["item"]["review_history"]
        self.assertEqual([h["action"] for h in hist], ["restore", "confirm_exclude"])
        self.assertEqual(hist[1]["reason_code"], "solid_non_print")
        self.assertEqual(hist[1]["reason_note"], "主图确认纯色无印花")

    # --- 16..22: Data quality, dedup, non-zero-filling, BSR preservation, missing image resilience ---
    def test_data_quality_and_cross_category_dedup_16_to_22(self):
        self.svc.run_preprocessing()
        ov = self.svc.get_overview()
        # 8 observations (7 women + 1 overlapping men) -> 7 unique ASINs
        self.assertEqual(ov["total_observations"], 8)
        self.assertEqual(ov["counts"]["total"], 7)

        # 16, 17, 18. Missing rating != 0, price=0 flagged, ParentAsin=Asin preserved & flagged
        solid = self.svc.get_product_detail("B0SOLID002")
        self.assertIsNone(solid["rating_stars"])
        self.assertIn("rating_missing", solid["data_quality_issues"])
        self.assertIn("price_zero", solid["data_quality_issues"])
        self.assertIsNone(solid["price"])
        self.assertIn("parent_asin_same", solid["data_quality_issues"])
        self.assertFalse(solid["amazon_url_verified"])

        # 19, 20, 21. Cross-category ASIN appears once, sales NOT doubled, both BSRs preserved
        overlap = self.svc.get_product_detail("B0PRNT0001")
        self.assertTrue(overlap["is_cross_category"])
        self.assertEqual(overlap["listing_sales_30d"], 3200)
        self.assertEqual(overlap["category_bsr_by_source"]["9056929011"]["bsr"], 4)
        self.assertEqual(overlap["category_bsr_by_source"]["9056993011"]["bsr"], 11)

        # 22. Missing image does not crash processing and is flagged
        blank = self.svc.get_product_detail("B0BLANK003")
        self.assertIsNone(blank["image_url"])
        self.assertIn("image_missing", blank["data_quality_issues"])
        self.assertIn("bsr_missing", blank["data_quality_issues"])

    # --- 23..30: Persistence across restarts, idempotency, duplicate request guard, scoring contract & sum invariant ---
    def test_persistence_idempotency_and_scoring_contract_23_to_30(self):
        before_raw_hash = sha256_file(self.raw_db)
        self.svc.run_preprocessing()

        # Exclude B0SOLID002 and verify duplicate request_id produces no extra history row
        self.svc.submit_manual_review(
            {
                "asin": "B0SOLID002",
                "action": "confirm_exclude",
                "reason_code": "solid_non_print",
                "request_id": "dup-guard-001",
            }
        )
        dup_resp = self.svc.submit_manual_review(
            {
                "asin": "B0SOLID002",
                "action": "confirm_exclude",
                "reason_code": "solid_non_print",
                "request_id": "dup-guard-001",
            }
        )
        self.assertTrue(dup_resp["deduplicated"])
        self.assertEqual(len(dup_resp["item"]["review_history"]), 1)

        # 23 & 24. Simulate server restart (brand new PreprocessingService reading same app_db) + re-run
        svc_restarted = PreprocessingService(SnapshotRepository(self.raw_db), app_db_path=self.app_db)
        svc_restarted.run_preprocessing()
        reloaded = svc_restarted.get_product_detail("B0SOLID002")
        self.assertEqual(reloaded["manual_review_status"], "confirmed_exclude")
        self.assertEqual(reloaded["eligibility"], "excluded")

        # 29. Sum invariant: total == ready + needs_review + excluded + blocked
        ov = svc_restarted.get_overview()
        c = ov["counts"]
        self.assertEqual(c["total"], c["ready"] + c["needs_review"] + c["excluded"] + c["blocked"])
        # Expected: ready=2 (PRNT0001, UNCRT006), needs_review=3 (BLANK003, STRIP004, CNFL0005), excluded=1 (SOLID002), blocked=1 (BLOCK007)
        self.assertEqual(c, {"total": 7, "ready": 2, "needs_review": 3, "excluded": 1, "blocked": 1})

        # 26, 27, 28. Scoring candidates default to ready ONLY (excludes excluded, needs_review, blocked)
        scoring = svc_restarted.get_scoring_candidates()
        self.assertEqual(scoring["eligibility_filter"], "ready")
        self.assertFalse(scoring["scoring_formula_implemented"])
        scoring_asins = {item["asin"] for item in scoring["items"]}
        self.assertEqual(scoring_asins, {"B0PRNT0001", "B0UNCRT006"})
        self.assertNotIn("B0SOLID002", scoring_asins)
        self.assertNotIn("B0BLANK003", scoring_asins)
        self.assertNotIn("B0BLOCK007", scoring_asins)

        # 30. Raw SQLite DB untouched
        self.assertEqual(before_raw_hash, sha256_file(self.raw_db))

    # --- Stale human review alert when basis (title/image) changes in newer snapshot ---
    def test_stale_review_detected_when_title_changes(self):
        self.svc.run_preprocessing()
        self.svc.submit_manual_review(
            {
                "asin": "B0SOLID002",
                "action": "confirm_exclude",
                "reason_code": "solid_non_print",
                "request_id": "stale-test-1",
            }
        )
        self.assertEqual(self.svc.get_product_detail("B0SOLID002")["eligibility"], "excluded")

        # Now point to a new snapshot where B0SOLID002's title changed to a graphic print
        mutated_db = create_preprocessing_fixture_db(
            self.tmp_dir / "raw_mutated.sqlite3", mutate_solid_title=True
        )
        svc2 = PreprocessingService(SnapshotRepository(mutated_db), app_db_path=self.app_db)
        run2 = svc2.run_preprocessing()
        self.assertEqual(run2["stale_review_count"], 1)

        item = svc2.get_product_detail("B0SOLID002")
        self.assertTrue(item["review_stale"])
        self.assertEqual(item["eligibility"], "needs_review")
        # Historical manual review record must still be preserved!
        self.assertEqual(item["manual_review_status"], "confirmed_exclude")
        self.assertEqual(len(item["review_history"]), 1)

    # --- Empty & missing DB resilience + validation ---
    def test_empty_and_missing_db_and_server_validation(self):
        missing_svc = PreprocessingService(
            SnapshotRepository(self.tmp_dir / "missing.sqlite3"),
            app_db_path=self.tmp_dir / "app_missing.sqlite3",
        )
        res = missing_svc.run_preprocessing()
        self.assertEqual(res["status"], "empty")
        self.assertFalse(missing_svc.get_overview()["ready"])

        empty_db = create_empty_schema_db(self.tmp_dir / "empty.sqlite3")
        empty_svc = PreprocessingService(
            SnapshotRepository(empty_db),
            app_db_path=self.tmp_dir / "app_empty.sqlite3",
        )
        self.assertEqual(empty_svc.run_preprocessing()["status"], "empty")

        # Invalid action / ASIN rejected server-side
        with self.assertRaises(PreprocessingValidationError):
            self.svc.submit_manual_review({"asin": "BAD", "action": "confirm_exclude"})
        with self.assertRaises(PreprocessingValidationError):
            self.svc.submit_manual_review({"asin": "B0SOLID002", "action": "auto_delete"})

    # --- HTTP API integration for Stage 03 + Stage 04 scoring contract ---
    def test_http_preprocessing_and_scoring_api(self):
        srv, _ = create_server(host="127.0.0.1", port=0, db_path=self.raw_db, app_db_path=self.app_db)
        port = srv.server_address[1]
        base = f"http://127.0.0.1:{port}"
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            with urlopen(f"{base}/api/preprocessing/overview", timeout=5) as r:
                ov = json.loads(r.read().decode("utf-8"))
                self.assertTrue(ov["ready"])
                self.assertEqual(ov["counts"]["total"], 7)

            req = Request(
                f"{base}/api/preprocessing/reviews",
                data=json.dumps(
                    {
                        "asin": "B0SOLID002",
                        "action": "confirm_exclude",
                        "reason_code": "solid_non_print",
                        "request_id": "http-excl-1",
                    }
                ).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urlopen(req, timeout=5) as r:
                body = json.loads(r.read().decode("utf-8"))
                self.assertEqual(body["item"]["eligibility"], "excluded")

            with urlopen(f"{base}/api/scoring/candidates", timeout=5) as r:
                cand = json.loads(r.read().decode("utf-8"))
                self.assertEqual(cand["total"], 2)
                self.assertNotIn("B0SOLID002", [i["asin"] for i in cand["items"]])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_real_sorftime_db_remains_strictly_immutable(self):
        if not DEFAULT_DB_PATH.exists():
            self.skipTest("Real SQLite snapshot not present")
        before = sha256_file(DEFAULT_DB_PATH)
        svc = PreprocessingService(
            SnapshotRepository(DEFAULT_DB_PATH),
            app_db_path=self.tmp_dir / "real_prep_test.sqlite3",
        )
        run = svc.run_preprocessing()
        self.assertEqual(run["unique_asin_count"], 155)
        self.assertEqual(run["counts"]["excluded"], 0)
        self.assertEqual(
            run["counts"]["total"] if "total" in run["counts"] else 155,
            sum(run["counts"].values()),
        )
        self.assertEqual(before, sha256_file(DEFAULT_DB_PATH))

    def test_price_anomaly_flagged_without_auto_exclude(self):
        """0 < sales_price_usd < $5 gets price_anomaly flag; $0 stays price_zero; $5.0 is normal; never auto-excluded."""
        from backend.preprocessing import check_data_quality_issues, derive_eligibility

        base = {
            "asin": "B0PRIC0001",
            "parent_asin": "B0PARP0001",
            "title": "Funny Vintage Cat Graphic Tee",
            "main_image_url": "https://img.example/cat.jpg",
            "rating_status": "valid",
            "rating_stars": 4.6,
            "listing_sales_30d": 1500,
            "source_categories": [{"node_id": "9056923011", "source_category_bsr": 50}],
            "online_date": "2026-08-01",
        }
        # $1.00 -> price_anomaly (NOT price_zero, NOT blocked, NOT excluded)
        low_issues = check_data_quality_issues({**base, "sales_price_usd": 1.0, "price_valid": True})
        self.assertIn("price_anomaly", low_issues)
        self.assertNotIn("price_zero", low_issues)
        elig, _ = derive_eligibility("possible_print", low_issues, "unreviewed")
        self.assertEqual(elig, "ready")

        # $4.99 -> price_anomaly
        edge_low = check_data_quality_issues({**base, "sales_price_usd": 4.99, "price_valid": True})
        self.assertIn("price_anomaly", edge_low)

        # $5.00 -> normal (no price_anomaly)
        ok_issues = check_data_quality_issues({**base, "sales_price_usd": 5.0, "price_valid": True})
        self.assertNotIn("price_anomaly", ok_issues)

        # $0 -> price_zero (NOT price_anomaly)
        zero_issues = check_data_quality_issues({**base, "sales_price_usd": 0.0, "price_valid": False})
        self.assertIn("price_zero", zero_issues)
        self.assertNotIn("price_anomaly", zero_issues)


if __name__ == "__main__":
    unittest.main()
