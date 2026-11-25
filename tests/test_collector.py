"""Tests for manual collection validation, confirmation tokens, dedup skipping, dry-run/mock, and live lock."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen

from backend.collector import (
    CollectionConflictError,
    CollectionForbiddenError,
    CollectionService,
    CollectionValidationError,
    bootstrap_live_database,
)
from backend.repository import SnapshotRepository
from backend.server import create_server
from tests.fixtures import create_sample_db


class TestCollectionSafetyAndWorkflow(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = create_sample_db(Path(self._tmp.name) / "sample.sqlite3")
        self.fake_key_path = Path(self._tmp.name) / "api_key.local"
        self.fake_key_path.write_text("SECRET_NEVER_EXPOSE_9999\n", encoding="utf-8")
        self.wb_key_path = Path(self._tmp.name) / "wb_api_key.local"
        self.repo = SnapshotRepository(self.db_path)
        self.service = CollectionService(
            self.repo,
            allow_live_collection=False,
            api_key_path=self.fake_key_path,
            workbench_api_key_path=self.wb_key_path,
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_api_key_is_never_exposed(self):
        cfg = self.service.get_config_and_summary()
        serialized = json.dumps(cfg, ensure_ascii=False)
        self.assertNotIn("SECRET_NEVER_EXPOSE_9999", serialized)
        self.assertTrue(cfg["safety"]["api_key_configured"])
        self.assertFalse(cfg["safety"]["api_key_exposed_to_browser"])

    def test_same_day_dedup_skips_existing_batches(self):
        plan = self.service.create_plan(
            {
                "categories": ["9056929011", "9056993011"],
                "observed_date": "2026-11-19",
                "pages": 1,
                "range": 100,
                "budget_cap": 20,
                "mode": "dry_run",
            }
        )
        self.assertEqual(plan["skipped_categories"], 2)
        self.assertEqual(plan["pending_request_categories"], 0)
        self.assertEqual(plan["estimated_total_credits"], 0)

        res = self.service.execute_run(
            {"confirmation_token": plan["confirmation_token"], "confirmed": True}
        )
        self.assertEqual(res["overall_status"], "skipped_all")
        self.assertEqual(res["actual_credits_consumed"], 0)
        self.assertFalse(res["database_modified"])

    def test_confirmation_required_and_replay_blocked(self):
        plan = self.service.create_plan(
            {
                "categories": ["9056929011"],
                "observed_date": "2026-11-20",
                "pages": 1,
                "range": 100,
                "budget_cap": 10,
                "mode": "dry_run",
            }
        )
        # Reject without confirmed=True
        with self.assertRaises(CollectionValidationError):
            self.service.execute_run(
                {"confirmation_token": plan["confirmation_token"], "confirmed": False}
            )

        # First execution succeeds
        first = self.service.execute_run(
            {"confirmation_token": plan["confirmation_token"], "confirmed": True}
        )
        self.assertEqual(first["overall_status"], "completed")
        self.assertEqual(first["results"][0]["status"], "dry_run_ready")

        # Replay of the same token must fail with ConflictError (anti-double-click)
        with self.assertRaises(CollectionConflictError):
            self.service.execute_run(
                {"confirmation_token": plan["confirmation_token"], "confirmed": True}
            )

    def test_mock_partial_failure_and_retryable_list(self):
        plan = self.service.create_plan(
            {
                "categories": ["9056929011", "9056993011"],
                "observed_date": "2026-11-20",
                "pages": 1,
                "range": 100,
                "budget_cap": 20,
                "mode": "mock",
                "simulate_partial_failure": True,
            }
        )
        res = self.service.execute_run(
            {"confirmation_token": plan["confirmation_token"], "confirmed": True}
        )
        self.assertEqual(res["overall_status"], "partial_success")
        self.assertEqual(res["retryable_categories"], ["9056993011"])
        self.assertFalse(res["real_api_called"])
        self.assertFalse(res["database_modified"])

    def test_live_mode_hard_blocked_when_locked(self):
        plan = self.service.create_plan(
            {
                "categories": ["9056929011"],
                "observed_date": "2026-11-20",
                "pages": 1,
                "range": 100,
                "budget_cap": 10,
                "mode": "live",
            }
        )
        self.assertTrue(plan["live_blocked"])
        with self.assertRaises(CollectionForbiddenError):
            self.service.execute_run(
                {"confirmation_token": plan["confirmation_token"], "confirmed": True}
            )

    def test_server_side_parameter_and_budget_validation(self):
        # Exceeds budget cap
        with self.assertRaises(CollectionValidationError):
            self.service.create_plan(
                {
                    "categories": ["9056929011", "9056993011"],
                    "observed_date": "2026-11-20",
                    "pages": 2,  # 2 * 2 * 5 = 20
                    "range": 100,
                    "budget_cap": 10,
                    "mode": "dry_run",
                }
            )
        # Unknown category
        with self.assertRaises(CollectionValidationError):
            self.service.create_plan({"categories": ["9999999999"]})
        # Pages out of range
        with self.assertRaises(CollectionValidationError):
            self.service.create_plan({"categories": ["9056929011"], "pages": 99})

    def test_live_collection_end_to_end_with_mocked_http_and_immutable_source_db(self):
        source_sha_before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        live_db = bootstrap_live_database(self.db_path, Path(self._tmp.name) / "live.sqlite3")
        app_db = Path(self._tmp.name) / "prep.sqlite3"
        local_key_file = Path(self._tmp.name) / "local_api_key.local"

        api_calls = []

        def fake_sorftime(endpoint, params, domain, api_key):
            api_calls.append((endpoint, params, domain, api_key))
            nid = params["nodeId"]
            return {
                "Code": 0,
                "Message": "Success",
                "RequestConsumed": 5,
                "RequestLeft": 1333,
                "Data": {
                    "Page": 1,
                    "PageCount": 25,
                    "Products": [
                        {
                            "Asin": "B0LIVETEE1",
                            "ParentAsin": "B0PLIVETEE",
                            "Title": "Women Funny Retro Cat Graphic T-Shirt Vintage Tee",
                            "ListingSalesVolumeOfMonth": 3200,
                            "Ratings": 4.6,
                            "RatingsCount": 510,
                            "SalesPrice": 1999,
                            "ListingSalesOfMonth": 6396800,
                            "Brand": "LiveTeeCo",
                            "Photo": ["https://m.media-amazon.com/images/I/livetee1.jpg"],
                            "OnlineDate": "2025-07-12",
                            "Rank": 980,
                            "BsrCategory": [["Women's T-Shirts", nid, "3"]],
                        }
                    ],
                },
            }

        srv, _ = create_server(
            host="127.0.0.1",
            port=0,
            db_path=live_db,
            app_db_path=app_db,
            allow_live_collection=True,
            workbench_api_key_path=local_key_file,
            api_requester=fake_sorftime,
        )
        port = srv.server_address[1]
        base = f"http://127.0.0.1:{port}"
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()

        def post_json(path, body):
            req = Request(
                f"{base}{path}",
                data=json.dumps(body).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))

        try:
            # 1. Save API key locally & confirm raw key is NEVER leaked back
            code, key_res = post_json("/api/collection/api-key", {"api_key": "LIVE_TEST_SECRET_7788"})
            self.assertEqual(code, 200)
            self.assertTrue(key_res["api_key_configured"])
            self.assertEqual(key_res["api_key_masked"], "****7788")
            self.assertNotIn("LIVE_TEST_SECRET_7788", json.dumps(key_res))

            # 2. Plan live collection for Women's T-Shirts (1044544) on 2026-11-20
            code, plan = post_json(
                "/api/collection/plan",
                {
                    "categories": ["1044544"],
                    "observed_date": "2026-11-20",
                    "pages": 1,
                    "range": 100,
                    "budget_cap": 30,
                    "mode": "live",
                },
            )
            self.assertEqual(code, 200)
            self.assertFalse(plan["live_blocked"])
            self.assertEqual(plan["estimated_total_credits"], 5)

            # 3. Confirm & execute live collection -> writes to live_db
            code, run_res = post_json(
                "/api/collection/run",
                {"confirmation_token": plan["confirmation_token"], "confirmed": True},
            )
            self.assertEqual(code, 200)
            self.assertEqual(run_res["overall_status"], "completed")
            self.assertTrue(run_res["real_api_called"])
            self.assertTrue(run_res["database_modified"])
            self.assertEqual(run_res["actual_credits_consumed"], 5)
            self.assertEqual(run_res["latest_request_left"], 1333)
            self.assertEqual(len(api_calls), 1)
            self.assertEqual(api_calls[0][1]["nodeId"], "1044544")

            # 4. New T-Shirt snapshot immediately appears on Dashboard & Preprocessing!
            with urlopen(f"{base}/api/overview?date=2026-11-20&category=1044544", timeout=5) as r:
                ov = json.loads(r.read().decode("utf-8"))
                self.assertTrue(ov["ready"])
                self.assertEqual(ov["kpis"]["unique_asin_count"], 1)
                self.assertEqual(ov["hot_samples"][0]["asin"], "B0LIVETEE1")

            with urlopen(f"{base}/api/preprocessing/overview?date=2026-11-20", timeout=5) as r:
                prep_ov = json.loads(r.read().decode("utf-8"))
                self.assertEqual(prep_ov["counts"]["total"], 1)
                self.assertEqual(prep_ov["counts"]["ready"], 1)

            # 5. Re-planning same date & category skips with 0 credits and 0 API calls
            code, plan2 = post_json(
                "/api/collection/plan",
                {"categories": ["1044544"], "observed_date": "2026-11-20", "mode": "live"},
            )
            self.assertEqual(plan2["skipped_categories"], 1)
            self.assertEqual(plan2["estimated_total_credits"], 0)
            code, run2 = post_json(
                "/api/collection/run",
                {"confirmation_token": plan2["confirmation_token"], "confirmed": True},
            )
            self.assertEqual(run2["overall_status"], "skipped_all")
            self.assertEqual(len(api_calls), 1)  # No second network call!

            # 6. Clear local API key
            code, clr = post_json("/api/collection/api-key", {"action": "clear"})
            self.assertEqual(code, 200)
            self.assertFalse(clr["can_clear_local_key"])
        finally:
            srv.shutdown()
            srv.server_close()

        # Original source snapshot DB must remain 100% untouched
        self.assertEqual(hashlib.sha256(self.db_path.read_bytes()).hexdigest(), source_sha_before)


if __name__ == "__main__":
    unittest.main()
