"""Unit tests for Module 1: Read-Only Supabase (`pp_records`) Synchronizer."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse

from backend.server import WorkbenchApp
from backend.supabase_sync import (
    SupabaseRequestError,
    SupabaseSyncService,
    clean_bsr_category_name,
    clean_supabase_brand,
    default_supabase_get_requester,
    flatten_supabase_records,
    match_target_category,
    parse_date_first_available,
)


class TestSupabaseSyncUnit(unittest.TestCase):
    def test_read_only_guard_blocks_non_get_methods(self):
        """Strict red-line guard: any non-GET method to Supabase must raise PermissionError immediately."""
        for forbidden_method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.assertRaises(PermissionError):
                default_supabase_get_requester(
                    "https://oyckbvstitrmpvnvityb.supabase.co/rest/v1/pp_records",
                    {},
                    method=forbidden_method,
                )

    def test_date_first_available_and_brand_cleaning(self):
        """Verify Date First Available parsing & 'Visit the ... Store' brand stripping."""
        self.assertEqual(parse_date_first_available("March 13, 2026"), "2026-03-13")
        self.assertEqual(parse_date_first_available("Nov. 5, 2025"), "2025-11-05")
        self.assertEqual(parse_date_first_available("13 March 2026"), "2026-03-13")
        self.assertEqual(parse_date_first_available("2026-08-09"), "2026-08-09")
        self.assertIsNone(parse_date_first_available("Not a valid date"))
        self.assertIsNone(parse_date_first_available(None))
        self.assertIsNone(parse_date_first_available(""))

        self.assertEqual(
            clean_supabase_brand("Visit the Crazy Dog T-Shirts Store"),
            "Crazy Dog T-Shirts",
        )
        self.assertEqual(clean_supabase_brand("Visit the Gildan Store"), "Gildan")
        self.assertEqual(clean_supabase_brand("Brand: Comfort Colors"), "Comfort Colors")
        self.assertIsNone(clean_supabase_brand("   "))
        self.assertIsNone(clean_supabase_brand(None))

    def test_flatten_records_bsr_matching_null_price_and_parent_priority(self):
        """Verify flattening of snapshots + discoveries, BSR 6-category matching, and null price handling."""
        discoveries = [
            {
                "workspace_id": "default",
                "record_id": "disc_B0SUPA0001",
                "collection": "discoveries",
                "revision": 1,
                "payload": {
                    "asin": "B0SUPA0001",
                    "parentAsin": "B0PARDSC01",
                },
            }
        ]
        snapshots = [
            # 1) Multi-category hit (Men's + Women's Novelty Sweatshirts) + Clothing main BSR
            {
                "workspace_id": "default",
                "record_id": "snap_B0SUPA0001",
                "collection": "snapshots",
                "revision": 1,
                "payload": {
                    "asin": "B0SUPA0001",
                    "source": {"parentAsin": "B0PARSNP01"},
                    "title": "Funny Retro Cat Graphic Christmas Sweatshirt",
                    "brand": "Visit the MeowPrint Store",
                    "price": {"value": 29.99, "currency": "USD"},
                    "rating": 4.7,
                    "reviewCount": {"value": 42},
                    "mainImage": "https://m.media-amazon.com/images/I/71cat00001._AC_UL320_.jpg",
                    "images": [
                        "https://m.media-amazon.com/images/I/71cat00001._AC_UL320_.jpg",
                        "https://m.media-amazon.com/images/I/71cat00002._AC_UL320_.jpg",
                    ],
                    "url": "https://www.amazon.com/dp/B0SUPA0001",
                    "specs": {"Date First Available": "March 13, 2026"},
                    "bsr": [
                        {"rank": 1250, "category": "Clothing, Shoes & Jewelry"},
                        {"rank": 10, "category": "Men's Novelty Sweatshirts"},
                        {"rank": 25, "category": "Women's Novelty Sweatshirts"},
                    ],
                },
            },
            # 2) Single-category hit with price.value = null and fallback parentAsin from snapshots.source
            {
                "workspace_id": "default",
                "record_id": "snap_B0SUPA0002",
                "collection": "snapshots",
                "revision": 1,
                "payload": {
                    "asin": "B0SUPA0002",
                    "source": {"parentAsin": "B0PARSNP02"},
                    "title": "Vintage Sunset Dog Graphic Tee Shirt",
                    "brand": "Brand: PupVibes",
                    "price": {"value": None},
                    "rating": None,
                    "reviewCount": {"value": None},
                    "mainImage": "https://m.media-amazon.com/images/I/71dog00002._AC_UL320_.jpg",
                    "specs": {"Date First Available": "invalid-date"},
                    "bsr": [
                        {"rank": "#88", "category": "Women's Novelty T-Shirts"},
                    ],
                },
            },
            # 3) Unmatched category item (should be counted in unmatched_asin_count and excluded from 6 core pools)
            {
                "workspace_id": "default",
                "record_id": "snap_B0SUPA0003",
                "collection": "snapshots",
                "revision": 1,
                "payload": {
                    "asin": "B0SUPA0003",
                    "title": "Kitchen Coffee Mug",
                    "brand": "MugCo",
                    "price": {"value": 14.99},
                    "bsr": [
                        {"rank": 5, "category": "Novelty Coffee Mugs"},
                    ],
                },
            },
        ]

        res = flatten_supabase_records(snapshots, discoveries)
        summary = res["summary"]
        self.assertEqual(summary["snapshot_records_total"], 3)
        self.assertEqual(summary["discovery_records_total"], 1)
        self.assertEqual(summary["unique_snapshot_asins"], 3)
        self.assertEqual(summary["matched_asin_count"], 2)
        self.assertEqual(summary["unmatched_asin_count"], 1)
        # B0SUPA0001 hits 2 categories + B0SUPA0002 hits 1 category = 3 category observations
        self.assertEqual(summary["matched_observation_count"], 3)
        self.assertEqual(summary["matched_category_count"], 3)
        self.assertEqual(summary["missing_sales_count"], 2)
        self.assertEqual(summary["missing_sales_pct"], 100.0)
        self.assertEqual(summary["missing_price_count"], 1)
        self.assertEqual(summary["missing_price_pct"], 50.0)

        p1 = res["products_by_asin"]["B0SUPA0001"]
        # Discovery parentAsin takes priority over snapshot.source.parentAsin
        self.assertEqual(p1["parent_asin"], "B0PARDSC01")
        self.assertEqual(p1["brand"], "MeowPrint")
        self.assertEqual(p1["sales_price_usd"], 29.99)
        self.assertTrue(p1["price_valid"])
        self.assertEqual(p1["rating_stars"], 4.7)
        self.assertEqual(p1["ratings_count"], 42)
        self.assertEqual(p1["online_date"], "2026-03-13")
        self.assertEqual(p1["main_category_bsr"], 1250)
        self.assertIsNone(p1["listing_sales_30d"])
        self.assertIsNone(p1["listing_revenue_30d_usd"])

        p2 = res["products_by_asin"]["B0SUPA0002"]
        # Fallback to snapshot.source.parentAsin
        self.assertEqual(p2["parent_asin"], "B0PARSNP02")
        self.assertEqual(p2["brand"], "PupVibes")
        self.assertIsNone(p2["sales_price_usd"])
        self.assertFalse(p2["price_valid"])
        self.assertIsNone(p2["online_date"])
        self.assertEqual(p2["matched_categories"][0]["node_id"], "9056923011")
        self.assertEqual(p2["matched_categories"][0]["source_category_bsr"], 88)

    def test_mock_http_pagination_and_end_to_end_pipeline_sync(self):
        """Test Range header pagination, local DB ingestion, and full 02 -> 04 BSR scoring integration."""
        snapshots_dataset = [
            {
                "workspace_id": "default",
                "record_id": "snap_1",
                "collection": "snapshots",
                "revision": 1,
                "payload": {
                    "asin": "B0SBTOP010",
                    "title": "Funny Christmas Santa Cat Graphic Sweatshirt",
                    "brand": "Visit the CatFest Store",
                    "price": {"value": 31.99},
                    "rating": 4.8,
                    "reviewCount": {"value": 15},
                    "mainImage": "https://m.media-amazon.com/images/I/71sb010._AC_UL320_.jpg",
                    "specs": {"Date First Available": "October 1, 2026"},
                    "bsr": [
                        {"rank": 300, "category": "Clothing, Shoes & Jewelry"},
                        {"rank": 10, "category": "Men's Novelty Sweatshirts"},
                    ],
                },
            },
            {
                "workspace_id": "default",
                "record_id": "snap_2",
                "collection": "snapshots",
                "revision": 1,
                "payload": {
                    "asin": "B0SBMID050",
                    "title": "Retro Halloween Ghost Graphic Sweatshirt",
                    "brand": "Visit the SpookyWear Store",
                    "price": {"value": 33.50},
                    "rating": 4.5,
                    "reviewCount": {"value": 80},
                    "mainImage": "https://m.media-amazon.com/images/I/71sb050._AC_UL320_.jpg",
                    "specs": {"Date First Available": "September 15, 2026"},
                    "bsr": [
                        {"rank": 1200, "category": "Clothing, Shoes & Jewelry"},
                        {"rank": 50, "category": "Men's Novelty Sweatshirts"},
                    ],
                },
            },
            {
                "workspace_id": "default",
                "record_id": "snap_3",
                "collection": "snapshots",
                "revision": 1,
                "payload": {
                    "asin": "B0SBLOW200",
                    "title": "Vintage Dino Graphic Sweatshirt",
                    "brand": "DinoTees",
                    "price": {"value": None},
                    "rating": 4.2,
                    "reviewCount": {"value": 210},
                    "mainImage": "https://m.media-amazon.com/images/I/71sb200._AC_UL320_.jpg",
                    "specs": {"Date First Available": "March 13, 2026"},
                    "bsr": [
                        {"rank": 200, "category": "Men's Novelty Sweatshirts"},
                    ],
                },
            },
        ]
        discoveries_dataset = [
            {
                "workspace_id": "default",
                "record_id": "disc_10",
                "collection": "discoveries",
                "revision": 1,
                "payload": {"asin": "B0SBTOP010", "parentAsin": "B0PARSB010"},
            },
            {
                "workspace_id": "default",
                "record_id": "disc_11",
                "collection": "discoveries",
                "revision": 1,
                "payload": {"asin": "B0SBMID050", "parentAsin": "B0PARSB050"},
            },
        ]

        recorded_calls = []
        valid_pp_records_cols = {
            "workspace_id",
            "collection",
            "record_id",
            "payload",
            "revision",
            "updated_at",
        }

        def mock_requester(url: str, headers: dict, timeout: float = 30.0):
            recorded_calls.append((url, dict(headers)))
            # Keyset pagination MUST NOT send deep Range offset headers
            self.assertNotIn("Range", headers)
            parsed = urlparse(url)
            qs = parse_qs(parsed.query)
            # Strict schema check matching real Supabase pp_records (NO `id` column!)
            select_cols = [
                c.strip()
                for c in (qs.get("select") or [""])[0].split(",")
                if c.strip()
            ]
            for col in select_cols:
                if col not in valid_pp_records_cols:
                    raise AssertionError(f"column pp_records.{col} does not exist")
            order_param = (qs.get("order") or [""])[0]
            self.assertEqual(order_param, "record_id.asc")
            for k in qs:
                if k not in ("select", "order", "limit") and k not in valid_pp_records_cols:
                    raise AssertionError(f"Unexpected query filter column: {k}")

            limit_val = int((qs.get("limit") or ["150"])[0])
            if limit_val == 1 and "collection" not in qs:
                return 200, snapshots_dataset[:1], {}

            col_filter = (qs.get("collection") or [""])[0]
            source = sorted(
                snapshots_dataset if col_filter == "eq.snapshots" else discoveries_dataset,
                key=lambda r: r["record_id"],
            )
            rec_filter = (qs.get("record_id") or [""])[0]
            if rec_filter.startswith("gt."):
                cursor = rec_filter[3:]
                source = [r for r in source if r["record_id"] > cursor]
            slice_rows = source[:limit_val]
            return 200, slice_rows, {}

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            live_db = tmp_path / "category_sales_live.sqlite3"
            app_db = tmp_path / "preprocessing.sqlite3"
            sb_cfg = tmp_path / "supabase_config.local"

            # Force small page_size=2 to verify multi-page keyset pagination (record_id=gt.{cursor}&limit=2)
            sync_svc = SupabaseSyncService(
                live_db_path=live_db,
                config_path=sb_cfg,
                http_requester=mock_requester,
            )
            save_res = sync_svc.save_config(
                {
                    "url": "https://oyckbvstitrmpvnvityb.supabase.co",
                    "api_key": "sb_secret_test_key_1234567890",
                }
            )
            self.assertTrue(save_res["config"]["has_api_key"])
            self.assertNotIn("sb_secret_test_key_1234567890", json.dumps(save_res))

            # Keyset paginate with page_size=2 -> snapshots (3 items) + discoveries (2 items) = 4 GET calls
            dry_res = sync_svc.sync_to_local_db(
                observed_date="2026-11-23", dry_run=True, page_size=2
            )
            self.assertTrue(dry_res["dry_run"])
            self.assertEqual(dry_res["summary"]["matched_asin_count"], 3)
            self.assertFalse(live_db.exists())
            self.assertEqual(len(recorded_calls), 4)
            self.assertIn("limit=2", recorded_calls[0][0])
            self.assertNotIn("record_id=gt.", recorded_calls[0][0])
            self.assertIn("record_id=gt.snap_2&limit=2", recorded_calls[1][0])
            self.assertIn("limit=2", recorded_calls[2][0])
            self.assertIn("record_id=gt.disc_11&limit=2", recorded_calls[3][0])

            # Now test via WorkbenchApp REST endpoints end-to-end, with a strict AI tripwire!
            ai_call_counter = []

            def forbid_ai_call(*args, **kwargs):
                ai_call_counter.append((args, kwargs))
                raise AssertionError("DeepSeek / AIService MUST NEVER be invoked during Supabase sync!")

            app = WorkbenchApp(
                db_path=live_db,
                app_db_path=app_db,
                supabase_config_path=sb_cfg,
                supabase_requester=mock_requester,
                ai_requester=forbid_ai_call,
            )

            code, test_resp = app.dispatch_api(
                "POST",
                "/api/supabase/config",
                json.dumps({"action": "test"}).encode("utf-8"),
            )
            self.assertEqual(code, 200)
            self.assertTrue(test_resp["ok"])

            recorded_calls.clear()
            code, dry_api_resp = app.dispatch_api(
                "POST",
                "/api/supabase/sync",
                json.dumps({"observed_date": "2026-11-23", "dry_run": True}).encode("utf-8"),
            )
            self.assertEqual(code, 200)
            self.assertTrue(dry_api_resp["dry_run"])
            self.assertEqual(len(ai_call_counter), 0)
            # Verify production default step sizes: snapshots limit=150, discoveries limit=400
            self.assertEqual(len(recorded_calls), 2)
            self.assertIn("collection=eq.snapshots", recorded_calls[0][0])
            self.assertIn("limit=150", recorded_calls[0][0])
            self.assertIn("collection=eq.discoveries", recorded_calls[1][0])
            self.assertIn("limit=400", recorded_calls[1][0])

            code, sync_resp = app.dispatch_api(
                "POST",
                "/api/supabase/sync",
                json.dumps({"observed_date": "2026-11-23", "dry_run": False}).encode("utf-8"),
            )
            self.assertEqual(code, 200)
            self.assertFalse(sync_resp["dry_run"])
            self.assertEqual(sync_resp["written_batch_count"], 1)
            self.assertEqual(sync_resp["written_batches"][0]["credits_used"], 0)
            self.assertEqual(sync_resp["written_batches"][0]["source"], "supabase")
            self.assertEqual(len(ai_call_counter), 0)

            # Verify Stage 02 -> Stage 04 scoring ranks BSR #10 > BSR #50 > BSR #200 automatically!
            code, score_resp = app.dispatch_api(
                "GET", "/api/scoring/evaluate?date=2026-11-23"
            )
            self.assertEqual(code, 200)
            self.assertEqual(score_resp["summary"]["scored_count"], 3)
            self.assertEqual(score_resp["summary"]["unranked_no_sales_count"], 0)
            ranked_asins = [item["asin"] for item in score_resp["items"]]
            self.assertEqual(ranked_asins, ["B0SBTOP010", "B0SBMID050", "B0SBLOW200"])
            for item in score_resp["items"]:
                self.assertIsNone(item["listing_sales_30d"])
                self.assertEqual(item["demand_metric_source"], "bsr_reverse_percentile")

    def test_dirty_customer_reviews_category_suffix_cleaning_and_recovery(self):
        """Verify upstream 'Customer Reviews:' dirty suffixes on all 6 target categories are cleaned & recovered."""
        dirty_cases = [
            ("Women's Novelty Sweatshirts Customer Reviews:", "9056929011"),
            ("Women's T-Shirts Customer Reviews:  ", "1044544"),
            ("Men's Novelty T-Shirts customer reviews :", "9056987011"),
            ("Women's Novelty T-Shirts CUSTOMER REVIEWS:", "9056923011"),
            ("Men's T-Shirts Customer Reviews:", "1045624"),
            ("Men's Novelty Sweatshirts Customer Review: ", "9056993011"),
        ]
        for raw_cat, expected_node_id in dirty_cases:
            cleaned = clean_bsr_category_name(raw_cat)
            self.assertNotIn("customer review", cleaned.lower())
            matched = match_target_category(raw_cat)
            self.assertIsNotNone(matched, f"Failed to recover dirty category: {raw_cat!r}")
            self.assertEqual(matched.node_id, expected_node_id)

        self.assertEqual(
            clean_bsr_category_name("Clothing, Shoes & Jewelry Customer Reviews: "),
            "Clothing, Shoes & Jewelry",
        )

        # End-to-end flattening of dirty-suffixed snapshots
        snapshots = [
            {
                "record_id": "dirty_1",
                "collection": "snapshots",
                "payload": {
                    "asin": "B0DIRTYSW1",
                    "title": "Funny Vintage Cat Sweatshirt",
                    "brand": "MeowClub",
                    "price": {"value": 29.99},
                    "rating": 4.7,
                    "reviewCount": {"value": 42},
                    "bsr": [
                        {"rank": 1500, "category": "Clothing, Shoes & Jewelry Customer Reviews:"},
                        {"rank": 22, "category": "Women's Novelty Sweatshirts Customer Reviews:"},
                    ],
                },
            },
            {
                "record_id": "dirty_2",
                "collection": "snapshots",
                "payload": {
                    "asin": "B0DIRTYTS2",
                    "title": "Retro Sunset Tee",
                    "brand": "SunTees",
                    "price": {"value": 19.99},
                    "rating": 4.6,
                    "reviewCount": {"value": 18},
                    "bsr": [
                        {"rank": 85, "category": "Women's T-Shirts Customer Reviews:  "},
                    ],
                },
            },
        ]
        flat = flatten_supabase_records(snapshots, [])
        self.assertEqual(flat["summary"]["matched_asin_count"], 2)
        self.assertEqual(flat["summary"]["unmatched_asin_count"], 0)
        obs_by_node = flat["observations_by_node"]
        self.assertEqual(len(obs_by_node["9056929011"]), 1)
        self.assertEqual(obs_by_node["9056929011"][0]["main_category_bsr"], 1500)
        self.assertEqual(obs_by_node["9056929011"][0]["source_category_bsr"], 22)
        self.assertEqual(len(obs_by_node["1044544"]), 1)
        self.assertEqual(obs_by_node["1044544"][0]["source_category_bsr"], 85)

    def test_timeout_500_504_halves_limit_up_to_2_retries_and_completes_keyset_scan(self):
        """Verify HTTP 500/504 statement timeouts halve `limit` up to 2 times and finish keyset scan cleanly."""
        dataset = [
            {
                "record_id": f"snap_{i:04d}",
                "collection": "snapshots",
                "payload": {"asin": f"B0TMOUT{i:03d}", "title": f"Item {i}"},
            }
            for i in range(90)
        ]
        attempted_limits = []

        def flaky_timeout_requester(url: str, headers: dict, timeout: float = 30.0):
            qs = parse_qs(urlparse(url).query)
            lim = int(qs["limit"][0])
            attempted_limits.append(lim)
            if lim > 50:
                if lim == 150:
                    raise SupabaseRequestError(
                        "Supabase 接口返回 HTTP 500：canceling statement due to statement timeout",
                        status_code=500,
                        is_timeout=True,
                    )
                raise SupabaseRequestError(
                    "Supabase 接口返回 HTTP 504：Gateway Timeout",
                    status_code=504,
                    is_timeout=True,
                )
            rec_filter = (qs.get("record_id") or [""])[0]
            rows = dataset
            if rec_filter.startswith("gt."):
                cursor = rec_filter[3:]
                rows = [r for r in rows if r["record_id"] > cursor]
            return 200, rows[:lim], {}

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            svc = SupabaseSyncService(
                live_db_path=tmp_path / "live.sqlite3",
                config_path=tmp_path / "sb.local",
                http_requester=flaky_timeout_requester,
            )
            svc.save_config({"url": "https://oyckbvstitrmpvnvityb.supabase.co", "api_key": "sb_secret_test_key_1234567890"})

            rows = svc.fetch_collection_paginated("snapshots")
            # 150 -> timeout(500) -> 75 -> timeout(504) -> 37 (succeeds: 37 + 37 + 16 = 90 rows!)
            self.assertEqual(len(rows), 90)
            self.assertEqual([r["record_id"] for r in rows], [f"snap_{i:04d}" for i in range(90)])
            self.assertEqual(attempted_limits[:3], [150, 75, 37])

            # Verify exceeding MAX_TIMEOUT_RETRIES (2 retries = 3 total attempts) surfaces SupabaseRequestError
            always_timeout_limits = []

            def always_500_requester(url: str, headers: dict, timeout: float = 30.0):
                qs = parse_qs(urlparse(url).query)
                always_timeout_limits.append(int(qs["limit"][0]))
                raise SupabaseRequestError(
                    "Supabase 接口返回 HTTP 500：canceling statement due to statement timeout",
                    status_code=500,
                    is_timeout=True,
                )

            svc_fail = SupabaseSyncService(
                live_db_path=tmp_path / "live2.sqlite3",
                config_path=tmp_path / "sb.local",
                http_requester=always_500_requester,
            )
            with self.assertRaises(SupabaseRequestError):
                svc_fail.fetch_collection_paginated("snapshots")
            # Initial attempt (150) + 2 retries (75, 37) = 3 total calls, then raises!
            self.assertEqual(always_timeout_limits, [150, 75, 37])

    def test_complementary_multi_snapshot_merge_sellersprite_masked_and_image_fallback(self):
        """Verify multi-snapshot complementary merge, SellerSprite fields, masked guard, and images[0] rescue."""
        from backend.repository import SnapshotRepository

        discoveries = [
            {
                "record_id": "disc_1",
                "collection": "discoveries",
                "updated_at": "2026-11-23T09:00:00Z",
                "payload": {"asin": "B0MERG0001", "parentAsin": "B0PARDISC1"},
            }
        ]
        snapshots = [
            # 1A) SellerSprite list import for B0MERG0001 (older timestamp, has sales/revenue/launch_date/top-level parentAsin/seller.ranks, NO mainImage)
            {
                "record_id": "snap_ss_1",
                "collection": "snapshots",
                "updated_at": "2026-11-23T10:00:00Z",
                "payload": {
                    "asin": "B0MERG0001",
                    "capturedAt": "2026-11-23T10:00:00Z",
                    "parentAsin": "B0PARTOP01",
                    "title": "SellerSprite Old Title Cat Sweatshirt",
                    "brand": "MeowStudio",
                    "mainImage": None,
                    "images": [
                        {"url": "https://m.media-amazon.com/images/G/01/play-icon.svg"},
                        {"url": "https://m.media-amazon.com/images/I/71ss_backup.jpg"},
                    ],
                    "bsr": [],
                    "seller": {
                        "fields": {
                            "sales_parent_30d": {"value": 1680, "availability": "available"},
                            "sales_child_30d": {"value": 420, "availability": "available"},
                            "revenue": {"value": 42000.50, "availability": "available"},
                            "revenue_child": {"value": 10500.00, "availability": "available"},
                            "launch_date": {"value": "2026-09-20", "availability": "available"},
                        },
                        "ranks": [
                            {"rank": 15, "category": "Women's Novelty Sweatshirts"},
                        ],
                    },
                },
            },
            # 1B) Detail page scrape for B0MERG0001 (newer timestamp, has HD mainImage/price/rating/specs, NO sales)
            {
                "record_id": "snap_dp_1",
                "collection": "snapshots",
                "updated_at": "2026-11-23T14:00:00Z",
                "payload": {
                    "asin": "B0MERG0001",
                    "capturedAt": "2026-11-23T14:00:00Z",
                    "source": {"parentAsin": "B0PARSRC01"},
                    "title": "Latest Detail Title Retro Cat Christmas Sweatshirt",
                    "brand": "Visit the MeowStudio Store",
                    "price": {"value": 29.99},
                    "rating": 4.8,
                    "reviewCount": {"value": 128},
                    "mainImage": "https://m.media-amazon.com/images/I/81dp_hd_main.jpg",
                    "specs": {"Date First Available": "September 1, 2026"},
                    "bsr": [
                        {"rank": 850, "category": "Clothing, Shoes & Jewelry"},
                        {"rank": 11, "category": "Women's Novelty Sweatshirts"},
                    ],
                },
            },
            # 2) Product with NO mainImage -> rescued from images[] (skipping .svg & video cover!), plus masked parent sales falling back to available child sales
            {
                "record_id": "snap_rescue_2",
                "collection": "snapshots",
                "updated_at": "2026-11-23T12:00:00Z",
                "payload": {
                    "asin": "B0RESC0002",
                    "title": "Funny Dog Coffee Graphic Tee",
                    "brand": "PupBrew",
                    "price": {"value": 19.99},
                    "rating": 4.6,
                    "reviewCount": {"value": 35},
                    "mainImage": "",
                    "images": [
                        {"url": "https://m.media-amazon.com/images/G/01/ui/icon-360.svg"},
                        "https://m.media-amazon.com/images/I/51vid._PKmb-play-button-overlay-thumb_.jpg",
                        {"url": "https://m.media-amazon.com/images/I/61vid.jpg", "isVideo": True},
                        {"url": "https://m.media-amazon.com/images/I/71rescued_dog.jpg"},
                    ],
                    "seller": {
                        "fields": {
                            "sales_parent_30d": {"value": 9999, "availability": "masked"},
                            "sales_child_30d": {"value": 310, "availability": "available"},
                            "revenue": {"value": "***", "availability": "masked"},
                            "revenue_child": {"value": 6196.90, "availability": "available"},
                            "launch_date": {"value": "2026-10-05", "availability": "available"},
                        },
                        "ranks": [
                            {"rank": 42, "category": "Women's Novelty T-Shirts"},
                        ],
                    },
                },
            },
            # 3) Product where BOTH parent & child sales are masked -> listing_sales_30d MUST stay None!
            {
                "record_id": "snap_masked_3",
                "collection": "snapshots",
                "updated_at": "2026-11-23T11:00:00Z",
                "payload": {
                    "asin": "B0MASK0003",
                    "title": "Retro Ghost Graphic Tee",
                    "brand": "BooTees",
                    "mainImage": "https://m.media-amazon.com/images/I/71ghost03.jpg",
                    "seller": {
                        "fields": {
                            "sales_parent_30d": {"value": "500+", "availability": "masked"},
                            "sales_child_30d": {"value": None, "availability": "masked"},
                            "revenue": {"value": 12000, "availability": "masked"},
                        },
                        "ranks": [
                            {"rank": 77, "category": "Men's Novelty T-Shirts"},
                        ],
                    },
                },
            },
        ]

        flat = flatten_supabase_records(snapshots, discoveries)
        summary = flat["summary"]
        self.assertEqual(summary["matched_asin_count"], 3)
        self.assertEqual(summary["merged_multi_snapshot_asin_count"], 1)
        self.assertEqual(summary["valid_sales_count"], 2)
        self.assertEqual(summary["valid_sales_pct"], 66.7)
        self.assertEqual(summary["valid_image_count"], 3)
        self.assertEqual(summary["valid_image_pct"], 100.0)
        self.assertEqual(summary["valid_price_count"], 2)
        self.assertEqual(summary["valid_price_pct"], 66.7)

        # Verify 1: B0MERG0001 complementary merge
        m1 = flat["products_by_asin"]["B0MERG0001"]
        self.assertEqual(m1["title"], "Latest Detail Title Retro Cat Christmas Sweatshirt")  # newest title
        self.assertEqual(m1["main_image_url"], "https://m.media-amazon.com/images/I/81dp_hd_main.jpg")  # detail mainImage
        self.assertIn("https://m.media-amazon.com/images/I/71ss_backup.jpg", m1["photos"])  # merged gallery
        self.assertEqual(m1["listing_sales_30d"], 1680)  # SellerSprite parent sales
        self.assertEqual(m1["listing_revenue_30d_usd"], 42000.50)  # SellerSprite parent revenue
        self.assertEqual(m1["online_date"], "2026-09-20")  # launch_date beats specs Date First Available
        self.assertEqual(m1["parent_asin"], "B0PARTOP01")  # top-level parentAsin beats discoveries & source
        self.assertEqual(m1["sales_price_usd"], 29.99)
        self.assertEqual(m1["rating_stars"], 4.8)
        self.assertEqual(m1["ratings_count"], 128)
        self.assertEqual(m1["main_category_bsr"], 850)
        self.assertEqual(m1["matched_categories"][0]["source_category_bsr"], 11)  # newest BSR rank

        # Verify 2: B0RESC0002 rescued image from images[] & child sales fallback when parent masked
        m2 = flat["products_by_asin"]["B0RESC0002"]
        self.assertEqual(m2["main_image_url"], "https://m.media-amazon.com/images/I/71rescued_dog.jpg")
        self.assertEqual(m2["listing_sales_30d"], 310)
        self.assertEqual(m2["listing_revenue_30d_usd"], 6196.90)
        self.assertEqual(m2["online_date"], "2026-10-05")
        self.assertEqual(m2["matched_categories"][0]["node_id"], "9056923011")

        # Verify 3: B0MASK0003 all masked -> strictly None!
        m3 = flat["products_by_asin"]["B0MASK0003"]
        self.assertIsNone(m3["listing_sales_30d"])
        self.assertIsNone(m3["listing_revenue_30d_usd"])

        # Verify SQLite ingestion writes real sales & revenue cents accurately into category_sales_live.sqlite3
        def mock_req(url: str, headers: dict, timeout: float = 30.0):
            qs = parse_qs(urlparse(url).query)
            col = (qs.get("collection") or [""])[0]
            return 200, (snapshots if col == "eq.snapshots" else discoveries), {}

        with tempfile.TemporaryDirectory() as tmpdir:
            tp = Path(tmpdir)
            live_db = tp / "live.sqlite3"
            svc = SupabaseSyncService(
                live_db_path=live_db,
                config_path=tp / "sb.local",
                http_requester=mock_req,
            )
            svc.save_config({"url": "https://oyckbvstitrmpvnvityb.supabase.co", "api_key": "sb_secret_test_key_1234567890"})
            svc.sync_to_local_db(observed_date="2026-11-23", dry_run=False)

            repo = SnapshotRepository(live_db)
            scope = repo.get_scope_data(observed_date="2026-11-23", category="all", dedup_mode="asin")
            by_asin = {p["asin"]: p for p in scope["products"]}
            self.assertEqual(by_asin["B0MERG0001"]["listing_sales_30d"], 1680)
            self.assertAlmostEqual(by_asin["B0MERG0001"]["listing_revenue_30d_usd"], 42000.50, places=2)
            self.assertEqual(by_asin["B0RESC0002"]["listing_sales_30d"], 310)
            self.assertEqual(by_asin["B0RESC0002"]["main_image_url"], "https://m.media-amazon.com/images/I/71rescued_dog.jpg")
            self.assertIsNone(by_asin["B0MASK0003"]["listing_sales_30d"])


if __name__ == "__main__":
    unittest.main()
