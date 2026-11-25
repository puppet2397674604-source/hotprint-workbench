"""Unit tests for Stage 04 Data Credibility (P1): price_anomaly & per-category sales_suspect OLS."""

from __future__ import annotations

import math
import unittest

from backend.config import (
    PRICE_ANOMALY_THRESHOLD_USD,
    SALES_BSR_MIN_SAMPLE_SIZE,
    SALES_BSR_RESIDUAL_SIGMA,
)
from backend.preprocessing import detect_sales_bsr_anomalies
from backend.scoring import compute_candidate_scores, compute_confidence


class TestStage04CredibilityScoring(unittest.TestCase):
    def test_price_anomaly_treated_as_missing_and_penalizes_confidence_20(self):
        """$1.00 product: Price Fit excluded from numerator & denominator, confidence penalized -20."""
        self.assertEqual(PRICE_ANOMALY_THRESHOLD_USD, 5.0)
        candidates = [
            {
                "asin": "B0LOWP0001",
                "parent_asin": "B0PARL0001",
                "title": "Christmas Striped Letter Sweatshirt",
                "brand": "CheapBrand",
                "source_categories": [
                    {
                        "node_id": "9056929011",
                        "category_key": "womens_novelty_sweatshirts",
                        "garment_type": "sweatshirt",
                        "source_category_bsr": 100,
                    }
                ],
                "garment_types": ["sweatshirt"],
                "listing_sales_30d": 10000,
                "price": 1.0,
                "price_valid": True,
                "rating_stars": 4.6,
                "ratings_count": 200,
                "online_date": "2026-10-20",
            },
            {
                "asin": "B0NORM0001",
                "parent_asin": "B0PARN0001",
                "title": "Normal Price Christmas Sweatshirt",
                "brand": "NormalBrand",
                "source_categories": [
                    {
                        "node_id": "9056929011",
                        "category_key": "womens_novelty_sweatshirts",
                        "garment_type": "sweatshirt",
                        "source_category_bsr": 50,
                    }
                ],
                "garment_types": ["sweatshirt"],
                "listing_sales_30d": 10000,
                "price": 32.99,
                "price_valid": True,
                "rating_stars": 4.6,
                "ratings_count": 200,
                "online_date": "2026-10-20",
            },
        ]

        res = compute_candidate_scores(candidates, observed_date="2026-11-20")
        by_asin = {i["asin"]: i for i in res["items"]}

        low = by_asin["B0LOWP0001"]
        norm = by_asin["B0NORM0001"]

        # Price anomaly flagged on $1.00 item only
        self.assertTrue(low["price_anomaly"])
        self.assertFalse(norm["price_anomaly"])

        # V2: Price anomaly scores 0.0 (never free-rides via denominator exclusion) + x0.85 profit-floor penalty!
        self.assertNotIn("price", low["missing_dimensions"])
        self.assertFalse(low["dimensions"]["price"]["missing"])
        self.assertEqual(low["dimensions"]["price"]["score"], 0.0)
        self.assertLess(low["total_score"], norm["total_score"])
        # All 5 dimensions sum to 100% effective weight
        active_weight_sum = sum(d["effective_weight"] for d in low["dimensions"].values())
        self.assertAlmostEqual(active_weight_sum, 100.0, delta=0.3)

        # Confidence penalized -20 (all other fields complete -> 80)
        self.assertEqual(norm["confidence_score"], 100)
        self.assertEqual(low["confidence_score"], 80)
        penalty_codes = [p["code"] for p in low["confidence_penalties"]]
        self.assertIn("price_anomaly", penalty_codes)
        self.assertNotIn("price_invalid", penalty_codes)

        # Direct compute_confidence check with combined price_anomaly (-20) + sales_suspect (-15) = 65
        combo = compute_confidence(
            {
                "asin": "B0COMB0001",
                "parent_asin": "B0PARC0001",
                "price": 1.0,
                "price_valid": True,
                "rating_stars": 4.7,
                "ratings_count": 50,
                "online_date": "2026-10-01",
                "price_anomaly": True,
                "sales_suspect": True,
            }
        )
        self.assertEqual(combo["score"], 65)
        self.assertEqual(combo["level"], "medium")

    def test_sales_suspect_per_category_ols_35_samples_and_small_category_skip(self):
        """35-sample single category OLS flags poor-BSR/huge-sales outlier & deducts 15 confidence; <30 skips cleanly."""
        self.assertEqual(SALES_BSR_RESIDUAL_SIGMA, 2.0)
        self.assertEqual(SALES_BSR_MIN_SAMPLE_SIZE, 30)

        candidates = []
        # Category A (9056929011): 34 normal items following clean log-linear BSR-sales curve
        # ln(sales) = 10.5 - 0.55 * ln(bsr) + tiny deterministic jitter
        for i in range(1, 35):
            bsr = i * 10
            jitter = ((i % 3) - 1) * 0.04
            sales = int(round(math.exp(10.5 - 0.55 * math.log(bsr) + jitter)))
            candidates.append(
                {
                    "asin": f"B0CAT1{i:04d}",
                    "parent_asin": f"B0PAR1{i:04d}",
                    "title": f"Normal Graphic Sweatshirt #{i}",
                    "brand": f"Brand{i}",
                    "source_categories": [
                        {
                            "node_id": "9056929011",
                            "category_key": "womens_novelty_sweatshirts",
                            "short_name": "女趣卫衣",
                            "garment_type": "sweatshirt",
                            "source_category_bsr": bsr,
                        }
                    ],
                    "garment_types": ["sweatshirt"],
                    "listing_sales_30d": sales,
                    "price": 32.0,
                    "price_valid": True,
                    "rating_stars": 4.6,
                    "ratings_count": 120,
                    "online_date": "2026-08-01",
                }
            )

        # 35th sample in Category A: terrible BSR (#5000) yet absurdly huge sales (250,000)!
        candidates.append(
            {
                "asin": "B0OUTL0001",
                "parent_asin": "B0PARO0001",
                "title": "Suspect Huge Sales Poor BSR Sweatshirt",
                "brand": "OutlierBrand",
                "source_categories": [
                    {
                        "node_id": "9056929011",
                        "category_key": "womens_novelty_sweatshirts",
                        "short_name": "女趣卫衣",
                        "garment_type": "sweatshirt",
                        "source_category_bsr": 5000,
                    }
                ],
                "garment_types": ["sweatshirt"],
                "listing_sales_30d": 250000,
                "price": 32.0,
                "price_valid": True,
                "rating_stars": 4.6,
                "ratings_count": 120,
                "online_date": "2026-08-01",
            }
        )

        # Category B (1044544): only 12 samples (< 30) including an extreme outlier -> MUST safely skip!
        for j in range(1, 13):
            candidates.append(
                {
                    "asin": f"B0SML2{j:04d}",
                    "parent_asin": f"B0PRS2{j:04d}",
                    "title": f"Small Category Tee #{j}",
                    "brand": f"SmallBrand{j}",
                    "source_categories": [
                        {
                            "node_id": "1044544",
                            "category_key": "womens_tshirts",
                            "short_name": "女T恤",
                            "garment_type": "t_shirt",
                            "source_category_bsr": 9000 if j == 12 else j * 5,
                        }
                    ],
                    "garment_types": ["t_shirt"],
                    "listing_sales_30d": 500000 if j == 12 else 500,
                    "price": 20.0,
                    "price_valid": True,
                    "rating_stars": 4.6,
                    "ratings_count": 100,
                    "online_date": "2026-08-01",
                }
            )

        res = compute_candidate_scores(candidates, observed_date="2026-11-20")
        audit = res["sales_bsr_audit"]

        # Category A (35 samples) evaluated cleanly; Category B (12 samples) skipped with explicit reason
        self.assertEqual(audit["9056929011"]["status"], "evaluated")
        self.assertEqual(audit["9056929011"]["sample_size"], 35)
        self.assertEqual(audit["9056929011"]["suspect_count"], 1)

        self.assertEqual(audit["1044544"]["status"], "skipped")
        self.assertEqual(audit["1044544"]["sample_size"], 12)
        self.assertIn("< 30", audit["1044544"]["reason"])
        self.assertEqual(audit["1044544"]["suspect_count"], 0)

        by_asin = {i["asin"]: i for i in res["items"]}
        outlier = by_asin["B0OUTL0001"]
        normal = by_asin["B0CAT10005"]
        small_cat_outlier = by_asin["B0SML20012"]

        # Outlier hit accurately + confidence penalized -15 (100 -> 85)
        self.assertTrue(outlier["sales_suspect"])
        self.assertEqual(len(outlier["sales_suspect_details"]), 1)
        self.assertEqual(outlier["sales_suspect_details"][0]["node_id"], "9056929011")
        self.assertGreater(outlier["sales_suspect_details"][0]["z_score"], 2.0)
        self.assertEqual(outlier["confidence_score"], 85)
        self.assertIn("sales_suspect", [p["code"] for p in outlier["confidence_penalties"]])

        # Normal sample untouched (confidence 100)
        self.assertFalse(normal["sales_suspect"])
        self.assertEqual(normal["confidence_score"], 100)

        # Small-category item skipped cleanly (not flagged)
        self.assertFalse(small_cat_outlier["sales_suspect"])
        self.assertEqual(small_cat_outlier["confidence_score"], 100)

    def test_sales_suspect_multi_category_any_trigger_without_mixing_categories(self):
        """An ASIN spanning two categories is flagged if ANY single category flags it; categories never mix."""
        items = []
        # Cat 1 (9056929011): 31 items where BSR ~ sales_high (~3000 at BSR=20) with realistic scatter
        for i in range(1, 32):
            bsr = i * 5
            jitter = ((i % 5) - 2) * 0.08
            sales = int(round(math.exp(9.2 - 0.4 * math.log(bsr) + jitter)))
            items.append(
                {
                    "asin": f"B0M1A{i:05d}",
                    "listing_sales_30d": sales,
                    "source_categories": [{"node_id": "9056929011", "source_category_bsr": bsr}],
                }
            )
        # Cat 2 (9056993011): 31 items on a completely different scale (~50 sales at BSR=1000)
        for i in range(1, 32):
            bsr = i * 5
            jitter = ((i % 5) - 2) * 0.08
            sales = int(round(math.exp(6.0 - 0.35 * math.log(bsr) + jitter)))
            items.append(
                {
                    "asin": f"B0M2B{i:05d}",
                    "listing_sales_30d": sales,
                    "source_categories": [{"node_id": "9056993011", "source_category_bsr": bsr}],
                }
            )
        # Multi-category ASIN: normal in Cat 1 (BSR 20, sales 3000), huge outlier in Cat 2 (BSR 1000, sales 3000)
        items.append(
            {
                "asin": "B0SPAN0001",
                "listing_sales_30d": 3000,
                "source_categories": [
                    {"node_id": "9056929011", "source_category_bsr": 20},
                    {"node_id": "9056993011", "source_category_bsr": 1000},
                ],
            }
        )
        audit = detect_sales_bsr_anomalies(items)
        self.assertIn("B0SPAN0001", audit["suspect_map"])
        triggered_nodes = [d["node_id"] for d in audit["suspect_map"]["B0SPAN0001"]]
        self.assertEqual(triggered_nodes, ["9056993011"])

    def test_bsr_reverse_percentile_when_sales_missing_and_unranked_when_both_missing(self):
        """Module 2: Products with NULL sales but valid BSR enter ranking via within-category BSR reverse percentile."""
        candidates = [
            # BSR #10 in Men's Novelty Sweatshirts (best BSR in 3-item category -> reverse percentile 100.0)
            {
                "asin": "B0BSR00010",
                "parent_asin": "B0PARB0010",
                "title": "Top BSR #10 Graphic Sweatshirt",
                "brand": "AlphaBrand",
                "source_categories": [
                    {
                        "node_id": "9056993011",
                        "category_key": "mens_novelty_sweatshirts",
                        "short_name": "男趣卫衣",
                        "garment_type": "sweatshirt",
                        "source_category_bsr": 10,
                    }
                ],
                "garment_types": ["sweatshirt"],
                "listing_sales_30d": None,
                "price": 32.0,
                "price_valid": True,
                "rating_stars": 4.6,
                "ratings_count": 20,
                "online_date": "2026-10-15",
            },
            # BSR #50 in Men's Novelty Sweatshirts (middle BSR in 3-item category -> reverse percentile 50.0)
            {
                "asin": "B0BSR00050",
                "parent_asin": "B0PARB0050",
                "title": "Mid BSR #50 Graphic Sweatshirt",
                "brand": "BetaBrand",
                "source_categories": [
                    {
                        "node_id": "9056993011",
                        "category_key": "mens_novelty_sweatshirts",
                        "short_name": "男趣卫衣",
                        "garment_type": "sweatshirt",
                        "source_category_bsr": 50,
                    }
                ],
                "garment_types": ["sweatshirt"],
                "listing_sales_30d": None,
                "price": 32.0,
                "price_valid": True,
                "rating_stars": 4.6,
                "ratings_count": 20,
                "online_date": "2026-10-15",
            },
            # BSR #100 in Men's Novelty Sweatshirts (worst BSR in 3-item category -> reverse percentile 0.0)
            {
                "asin": "B0BSR00100",
                "parent_asin": "B0PARB0100",
                "title": "Low BSR #100 Graphic Sweatshirt",
                "brand": "GammaBrand",
                "source_categories": [
                    {
                        "node_id": "9056993011",
                        "category_key": "mens_novelty_sweatshirts",
                        "short_name": "男趣卫衣",
                        "garment_type": "sweatshirt",
                        "source_category_bsr": 100,
                    }
                ],
                "garment_types": ["sweatshirt"],
                "listing_sales_30d": None,
                "price": 32.0,
                "price_valid": True,
                "rating_stars": 4.6,
                "ratings_count": 20,
                "online_date": "2026-10-15",
            },
            # Single-sample category (Women's T-Shirts) -> reverse percentile defaults to 50.0
            {
                "asin": "B0SNGL0001",
                "parent_asin": "B0PARS0001",
                "title": "Single Sample Category Graphic Tee",
                "brand": "SoloBrand",
                "source_categories": [
                    {
                        "node_id": "1044544",
                        "category_key": "womens_tshirts",
                        "short_name": "女T恤",
                        "garment_type": "t_shirt",
                        "source_category_bsr": 500,
                    }
                ],
                "garment_types": ["t_shirt"],
                "listing_sales_30d": None,
                "price": 20.0,
                "price_valid": True,
                "rating_stars": 4.6,
                "ratings_count": 20,
                "online_date": "2026-10-15",
            },
            # Missing BOTH listing_sales_30d AND source_category_bsr -> MUST go to unranked_no_sales
            {
                "asin": "B0NONE0001",
                "parent_asin": "B0PARN0001",
                "title": "No Sales And No BSR Tee",
                "brand": "GhostBrand",
                "source_categories": [
                    {
                        "node_id": "1044544",
                        "category_key": "womens_tshirts",
                        "short_name": "女T恤",
                        "garment_type": "t_shirt",
                        "source_category_bsr": None,
                    }
                ],
                "garment_types": ["t_shirt"],
                "listing_sales_30d": None,
                "price": 20.0,
                "price_valid": True,
                "rating_stars": 4.6,
                "ratings_count": 20,
                "online_date": "2026-10-15",
            },
        ]

        res = compute_candidate_scores(candidates, observed_date="2026-11-20")
        self.assertEqual(res["summary"]["scored_count"], 4)
        self.assertEqual(res["summary"]["unranked_no_sales_count"], 1)
        self.assertEqual(res["unranked_no_sales"][0]["asin"], "B0NONE0001")

        by_asin = {i["asin"]: i for i in res["items"]}
        top = by_asin["B0BSR00010"]
        mid = by_asin["B0BSR00050"]
        low = by_asin["B0BSR00100"]
        solo = by_asin["B0SNGL0001"]

        self.assertEqual(top["demand_metric_source"], "bsr_reverse_percentile")
        self.assertEqual(top["dimensions"]["sales"]["score"], 100.0)
        self.assertEqual(mid["dimensions"]["sales"]["score"], 50.0)
        self.assertEqual(low["dimensions"]["sales"]["score"], 0.0)
        self.assertEqual(solo["dimensions"]["sales"]["score"], 50.0)
        self.assertIn("基于榜单排名，超越同类目 100.0% 的商品", top["dimensions"]["sales"]["detail"])
        self.assertIn("基于榜单排名，超越同类目 50.0% 的商品", mid["dimensions"]["sales"]["detail"])
        self.assertIn("基于榜单排名，超越同类目 0.0% 的商品", low["dimensions"]["sales"]["detail"])

        # Verify ranking order: BSR #10 > BSR #50 > BSR #100
        self.assertGreater(top["total_score"], mid["total_score"])
        self.assertGreater(mid["total_score"], low["total_score"])
        # Verify Freshness gate uses min(1.0, s_sales / 50.0): low has s_sales=0 -> freshness=0
        self.assertEqual(low["dimensions"]["freshness"]["score"], 0.0)
        self.assertGreater(top["dimensions"]["freshness"]["score"], 0.0)

    def test_incomplete_products_missing_image_or_3_plus_dims_moved_to_folded_section(self):
        """Verify items missing main_image_url or missing >= 3 dimensions are moved out of main leaderboard."""
        candidates = [
            # 1) Complete product -> stays in ranked items
            {
                "asin": "B0COMP0001",
                "parent_asin": "B0PARC0001",
                "title": "Complete Funny Cat Sweatshirt",
                "brand": "GoodBrand",
                "main_image_url": "https://m.media-amazon.com/images/I/71good01.jpg",
                "source_categories": [{"node_id": "9056929011", "garment_type": "sweatshirt", "source_category_bsr": 20}],
                "garment_types": ["sweatshirt"],
                "listing_sales_30d": 1200,
                "price": 31.99,
                "price_valid": True,
                "rating_stars": 4.7,
                "ratings_count": 85,
                "online_date": "2026-10-01",
            },
            # 2) Missing 2 dimensions only (freshness + price), has valid image -> stays in ranked items
            {
                "asin": "B0MISS2DIM",
                "parent_asin": "B0PARM2DIM",
                "title": "Two Missing Dims Sweatshirt",
                "brand": "OkayBrand",
                "main_image_url": "https://m.media-amazon.com/images/I/71okay02.jpg",
                "source_categories": [{"node_id": "9056929011", "garment_type": "sweatshirt", "source_category_bsr": 35}],
                "garment_types": ["sweatshirt"],
                "listing_sales_30d": 800,
                "price": None,
                "price_valid": False,
                "rating_stars": 4.6,
                "ratings_count": 40,
                "online_date": None,
            },
            # 3) Missing main_image_url (even with high sales!) -> moved to unranked_incomplete ("缺主图")
            {
                "asin": "B0NOIMG999",
                "parent_asin": "B0PARNOIMG",
                "title": "No Main Image Huge Sales Tee",
                "brand": "GhostImg",
                "main_image_url": "",
                "source_categories": [{"node_id": "1044544", "garment_type": "t_shirt", "source_category_bsr": 5}],
                "garment_types": ["t_shirt"],
                "listing_sales_30d": 9500,
                "price": 21.99,
                "price_valid": True,
                "rating_stars": 4.8,
                "ratings_count": 300,
                "online_date": "2026-10-10",
            },
            # 4) Has image, but missing 3 dimensions (opportunity, rating, price) -> moved to unranked_incomplete ("缺 3 维")
            {
                "asin": "B0MISS3DIM",
                "parent_asin": "B0PARM3DIM",
                "title": "Sparse 3 Missing Dims Tee",
                "brand": "SparseCo",
                "main_image_url": "https://m.media-amazon.com/images/I/71sparse.jpg",
                "source_categories": [{"node_id": "1044544", "garment_type": "t_shirt", "source_category_bsr": 12}],
                "garment_types": ["t_shirt"],
                "listing_sales_30d": None,
                "price": None,
                "price_valid": False,
                "rating_stars": None,
                "ratings_count": None,
                "online_date": "2026-11-10",
            },
            # 5) Missing BOTH main_image_url AND 4 dimensions -> moved to unranked_incomplete ("缺主图 / 缺 4 维")
            {
                "asin": "B0BOTH9999",
                "parent_asin": "B0PARBOTH9",
                "title": "No Image And 4 Missing Dims",
                "brand": "BareBrand",
                "main_image_url": None,
                "source_categories": [{"node_id": "1044544", "garment_type": "t_shirt", "source_category_bsr": 1}],
                "garment_types": ["t_shirt"],
                "listing_sales_30d": None,
                "price": None,
                "price_valid": False,
                "rating_stars": None,
                "ratings_count": None,
                "online_date": None,
            },
        ]

        res = compute_candidate_scores(candidates, observed_date="2026-11-20")
        ranked_asins = [i["asin"] for i in res["items"]]
        self.assertEqual(set(ranked_asins), {"B0COMP0001", "B0MISS2DIM"})
        self.assertEqual(res["summary"]["scored_count"], 2)
        self.assertEqual(res["summary"]["unranked_incomplete_count"], 3)

        inc_by_asin = {i["asin"]: i for i in res["unranked_incomplete"]}
        self.assertEqual(set(inc_by_asin.keys()), {"B0NOIMG999", "B0MISS3DIM", "B0BOTH9999"})
        self.assertEqual(inc_by_asin["B0NOIMG999"]["incomplete_tag"], "缺主图")
        self.assertEqual(inc_by_asin["B0MISS3DIM"]["incomplete_tag"], "缺 3 维")
        self.assertEqual(inc_by_asin["B0BOTH9999"]["incomplete_tag"], "缺主图 / 缺 4 维")

    def test_low_rating_penalty_severe_mild_good_and_unrated(self):
        """Verify 1.0★ gets ×0.55 severe penalty, 3.5★ gets ×0.85 mild penalty, 4.5★ & unrated get ×1.0."""
        candidates = [
            # 1) 1.0★ severe bad review item (even with huge sales!) -> ×0.55 severe penalty
            {
                "asin": "B0STAR1000",
                "parent_asin": "B0PARS1000",
                "title": "1-Star Bad Quality Graphic Sweatshirt",
                "brand": "BadBrand",
                "main_image_url": "https://m.media-amazon.com/images/I/71bad10.jpg",
                "source_categories": [{"node_id": "9056929011", "garment_type": "sweatshirt", "source_category_bsr": 5}],
                "garment_types": ["sweatshirt"],
                "listing_sales_30d": 5000,
                "price": 32.0,
                "price_valid": True,
                "rating_stars": 1.0,
                "ratings_count": 12,
                "online_date": "2026-10-20",
            },
            # 2) 3.5★ mild low rating item -> ×0.85 mild penalty
            {
                "asin": "B0STAR3500",
                "parent_asin": "B0PARS3500",
                "title": "3.5-Star Mediocre Graphic Sweatshirt",
                "brand": "MedBrand",
                "main_image_url": "https://m.media-amazon.com/images/I/71med35.jpg",
                "source_categories": [{"node_id": "9056929011", "garment_type": "sweatshirt", "source_category_bsr": 15}],
                "garment_types": ["sweatshirt"],
                "listing_sales_30d": 2000,
                "price": 32.0,
                "price_valid": True,
                "rating_stars": 3.5,
                "ratings_count": 25,
                "online_date": "2026-10-20",
            },
            # 3) 4.5★ healthy rating item -> ×1.0 (no penalty)
            {
                "asin": "B0STAR4500",
                "parent_asin": "B0PARS4500",
                "title": "4.5-Star Great Graphic Sweatshirt",
                "brand": "GoodBrand",
                "main_image_url": "https://m.media-amazon.com/images/I/71good45.jpg",
                "source_categories": [{"node_id": "9056929011", "garment_type": "sweatshirt", "source_category_bsr": 10}],
                "garment_types": ["sweatshirt"],
                "listing_sales_30d": 3500,
                "price": 32.0,
                "price_valid": True,
                "rating_stars": 4.5,
                "ratings_count": 60,
                "online_date": "2026-10-20",
            },
            # 4) Unrated new item (rating_stars=None, ratings_count=0) -> ×1.0 (no penalty)
            {
                "asin": "B0STARNONE",
                "parent_asin": "B0PARSNONE",
                "title": "Brand New Unrated Graphic Sweatshirt",
                "brand": "NewBrand",
                "main_image_url": "https://m.media-amazon.com/images/I/71new00.jpg",
                "source_categories": [{"node_id": "9056929011", "garment_type": "sweatshirt", "source_category_bsr": 25}],
                "garment_types": ["sweatshirt"],
                "listing_sales_30d": 1500,
                "price": 32.0,
                "price_valid": True,
                "rating_stars": None,
                "ratings_count": 0,
                "online_date": "2026-11-10",
            },
        ]

        res = compute_candidate_scores(candidates, observed_date="2026-11-20")
        by_asin = {i["asin"]: i for i in res["items"]}

        s10 = by_asin["B0STAR1000"]
        s35 = by_asin["B0STAR3500"]
        s45 = by_asin["B0STAR4500"]
        sno = by_asin["B0STARNONE"]

        self.assertEqual(s10["low_rating_penalty"], "severe")
        self.assertEqual(s10["low_rating_multiplier"], 0.55)
        self.assertEqual(s10["total_score"], round(s10["pre_penalty_score"] * 0.55, 2))

        self.assertEqual(s35["low_rating_penalty"], "mild")
        self.assertEqual(s35["low_rating_multiplier"], 0.85)
        self.assertEqual(s35["total_score"], round(s35["pre_penalty_score"] * 0.85, 2))

        self.assertIsNone(s45["low_rating_penalty"])
        self.assertEqual(s45["low_rating_multiplier"], 1.0)
        self.assertEqual(s45["total_score"], s45["pre_penalty_score"])

        self.assertIsNone(sno["low_rating_penalty"])
        self.assertEqual(sno["low_rating_multiplier"], 1.0)
        self.assertEqual(sno["total_score"], sno["pre_penalty_score"])

        # 4.5★ healthy product ranks strictly above 1.0★ severe bad review item despite lower raw sales!
        self.assertLess(s45["rank"], s10["rank"])


if __name__ == "__main__":
    unittest.main()
