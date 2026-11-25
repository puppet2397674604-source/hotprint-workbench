"""Unit & HTTP API tests for `backend/supabase_state.py` (`select_*` cloud state sync & `pp_*` write guard)."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse

from backend.server import WorkbenchApp
from backend.supabase_state import (
    SELECT_TABLES_SPEC,
    SupabaseStateService,
    default_hp_http_requester,
    enforce_select_table_write_guard,
    load_select_migration_sql,
)


class TestSupabaseSelectStateSync(unittest.TestCase):
    def test_non_select_tables_hard_blocked_with_permission_error(self):
        """Red-line guard: POST/PATCH/PUT/DELETE on `pp_records` or any non-`select_` table MUST raise PermissionError."""
        forbidden_tables = (
            "pp_records",
            "pp_changes",
            "hp_manual_reviews",
            "users",
            "manual_reviews",
            "select_../pp_records",
            "select_records/extra",
            "",
        )
        for method in ("POST", "PATCH", "PUT", "DELETE"):
            for tbl in forbidden_tables:
                with self.assertRaises(PermissionError, msg=f"{method} on {tbl!r} must raise PermissionError"):
                    enforce_select_table_write_guard(method, tbl)

        # Also verify transport-level guard in default_hp_http_requester before any network socket opens
        for method in ("POST", "PATCH", "PUT", "DELETE"):
            for bad_tbl in ("pp_records", "pp_changes", "hp_manual_reviews"):
                with self.assertRaises(PermissionError):
                    default_hp_http_requester(
                        method,
                        f"https://oyckbvstitrmpvnvityb.supabase.co/rest/v1/{bad_tbl}",
                        {"apikey": "dummy"},
                        b"{}",
                    )

        # Also verify service-level `_request_hp_table` blocks writes to `pp_records` & `hp_*`
        with tempfile.TemporaryDirectory() as td:
            tp = Path(td)
            svc = SupabaseStateService(
                app_db_path=tp / "app.sqlite3",
                config_path=tp / "sb.local",
                http_requester=lambda *a, **kw: (200, [], {}),
            )
            (tp / "sb.local").write_text(
                json.dumps({"url": "https://oyckbvstitrmpvnvityb.supabase.co", "api_key": "sb_test_key_12345678"}),
                encoding="utf-8",
            )
            for method in ("POST", "PATCH", "PUT", "DELETE"):
                for bad_tbl in ("pp_records", "pp_changes", "hp_manual_reviews"):
                    with self.assertRaises(PermissionError):
                        svc._request_hp_table(method, bad_tbl, payload_obj={"x": 1})

    def test_migration_sql_contains_all_six_idempotent_select_tables(self):
        sql = load_select_migration_sql()
        expected_tables = (
            "select_manual_reviews",
            "select_product_ai_cache",
            "select_print_directions",
            "select_direction_asin_links",
            "select_direction_frozen_snapshots",
            "select_scoring_presets",
        )
        for tbl in expected_tables:
            self.assertIn(f"CREATE TABLE IF NOT EXISTS public.{tbl}", sql)
        self.assertEqual({spec["hp_table"] for spec in SELECT_TABLES_SPEC}, set(expected_tables))
        self.assertNotRegex(sql, r"\b(?:idx_)?hp_\w+")
        self.assertNotIn("pp_records (", sql)

    def test_select_status_uninitialized_pgrst205_returns_migration_sql(self):
        def missing_table_requester(method, url, headers, body_bytes=None, timeout=30.0):
            self.assertEqual(method, "GET")
            return (
                404,
                {"code": "PGRST205", "message": "Could not find the table 'public.select_manual_reviews' in the schema cache"},
                {},
            )

        with tempfile.TemporaryDirectory() as td:
            tp = Path(td)
            sb_cfg = tp / "sb.local"
            sb_cfg.write_text(
                json.dumps({"url": "https://oyckbvstitrmpvnvityb.supabase.co", "api_key": "sb_test_key_12345678"}),
                encoding="utf-8",
            )
            app = WorkbenchApp(
                db_path=tp / "live.sqlite3",
                app_db_path=tp / "app.sqlite3",
                supabase_config_path=sb_cfg,
                supabase_hp_requester=missing_table_requester,
            )
            code, resp = app.dispatch_api("GET", "/api/supabase/hp-status")
            self.assertEqual(code, 200)
            self.assertTrue(resp["configured"])
            self.assertFalse(resp["initialized"])
            self.assertEqual(len(resp["missing_tables"]), 6)
            self.assertIn("select_manual_reviews", resp["missing_tables"])
            self.assertIn("CREATE TABLE IF NOT EXISTS public.select_manual_reviews", resp["migration_sql"])

    def test_select_push_200_batch_upsert_and_pull_round_trip_merge(self):
        """Verify 200-row chunked Upsert push to `select_*` from Machine A and full merge pull into Machine B."""
        cloud_store = {spec["hp_table"]: {} for spec in SELECT_TABLES_SPEC}
        pk_cols_by_table = {
            spec["hp_table"]: [c.strip() for c in spec["on_conflict"].split(",")]
            for spec in SELECT_TABLES_SPEC
        }
        post_batches_by_table = {spec["hp_table"]: [] for spec in SELECT_TABLES_SPEC}

        def mock_cloud_rest(method, url, headers, body_bytes=None, timeout=30.0):
            parsed = urlparse(url)
            tbl = parsed.path.split("/rest/v1/", 1)[1].strip("/")
            qs = parse_qs(parsed.query)
            self.assertTrue(tbl.startswith("select_"), f"Expected select_* table, got {tbl!r}")
            enforce_select_table_write_guard(method, tbl)

            if method == "POST":
                self.assertIn("resolution=merge-duplicates", headers.get("Prefer", ""))
                rows = json.loads(body_bytes.decode("utf-8"))
                post_batches_by_table[tbl].append(len(rows))
                pk_cols = pk_cols_by_table[tbl]
                for r in rows:
                    pk_key = tuple(r[c] for c in pk_cols)
                    cloud_store[tbl][pk_key] = dict(r)
                return 201, [], {}

            if method == "GET":
                all_items = list(cloud_store[tbl].values())
                limit = int((qs.get("limit") or ["500"])[0])
                offset = int((qs.get("offset") or ["0"])[0])
                sliced = all_items[offset : offset + limit]
                resp_headers = {"content-range": f"0-{max(0, len(sliced)-1)}/{len(all_items)}" if all_items else "*/0"}
                return 200, sliced, resp_headers

            raise AssertionError(f"Unexpected method {method}")

        with tempfile.TemporaryDirectory() as td:
            tp = Path(td)
            sb_cfg = tp / "sb.local"
            sb_cfg.write_text(
                json.dumps({"url": "https://oyckbvstitrmpvnvityb.supabase.co", "api_key": "sb_test_key_12345678"}),
                encoding="utf-8",
            )

            # Machine A: populate 250 manual reviews (>200 -> 2 batches!), AI cache, direction, link, frozen snapshot, preset
            app_db_a = tp / "machine_a.sqlite3"
            app_a = WorkbenchApp(
                db_path=tp / "live_a.sqlite3",
                app_db_path=app_db_a,
                supabase_config_path=sb_cfg,
                supabase_hp_requester=mock_cloud_rest,
            )

            with sqlite3.connect(app_db_a) as conn:
                for i in range(250):
                    conn.execute(
                        """INSERT INTO manual_reviews
                           (domain, asin, review_status, reason_code, reason_note, operator_source, reviewed_at, basis_fingerprint, last_request_id)
                           VALUES (1, ?, ?, 'solid_non_print', ?, 'workbench', '2026-11-23T12:00:00Z', 'fp123', ?)""",
                        (
                            f"B0REV{i:05d}",
                            "confirmed_exclude" if i % 2 == 0 else "confirmed_keep",
                            f"审核备注 #{i}",
                            f"req_{i}",
                        ),
                    )
                conn.execute(
                    """INSERT INTO product_ai_cache
                       (asin, evidence_hash, model, prompt_version, is_graphic_print, pattern_type, confidence,
                        filter_reason, print_subject, slogan_text, visual_style, theme_tags_json,
                        season_tag, motif, transferability, raw_json, updated_at)
                       VALUES ('B0AICACHE1', 'ev_hash_1', 'deepseek-flash', 'v2', 1, 'graphic_print', 0.96,
                               '圣诞猫咪插画', '圣诞猫', 'MEOW XMAS', '复古手绘', '["cat", "christmas"]',
                               'christmas', 'cat', 'pure_graphic', '{"ok":true}', '2026-11-23T12:30:00Z')"""
                )
                conn.execute(
                    """INSERT INTO print_directions
                       (id, name, style_tags, target_audience, feasibility_rating, notes, status,
                        direction_type, source, variant_suggestions, created_at, updated_at)
                       VALUES (7, '复古圣诞猫咪梗', '["复古", "猫咪"]', '宠物人群', 5, '高潜方向', 'shortlisted',
                               'trend_cluster', 'manual', '["变体A", "变体B"]', '2026-11-23T10:00:00Z', '2026-11-23T13:00:00Z')"""
                )
                conn.execute(
                    """INSERT INTO direction_asin_links (direction_id, asin, added_at)
                       VALUES (7, 'B0AICACHE1', '2026-11-23T13:01:00Z')"""
                )
                conn.execute(
                    """INSERT INTO direction_frozen_snapshots
                       (direction_id, observed_date, direction_name, direction_score, season_tag, snapshot_json, frozen_at, updated_at)
                       VALUES (7, '2026-11-23', '复古圣诞猫咪梗', 88.5, 'christmas', '{"rank":1}', '2026-11-23T13:05:00Z', '2026-11-23T13:05:00Z')"""
                )
                conn.execute(
                    """INSERT INTO scoring_presets
                       (preset_id, name, is_default, weights_json, price_ranges_json, created_at, updated_at)
                       VALUES ('preset_team1', '团队高动销方案', 0,
                               '{"sales":20,"opportunity":40,"rating":15,"freshness":15,"price":10}',
                               '{"t_shirt":{"min":15,"max":26},"sweatshirt":{"min":26,"max":45}}',
                               '2026-11-23T11:00:00Z', '2026-11-23T13:10:00Z')"""
                )
                conn.commit()

            # 1) Push from Machine A via REST API
            code, push_res = app_a.dispatch_api("POST", "/api/supabase/hp-push", b"{}")
            self.assertEqual(code, 200)
            self.assertTrue(push_res["ok"])
            # 250 manual_reviews MUST be split into 2 batches: [200, 50]
            self.assertEqual(post_batches_by_table["select_manual_reviews"], [200, 50])
            self.assertEqual(len(cloud_store["select_manual_reviews"]), 250)
            self.assertEqual(len(cloud_store["select_product_ai_cache"]), 1)
            self.assertEqual(len(cloud_store["select_print_directions"]), 1)
            self.assertEqual(len(cloud_store["select_direction_asin_links"]), 1)
            self.assertEqual(len(cloud_store["select_direction_frozen_snapshots"]), 1)
            self.assertGreaterEqual(len(cloud_store["select_scoring_presets"]), 2)

            # 2) Check `GET /api/supabase/select-status` (and `/api/supabase/hp-status`)
            code, st_res = app_a.dispatch_api("GET", "/api/supabase/select-status")
            self.assertEqual(code, 200)
            self.assertTrue(st_res["initialized"])
            self.assertEqual(st_res["total_cloud_rows"], st_res["total_local_rows"])

            # 3) Machine B (brand new empty SQLite DB, plus 1 newer local review that must NOT be overwritten!)
            app_db_b = tp / "machine_b.sqlite3"
            app_b = WorkbenchApp(
                db_path=tp / "live_b.sqlite3",
                app_db_path=app_db_b,
                supabase_config_path=sb_cfg,
                supabase_hp_requester=mock_cloud_rest,
            )
            with sqlite3.connect(app_db_b) as conn:
                conn.execute(
                    """INSERT INTO manual_reviews
                       (domain, asin, review_status, reason_code, reason_note, operator_source, reviewed_at, basis_fingerprint, last_request_id)
                       VALUES (1, 'B0REV00000', 'confirmed_keep', 'confirmed_print_visual', 'B机更新结论', 'workbench', '2026-11-24T00:00:00Z', 'fp_new', 'req_new')"""
                )
                conn.commit()

            code, pull_res = app_b.dispatch_api(
                "POST",
                "/api/supabase/select-pull",
                json.dumps({"page_size": 120}).encode("utf-8"),
            )
            self.assertEqual(code, 200)
            self.assertTrue(pull_res["ok"])

            with sqlite3.connect(app_db_b) as conn:
                conn.row_factory = sqlite3.Row
                rev_cnt = conn.execute("SELECT COUNT(*) FROM manual_reviews").fetchone()[0]
                self.assertEqual(rev_cnt, 250)
                r0 = conn.execute("SELECT review_status, reason_note FROM manual_reviews WHERE asin = 'B0REV00000'").fetchone()
                self.assertEqual(r0["review_status"], "confirmed_keep")
                self.assertEqual(r0["reason_note"], "B机更新结论")

                ai_row = conn.execute("SELECT * FROM product_ai_cache WHERE asin = 'B0AICACHE1'").fetchone()
                self.assertIsNotNone(ai_row)
                self.assertEqual(ai_row["motif"], "cat")

                dir_row = conn.execute("SELECT * FROM print_directions WHERE id = 7").fetchone()
                self.assertIsNotNone(dir_row)
                self.assertEqual(dir_row["name"], "复古圣诞猫咪梗")

                link_cnt = conn.execute("SELECT COUNT(*) FROM direction_asin_links WHERE direction_id = 7").fetchone()[0]
                self.assertEqual(link_cnt, 1)

                frz_row = conn.execute("SELECT * FROM direction_frozen_snapshots WHERE direction_id = 7").fetchone()
                self.assertIsNotNone(frz_row)
                self.assertAlmostEqual(frz_row["direction_score"], 88.5, places=1)

                preset_row = conn.execute("SELECT * FROM scoring_presets WHERE preset_id = 'preset_team1'").fetchone()
                self.assertIsNotNone(preset_row)
                self.assertEqual(preset_row["name"], "团队高动销方案")


if __name__ == "__main__":
    unittest.main()
