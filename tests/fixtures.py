"""Synthetic SQLite fixtures for deterministic isolated testing."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS category_sales_batches (
    id INTEGER PRIMARY KEY,
    observed_date TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    domain INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    product_count INTEGER NOT NULL,
    request_consumed INTEGER,
    raw_response TEXT NOT NULL,
    UNIQUE (observed_date, domain, node_id)
);
CREATE TABLE IF NOT EXISTS category_sales_observations (
    batch_id INTEGER NOT NULL REFERENCES category_sales_batches(id),
    result_position INTEGER NOT NULL,
    asin TEXT NOT NULL,
    parent_asin TEXT,
    title TEXT,
    listing_sales_30d INTEGER CHECK (listing_sales_30d >= 0),
    PRIMARY KEY (batch_id, result_position)
);
CREATE VIEW IF NOT EXISTS category_sales_by_category AS
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
FROM observations AS o;
"""


def create_empty_schema_db(db_path: Path) -> Path:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()
    return db_path


def create_sample_db(db_path: Path) -> Path:
    """Create a synthetic DB with overlap ASIN, $0 price, missing rating, same parent ASIN, and missing BSR."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_SQL)

        women_products = [
            {
                "Asin": "B0OVERLAP1",
                "ParentAsin": "B0PARENT01",
                "Title": "Funny Cat Graphic Sweatshirt Cozy Pullover",
                "ListingSalesVolumeOfMonth": 3000,
                "ListingSalesOfMonth": 7497000,
                "SalesPrice": 2499,
                "Ratings": 4.6,
                "RatingsCount": 210,
                "Brand": "PrintCraft",
                "Photo": ["https://m.media-amazon.com/images/I/cat1.jpg", "https://m.media-amazon.com/images/I/cat2.jpg"],
                "EBCPhoto": ["https://m.media-amazon.com/images/S/ebc1.jpg"],
                "OnlineDate": "2025-10-01",
                "OnlineDays": 414,
                "Rank": 2100,
                "BsrCategory": [["Sweatshirts", "9056929011", "5"], ["Sweatshirts", "9056993011", "12"]],
                "IsFBA": True,
                "APlus": True,
                "HasVideo": False,
                "HasBrandStore": True,
                "VariationASINCount": 24,
            },
            {
                "Asin": "B0ZEROPRC1",
                "ParentAsin": "B0ZEROPRC1",  # ParentAsin = Asin!
                "Title": "Christmas Retro Vintage Tree Sweatshirt",
                "ListingSalesVolumeOfMonth": 1500,
                "ListingSalesOfMonth": 0,
                "SalesPrice": 0,  # Zero price anomaly!
                "Ratings": 0.0,  # Missing rating!
                "RatingsCount": 0,
                "Brand": "FestiveTee",
                "Photo": ["https://m.media-amazon.com/images/I/tree1.jpg"],
                "OnlineDate": "2026-11-12",
                "OnlineDays": 7,
                "Rank": 5400,
                "BsrCategory": [["Sweatshirts", "9056929011", "18"]],
                "IsFBA": False,
                "APlus": False,
                "VariationASINCount": 12,
            },
            {
                "Asin": "B0WOMEN003",
                "ParentAsin": "B0PARENT03",
                "Title": "Book Lover Coffee Graphic Crewneck Sweatshirt",
                "ListingSalesVolumeOfMonth": 800,
                "ListingSalesOfMonth": 1599200,
                "SalesPrice": 1999,
                "Ratings": 4.2,
                "RatingsCount": 48,
                "Brand": "PrintCraft",
                "Photo": [],  # Missing image!
                "OnlineDate": "2026-08-05",
                "OnlineDays": 106,
                "Rank": 9200,
                "BsrCategory": [],  # Missing source BSR!
            },
        ]

        men_products = [
            # Same ASIN as women_products[0] (cross-category overlap)
            {
                **women_products[0],
            },
            {
                "Asin": "B0MEN00002",
                "ParentAsin": "B0PARENT04",
                "Title": "Sarcastic Dad Joke Funny Graphic Sweatshirt",
                "ListingSalesVolumeOfMonth": 5200,
                "ListingSalesOfMonth": 16634800,
                "SalesPrice": 3199,
                "Ratings": 4.8,
                "RatingsCount": 640,
                "Brand": "DadHumor",
                "Photo": ["https://m.media-amazon.com/images/I/dad1.jpg"],
                "OnlineDate": "2024-09-10",
                "OnlineDays": 800,
                "Rank": 1200,
                "BsrCategory": [["Sweatshirts", "9056993011", "3"]],
            },
        ]

        batches = [
            ("2026-11-19", "2026-11-19T02:16:03+00:00", 1, "9056929011", women_products, 5, 1343, 28),
            ("2026-11-19", "2026-11-19T02:16:06+00:00", 1, "9056993011", men_products, 5, 1338, 30),
        ]

        for obs_date, fetched_at, domain, node_id, items, consumed, left, page_count in batches:
            raw = {
                "Code": 0,
                "Message": "ok",
                "RequestConsumed": consumed,
                "RequestLeft": left,
                "Data": {"Page": 1, "PageCount": page_count, "Products": items},
            }
            cur = conn.execute(
                """INSERT INTO category_sales_batches
                   (observed_date, fetched_at, domain, node_id, product_count, request_consumed, raw_response)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (obs_date, fetched_at, domain, node_id, len(items), consumed, json.dumps(raw)),
            )
            batch_id = cur.lastrowid
            for pos, p in enumerate(items, start=1):
                conn.execute(
                    """INSERT INTO category_sales_observations
                       (batch_id, result_position, asin, parent_asin, title, listing_sales_30d)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (batch_id, pos, p["Asin"], p["ParentAsin"], p["Title"], p["ListingSalesVolumeOfMonth"]),
                )
        conn.commit()
    finally:
        conn.close()
    return db_path
