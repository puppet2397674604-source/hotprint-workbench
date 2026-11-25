"""End-to-end HTTP API contract tests (live local socket on ephemeral port)."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from backend.server import create_server
from tests.fixtures import create_sample_db


class TestHttpApiContracts(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = create_sample_db(Path(self._tmp.name) / "sample.sqlite3")
        self.server, self.app = create_server(host="127.0.0.1", port=0, db_path=self.db_path)
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def _get(self, path: str):
        with urlopen(f"{self.base_url}{path}", timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def _post(self, path: str, body: dict):
        data = json.dumps(body).encode("utf-8")
        req = Request(
            f"{self.base_url}{path}",
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as err:
            return err.code, json.loads(err.read().decode("utf-8"))

    def test_health_overview_products_and_detail(self):
        code, health = self._get("/api/health")
        self.assertEqual(code, 200)
        self.assertTrue(health["ok"])
        self.assertEqual(health["db_status"]["state"], "ready")
        self.assertFalse(health["allow_live_collection"])

        code, overview = self._get("/api/overview?category=all")
        self.assertEqual(code, 200)
        self.assertTrue(overview["ready"])
        self.assertEqual(overview["kpis"]["unique_asin_count"], 4)
        self.assertEqual(overview["kpis"]["observation_count"], 5)

        code, products = self._get("/api/products?category=all&page=1&page_size=10")
        self.assertEqual(code, 200)
        self.assertEqual(products["total"], 4)

        code, detail = self._get("/api/products/B0OVERLAP1")
        self.assertEqual(code, 200)
        self.assertEqual(detail["item"]["asin"], "B0OVERLAP1")

    def test_collection_endpoints_and_guards(self):
        code, cfg = self._get("/api/collection/config")
        self.assertEqual(code, 200)
        self.assertEqual(len(cfg["categories"]), 6)
        self.assertEqual(cfg["category_summary"]["configured"], 6)
        self.assertEqual(cfg["category_summary"]["pending_config"], 0)
        self.assertFalse(cfg["schedule_placeholder"]["enabled"])

        code, batches = self._get("/api/collection/batches")
        self.assertEqual(code, 200)
        self.assertEqual(batches["total"], 2)

        # Plan + confirmed dry_run
        code, plan = self._post(
            "/api/collection/plan",
            {
                "categories": ["9056929011"],
                "observed_date": "2026-11-20",
                "mode": "dry_run",
            },
        )
        self.assertEqual(code, 200)
        token = plan["confirmation_token"]

        code, run_res = self._post(
            "/api/collection/run",
            {"confirmation_token": token, "confirmed": True},
        )
        self.assertEqual(code, 200)
        self.assertEqual(run_res["overall_status"], "completed")

        # Replay returns 409
        code, replay = self._post(
            "/api/collection/run",
            {"confirmation_token": token, "confirmed": True},
        )
        self.assertEqual(code, 409)
        self.assertEqual(replay["code"], "conflict_error")

        # Live mode returns 403
        code, live_plan = self._post(
            "/api/collection/plan",
            {
                "categories": ["9056929011"],
                "observed_date": "2026-11-20",
                "mode": "live",
            },
        )
        self.assertEqual(code, 200)
        code, live_run = self._post(
            "/api/collection/run",
            {"confirmation_token": live_plan["confirmation_token"], "confirmed": True},
        )
        self.assertEqual(code, 403)
        self.assertEqual(live_run["code"], "live_collection_blocked")

    def test_roadmap_and_static_index(self):
        code, roadmap = self._get("/api/roadmap")
        self.assertEqual(code, 200)
        self.assertEqual(len(roadmap["stages"]), 4)
        self.assertEqual(roadmap["input_pool_summary"]["unique_asin_count"], 4)

        with urlopen(f"{self.base_url}/", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            html = resp.read().decode("utf-8")
            self.assertIn("印选", html)

    def test_missing_database_api_response(self):
        missing_path = Path(self._tmp.name) / "nonexistent.sqlite3"
        srv, _ = create_server(host="127.0.0.1", port=0, db_path=missing_path)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            port = srv.server_address[1]
            with urlopen(f"http://127.0.0.1:{port}/api/overview", timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                self.assertFalse(data["ready"])
                self.assertEqual(data["db_status"]["state"], "missing")
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
