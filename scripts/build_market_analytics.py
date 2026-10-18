from __future__ import annotations

from pathlib import Path
import sys

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from backend.services.market_analytics_pipeline import main as delegated_main


if __name__ == "__main__":
    raise SystemExit(delegated_main())

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from backend.services.product_catalog_classifier import classify_product, load_catalog_rows

SQLITE_PATH = BASE_DIR / "data" / "taobao_market.db"
COMPOSE_FILE = BASE_DIR / "docker-compose.yml"
ANALYTICS_DIR = BASE_DIR / "data" / "analytics"
MODEL_VERSION = "reserved"
UNKNOWN_SHOP = "未知店铺"
NUMBER_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建按商品小类细分的市场统计结果，并同步到 MySQL")
    parser.add_argument("--top-n", type=int, default=20, help="兼容保留参数，当前不会生成机器学习推荐结果")
    parser.add_argument("--mode", choices=("all", "latest-batch"), default="all", help="使用全部数据或仅使用最新批次数据")
    parser.add_argument("--batch-id", default="", help="指定批次号；指定后优先级高于 --mode")
    return parser.parse_args()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def resolve_snapshot_sql(conn: sqlite3.Connection, mode: str, batch_id: str) -> tuple[str, tuple[Any, ...]]:
    base_sql = """
        SELECT
            crawl_batch_id,
            crawl_time,
            keyword AS source_keyword,
            page_no,
            source_platform,
            shop,
            sku_name,
            coupon_price,
            original_price,
            source_url
        FROM taobao_pc_parts_snapshot
    """
    if batch_id:
        return base_sql + " WHERE crawl_batch_id = ? ORDER BY id", (batch_id,)

    if mode == "latest-batch":
        latest_batch = conn.execute(
            "SELECT crawl_batch_id FROM taobao_pc_parts_snapshot ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not latest_batch:
            return base_sql + " WHERE 1 = 0", ()
        return base_sql + " WHERE crawl_batch_id = ? ORDER BY id", (latest_batch[0],)

    return base_sql + " ORDER BY id", ()


def load_snapshot(mode: str, batch_id: str) -> pd.DataFrame:
    if not SQLITE_PATH.exists():
        raise FileNotFoundError(f"未找到 SQLite 数据库：{SQLITE_PATH}")

    with sqlite3.connect(SQLITE_PATH) as conn:
        sql, params = resolve_snapshot_sql(conn, mode, batch_id)
        return pd.read_sql_query(sql, conn, params=params)


def parse_price(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = NUMBER_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def prepare_snapshot(frame: pd.DataFrame) -> pd.DataFrame:
    df = frame.copy()
    df["crawl_time"] = pd.to_datetime(df["crawl_time"], errors="coerce")
    df["source_keyword"] = df["source_keyword"].fillna("").astype(str).str.strip()
    df["shop_match"] = df["shop"].fillna("").astype(str).str.strip()
    df["shop"] = df["shop_match"].replace("", UNKNOWN_SHOP)
    df["sku_name"] = df["sku_name"].fillna("").astype(str).str.replace(r"\s+", " ", regex=True).str.strip()
    df["source_url"] = df["source_url"].fillna("").astype(str).str.strip()
    df["coupon_price_num"] = df["coupon_price"].map(parse_price)
    df["original_price_num"] = df["original_price"].map(parse_price)
    df["effective_price"] = df["coupon_price_num"].where(df["coupon_price_num"].notna(), df["original_price_num"])
    df["baseline_price"] = df["original_price_num"].where(df["original_price_num"].notna(), df["effective_price"])
    df["discount_rate"] = np.where(
        (df["baseline_price"].notna()) & (df["baseline_price"] > 0) & (df["effective_price"].notna()),
        df["effective_price"] / df["baseline_price"],
        1.0,
    )
    df["discount_rate"] = pd.to_numeric(df["discount_rate"], errors="coerce").fillna(1.0).clip(lower=0.0, upper=1.5)

    df = df[df["source_keyword"] != ""].copy()
    df = df[df["sku_name"] != ""].copy()
    df = df[df["effective_price"].notna()].copy()
    df["effective_price"] = df["effective_price"].astype(float)
    df["baseline_price"] = df["baseline_price"].astype(float)

    taxonomy = df.apply(
        lambda row: classify_product(row["source_keyword"], row["sku_name"]),
        axis=1,
        result_type="expand",
    )
    df["keyword"] = taxonomy["canonical_keyword"]
    df["subtype_group"] = taxonomy["subtype_group"]
    df["product_subtype"] = taxonomy["product_subtype"]
    df["category_level1"] = taxonomy["category_level1"]
    df["category_level2"] = taxonomy["category_level2"]
    df["category_level3"] = taxonomy["category_level3"]
    df["category_level4"] = taxonomy["category_level4"]
    df["category_remark"] = taxonomy["category_remark"]
    return df


def build_category_catalog_frame() -> pd.DataFrame:
    rows = load_catalog_rows()
    return pd.DataFrame(rows, columns=["level1", "level2", "level3", "level4", "remark"])


def build_keyword_stats(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    subtype_count_map = df.groupby("keyword")["product_subtype"].nunique().to_dict()
    for keyword, group in df.groupby("keyword", sort=True):
        rows.append(
            {
                "keyword": keyword,
                "snapshot_count": int(len(group)),
                "shop_count": int(group["shop"].nunique()),
                "subtype_count": int(subtype_count_map.get(keyword, 0)),
                "min_effective_price": round(float(group["effective_price"].min()), 2),
                "max_effective_price": round(float(group["effective_price"].max()), 2),
                "avg_effective_price": round(float(group["effective_price"].mean()), 2),
                "avg_discount_rate": round(float(group["discount_rate"].mean()), 4),
                "latest_crawl_time": group["crawl_time"].max().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    return pd.DataFrame(rows)


def build_shop_stats(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    total_by_keyword = df.groupby("keyword")["sku_name"].count().to_dict()
    grouped = df.groupby(["keyword", "shop"], sort=True)
    for (keyword, shop), group in grouped:
        total = total_by_keyword[keyword]
        rows.append(
            {
                "keyword": keyword,
                "shop": shop,
                "product_count": int(len(group)),
                "market_share": round(float(len(group) / total), 4),
                "min_effective_price": round(float(group["effective_price"].min()), 2),
                "max_effective_price": round(float(group["effective_price"].max()), 2),
                "avg_effective_price": round(float(group["effective_price"].mean()), 2),
                "avg_discount_rate": round(float(group["discount_rate"].mean()), 4),
                "latest_crawl_time": group["crawl_time"].max().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    return pd.DataFrame(rows)


def build_subtype_stats(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    grouped = df.groupby(["keyword", "subtype_group", "product_subtype"], sort=True)
    for (keyword, subtype_group, product_subtype), group in grouped:
        rows.append(
            {
                "keyword": keyword,
                "subtype_group": subtype_group,
                "product_subtype": product_subtype,
                "snapshot_count": int(len(group)),
                "shop_count": int(group["shop"].nunique()),
                "min_effective_price": round(float(group["effective_price"].min()), 2),
                "max_effective_price": round(float(group["effective_price"].max()), 2),
                "avg_effective_price": round(float(group["effective_price"].mean()), 2),
                "avg_discount_rate": round(float(group["discount_rate"].mean()), 4),
                "latest_crawl_time": group["crawl_time"].max().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    return pd.DataFrame(rows)


def build_subtype_trend(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    grouped = df.groupby(["crawl_time", "keyword", "subtype_group", "product_subtype"], sort=True)
    for (crawl_time, keyword, subtype_group, product_subtype), group in grouped:
        rows.append(
            {
                "crawl_time": crawl_time.strftime("%Y-%m-%d %H:%M:%S"),
                "keyword": keyword,
                "subtype_group": subtype_group,
                "product_subtype": product_subtype,
                "snapshot_count": int(len(group)),
                "avg_effective_price": round(float(group["effective_price"].mean()), 2),
                "min_effective_price": round(float(group["effective_price"].min()), 2),
                "max_effective_price": round(float(group["effective_price"].max()), 2),
            }
        )
    return pd.DataFrame(rows)


def build_alerts(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    grouped = df.groupby(["keyword", "product_subtype"], sort=True)
    for (keyword, product_subtype), group in grouped:
        if len(group) < 3:
            continue
        prices = group["effective_price"].astype(float)
        avg_price = float(prices.mean())
        std_price = float(prices.std(ddof=0))
        if abs(std_price) <= 1e-9:
            continue

        upper = avg_price + std_price * 1.6
        lower = avg_price - std_price * 1.6
        outliers = group[(group["effective_price"] > upper) | (group["effective_price"] < lower)]
        for _, item in outliers.iterrows():
            delta_rate = abs(float(item["effective_price"]) - avg_price) / avg_price if avg_price else 0.0
            rows.append(
                {
                    "keyword": keyword,
                    "product_subtype": product_subtype,
                    "shop": item["shop"],
                    "sku_name": item["sku_name"],
                    "effective_price": round(float(item["effective_price"]), 2),
                    "avg_price": round(avg_price, 2),
                    "delta_rate": round(delta_rate, 4),
                    "level": "high" if delta_rate >= 0.45 else "medium",
                    "alert_type": "价格波动告警",
                    "rule_name": "按商品小类的均值与标准差规则检测",
                    "crawl_time": item["crawl_time"].strftime("%Y-%m-%d %H:%M:%S"),
                }
            )

    if not rows:
        return pd.DataFrame(
            columns=["keyword", "product_subtype", "shop", "sku_name", "effective_price", "avg_price", "delta_rate", "level", "alert_type", "rule_name", "crawl_time"]
        )
    return pd.DataFrame(rows).sort_values(by=["delta_rate", "crawl_time"], ascending=[False, False])


def build_recommendations(_: pd.DataFrame, __: int) -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "keyword",
            "rank_no",
            "cluster_id",
            "shop",
            "sku_name",
            "effective_price",
            "coupon_price",
            "original_price",
            "discount_rate",
            "text_score",
            "price_score",
            "discount_score",
            "recommendation_score",
            "recommended_reason",
            "source_url",
            "crawl_batch_id",
            "model_version",
            "generated_at",
        ]
    )


def export_debug_artifacts(
    keyword_stats: pd.DataFrame,
    shop_stats: pd.DataFrame,
    subtype_stats: pd.DataFrame,
    subtype_trend: pd.DataFrame,
    alerts: pd.DataFrame,
    recommendations: pd.DataFrame,
) -> None:
    ensure_dir(ANALYTICS_DIR)
    keyword_stats.to_csv(ANALYTICS_DIR / "keyword_stats.csv", index=False, encoding="utf-8-sig")
    shop_stats.to_csv(ANALYTICS_DIR / "shop_stats.csv", index=False, encoding="utf-8-sig")
    subtype_stats.to_csv(ANALYTICS_DIR / "subtype_stats.csv", index=False, encoding="utf-8-sig")
    subtype_trend.to_csv(ANALYTICS_DIR / "subtype_trend.csv", index=False, encoding="utf-8-sig")
    alerts.to_csv(ANALYTICS_DIR / "alerts.csv", index=False, encoding="utf-8-sig")
    recommendations.to_csv(ANALYTICS_DIR / "recommendations.csv", index=False, encoding="utf-8-sig")

    summary = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "keyword_count": int(keyword_stats.shape[0]),
        "subtype_count": int(subtype_stats.shape[0]),
        "alert_rows": int(alerts.shape[0]),
        "recommendation_rows": int(recommendations.shape[0]),
        "model_version": MODEL_VERSION,
    }
    (ANALYTICS_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def sql_literal(value: Any) -> str:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return "NULL"
    if isinstance(value, (int, np.integer)):
        return str(int(value))
    if isinstance(value, (float, np.floating)):
        return f"{float(value):.4f}".rstrip("0").rstrip(".")
    text = str(value).replace("\\", "\\\\").replace("'", "''")
    return f"'{text}'"


def build_insert_sql(table_name: str, columns: list[str], frame: pd.DataFrame, chunk_size: int = 200) -> list[str]:
    if frame.empty:
        return []

    statements: list[str] = []
    for start in range(0, len(frame), chunk_size):
        chunk = frame.iloc[start:start + chunk_size]
        values_sql = []
        for _, row in chunk.iterrows():
            values_sql.append(f"({', '.join(sql_literal(row[column]) for column in columns)})")
        statements.append(f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES\n" + ",\n".join(values_sql) + ";")
    return statements


def build_snapshot_update_sql(df: pd.DataFrame) -> list[str]:
    statements = [
        """
UPDATE pc_part_market_snapshot
SET
  canonical_keyword = NULL,
  subtype_group = NULL,
  product_subtype = NULL,
  category_level1 = NULL,
  category_level2 = NULL,
  category_level3 = NULL,
  category_level4 = NULL,
  category_remark = NULL;
""".strip()
    ]
    unique_rows = df[
        [
            "crawl_batch_id",
            "source_keyword",
            "page_no",
            "shop_match",
            "sku_name",
            "keyword",
            "subtype_group",
            "product_subtype",
            "category_level1",
            "category_level2",
            "category_level3",
            "category_level4",
            "category_remark",
        ]
    ].drop_duplicates()

    for _, row in unique_rows.iterrows():
        statements.append(
            f"""
UPDATE pc_part_market_snapshot
SET
  canonical_keyword = {sql_literal(row["keyword"])},
  subtype_group = {sql_literal(row["subtype_group"])},
  product_subtype = {sql_literal(row["product_subtype"])},
  category_level1 = {sql_literal(row["category_level1"])},
  category_level2 = {sql_literal(row["category_level2"])},
  category_level3 = {sql_literal(row["category_level3"])},
  category_level4 = {sql_literal(row["category_level4"])},
  category_remark = {sql_literal(row["category_remark"])}
WHERE
  crawl_batch_id = {sql_literal(row["crawl_batch_id"])}
  AND keyword = {sql_literal(row["source_keyword"])}
  AND page_no = {sql_literal(int(row["page_no"]))}
  AND COALESCE(shop, '') = {sql_literal(row["shop_match"])}
  AND sku_name = {sql_literal(row["sku_name"])};
""".strip()
        )
    return statements


def sync_to_mysql(
    prepared_snapshot: pd.DataFrame,
    category_catalog: pd.DataFrame,
    keyword_stats: pd.DataFrame,
    shop_stats: pd.DataFrame,
    subtype_stats: pd.DataFrame,
    subtype_trend: pd.DataFrame,
    alerts: pd.DataFrame,
    recommendations: pd.DataFrame,
) -> None:
    schema_sql = """
USE school_market;

SET @has_canonical_keyword := (
  SELECT COUNT(*)
  FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = 'school_market' AND TABLE_NAME = 'pc_part_market_snapshot' AND COLUMN_NAME = 'canonical_keyword'
);
SET @ddl := IF(@has_canonical_keyword = 0, 'ALTER TABLE pc_part_market_snapshot ADD COLUMN canonical_keyword VARCHAR(128) NULL', 'SELECT 1');
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @has_subtype_group := (
  SELECT COUNT(*)
  FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = 'school_market' AND TABLE_NAME = 'pc_part_market_snapshot' AND COLUMN_NAME = 'subtype_group'
);
SET @ddl := IF(@has_subtype_group = 0, 'ALTER TABLE pc_part_market_snapshot ADD COLUMN subtype_group VARCHAR(128) NULL', 'SELECT 1');
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @has_product_subtype := (
  SELECT COUNT(*)
  FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = 'school_market' AND TABLE_NAME = 'pc_part_market_snapshot' AND COLUMN_NAME = 'product_subtype'
);
SET @ddl := IF(@has_product_subtype = 0, 'ALTER TABLE pc_part_market_snapshot ADD COLUMN product_subtype VARCHAR(128) NULL', 'SELECT 1');
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @has_category_level1 := (
  SELECT COUNT(*)
  FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = 'school_market' AND TABLE_NAME = 'pc_part_market_snapshot' AND COLUMN_NAME = 'category_level1'
);
SET @ddl := IF(@has_category_level1 = 0, 'ALTER TABLE pc_part_market_snapshot ADD COLUMN category_level1 VARCHAR(128) NULL', 'SELECT 1');
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @has_category_level2 := (
  SELECT COUNT(*)
  FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = 'school_market' AND TABLE_NAME = 'pc_part_market_snapshot' AND COLUMN_NAME = 'category_level2'
);
SET @ddl := IF(@has_category_level2 = 0, 'ALTER TABLE pc_part_market_snapshot ADD COLUMN category_level2 VARCHAR(128) NULL', 'SELECT 1');
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @has_category_level3 := (
  SELECT COUNT(*)
  FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = 'school_market' AND TABLE_NAME = 'pc_part_market_snapshot' AND COLUMN_NAME = 'category_level3'
);
SET @ddl := IF(@has_category_level3 = 0, 'ALTER TABLE pc_part_market_snapshot ADD COLUMN category_level3 VARCHAR(128) NULL', 'SELECT 1');
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @has_category_level4 := (
  SELECT COUNT(*)
  FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = 'school_market' AND TABLE_NAME = 'pc_part_market_snapshot' AND COLUMN_NAME = 'category_level4'
);
SET @ddl := IF(@has_category_level4 = 0, 'ALTER TABLE pc_part_market_snapshot ADD COLUMN category_level4 VARCHAR(128) NULL', 'SELECT 1');
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @has_category_remark := (
  SELECT COUNT(*)
  FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = 'school_market' AND TABLE_NAME = 'pc_part_market_snapshot' AND COLUMN_NAME = 'category_remark'
);
SET @ddl := IF(@has_category_remark = 0, 'ALTER TABLE pc_part_market_snapshot ADD COLUMN category_remark VARCHAR(255) NULL', 'SELECT 1');
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

CREATE TABLE IF NOT EXISTS pc_part_market_category_catalog (
  id BIGINT NOT NULL AUTO_INCREMENT,
  level1 VARCHAR(128) NOT NULL,
  level2 VARCHAR(128) NOT NULL,
  level3 VARCHAR(128) NOT NULL,
  level4 VARCHAR(128) NOT NULL,
  remark VARCHAR(255) NULL,
  source_file VARCHAR(255) NOT NULL DEFAULT '电脑配件商品分类表.txt',
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_catalog_path (level1, level2, level3, level4)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS pc_part_market_keyword_stats (
  keyword VARCHAR(128) NOT NULL,
  snapshot_count INT NOT NULL,
  shop_count INT NOT NULL,
  subtype_count INT NOT NULL,
  min_effective_price DECIMAL(10, 2) NOT NULL,
  max_effective_price DECIMAL(10, 2) NOT NULL,
  avg_effective_price DECIMAL(10, 2) NOT NULL,
  avg_discount_rate DECIMAL(10, 4) NOT NULL,
  latest_crawl_time DATETIME NOT NULL,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (keyword)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

SET @has_keyword_subtype_count := (
  SELECT COUNT(*)
  FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = 'school_market' AND TABLE_NAME = 'pc_part_market_keyword_stats' AND COLUMN_NAME = 'subtype_count'
);
SET @ddl := IF(@has_keyword_subtype_count = 0, 'ALTER TABLE pc_part_market_keyword_stats ADD COLUMN subtype_count INT NOT NULL DEFAULT 0 AFTER shop_count', 'SELECT 1');
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

CREATE TABLE IF NOT EXISTS pc_part_market_shop_stats (
  id BIGINT NOT NULL AUTO_INCREMENT,
  keyword VARCHAR(128) NOT NULL,
  shop VARCHAR(255) NOT NULL,
  product_count INT NOT NULL,
  market_share DECIMAL(10, 4) NOT NULL,
  min_effective_price DECIMAL(10, 2) NOT NULL,
  max_effective_price DECIMAL(10, 2) NOT NULL,
  avg_effective_price DECIMAL(10, 2) NOT NULL,
  avg_discount_rate DECIMAL(10, 4) NOT NULL,
  latest_crawl_time DATETIME NOT NULL,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_keyword_shop (keyword, shop),
  KEY idx_keyword_product_count (keyword, product_count)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS pc_part_market_subtype_stats (
  id BIGINT NOT NULL AUTO_INCREMENT,
  keyword VARCHAR(128) NOT NULL,
  subtype_group VARCHAR(128) NOT NULL,
  product_subtype VARCHAR(128) NOT NULL,
  snapshot_count INT NOT NULL,
  shop_count INT NOT NULL,
  min_effective_price DECIMAL(10, 2) NOT NULL,
  max_effective_price DECIMAL(10, 2) NOT NULL,
  avg_effective_price DECIMAL(10, 2) NOT NULL,
  avg_discount_rate DECIMAL(10, 4) NOT NULL,
  latest_crawl_time DATETIME NOT NULL,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_keyword_subtype (keyword, product_subtype),
  KEY idx_keyword_snapshot_count (keyword, snapshot_count),
  KEY idx_keyword_avg_price (keyword, avg_effective_price)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS pc_part_market_subtype_trend (
  id BIGINT NOT NULL AUTO_INCREMENT,
  crawl_time DATETIME NOT NULL,
  keyword VARCHAR(128) NOT NULL,
  subtype_group VARCHAR(128) NOT NULL,
  product_subtype VARCHAR(128) NOT NULL,
  snapshot_count INT NOT NULL,
  avg_effective_price DECIMAL(10, 2) NOT NULL,
  min_effective_price DECIMAL(10, 2) NOT NULL,
  max_effective_price DECIMAL(10, 2) NOT NULL,
  PRIMARY KEY (id),
  KEY idx_keyword_crawl_time (keyword, crawl_time),
  KEY idx_keyword_subtype (keyword, product_subtype)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS pc_part_market_alerts (
  id BIGINT NOT NULL AUTO_INCREMENT,
  keyword VARCHAR(128) NOT NULL,
  product_subtype VARCHAR(128) NOT NULL,
  shop VARCHAR(255) NOT NULL,
  sku_name VARCHAR(512) NOT NULL,
  effective_price DECIMAL(10, 2) NOT NULL,
  avg_price DECIMAL(10, 2) NOT NULL,
  delta_rate DECIMAL(10, 4) NOT NULL,
  level VARCHAR(16) NOT NULL,
  alert_type VARCHAR(64) NOT NULL,
  rule_name VARCHAR(128) NOT NULL,
  crawl_time DATETIME NOT NULL,
  PRIMARY KEY (id),
  KEY idx_keyword_level (keyword, level),
  KEY idx_keyword_subtype (keyword, product_subtype)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS pc_part_market_recommendation (
  id BIGINT NOT NULL AUTO_INCREMENT,
  keyword VARCHAR(128) NOT NULL,
  rank_no INT NOT NULL,
  cluster_id INT NOT NULL,
  shop VARCHAR(255) NOT NULL,
  sku_name VARCHAR(512) NOT NULL,
  effective_price DECIMAL(10, 2) NOT NULL,
  coupon_price DECIMAL(10, 2) NULL,
  original_price DECIMAL(10, 2) NULL,
  discount_rate DECIMAL(10, 4) NOT NULL,
  text_score DECIMAL(10, 4) NOT NULL,
  price_score DECIMAL(10, 4) NOT NULL,
  discount_score DECIMAL(10, 4) NOT NULL,
  recommendation_score DECIMAL(10, 4) NOT NULL,
  recommended_reason VARCHAR(255) NOT NULL,
  source_url TEXT NULL,
  crawl_batch_id VARCHAR(32) NOT NULL,
  model_version VARCHAR(64) NOT NULL,
  generated_at DATETIME NOT NULL,
  PRIMARY KEY (id),
  KEY idx_keyword_rank (keyword, rank_no),
  KEY idx_keyword_score (keyword, recommendation_score),
  KEY idx_generated_at (generated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

TRUNCATE TABLE pc_part_market_keyword_stats;
TRUNCATE TABLE pc_part_market_shop_stats;
TRUNCATE TABLE pc_part_market_subtype_stats;
TRUNCATE TABLE pc_part_market_subtype_trend;
TRUNCATE TABLE pc_part_market_alerts;
TRUNCATE TABLE pc_part_market_recommendation;
TRUNCATE TABLE pc_part_market_category_catalog;
"""

    statements = [schema_sql]
    statements.extend(build_snapshot_update_sql(prepared_snapshot))
    statements.extend(
        build_insert_sql(
            "pc_part_market_category_catalog",
            ["level1", "level2", "level3", "level4", "remark"],
            category_catalog,
        )
    )
    statements.extend(
        build_insert_sql(
            "pc_part_market_keyword_stats",
            ["keyword", "snapshot_count", "shop_count", "subtype_count", "min_effective_price", "max_effective_price", "avg_effective_price", "avg_discount_rate", "latest_crawl_time"],
            keyword_stats,
        )
    )
    statements.extend(
        build_insert_sql(
            "pc_part_market_shop_stats",
            ["keyword", "shop", "product_count", "market_share", "min_effective_price", "max_effective_price", "avg_effective_price", "avg_discount_rate", "latest_crawl_time"],
            shop_stats,
        )
    )
    statements.extend(
        build_insert_sql(
            "pc_part_market_subtype_stats",
            ["keyword", "subtype_group", "product_subtype", "snapshot_count", "shop_count", "min_effective_price", "max_effective_price", "avg_effective_price", "avg_discount_rate", "latest_crawl_time"],
            subtype_stats,
        )
    )
    statements.extend(
        build_insert_sql(
            "pc_part_market_subtype_trend",
            ["crawl_time", "keyword", "subtype_group", "product_subtype", "snapshot_count", "avg_effective_price", "min_effective_price", "max_effective_price"],
            subtype_trend,
        )
    )
    statements.extend(
        build_insert_sql(
            "pc_part_market_alerts",
            ["keyword", "product_subtype", "shop", "sku_name", "effective_price", "avg_price", "delta_rate", "level", "alert_type", "rule_name", "crawl_time"],
            alerts,
        )
    )
    statements.extend(
        build_insert_sql(
            "pc_part_market_recommendation",
            ["keyword", "rank_no", "cluster_id", "shop", "sku_name", "effective_price", "coupon_price", "original_price", "discount_rate", "text_score", "price_score", "discount_score", "recommendation_score", "recommended_reason", "source_url", "crawl_batch_id", "model_version", "generated_at"],
            recommendations,
        )
    )

    sql_script = "\n".join(statements)
    command = [
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "exec",
        "-T",
        "mysql",
        "mysql",
        "-uroot",
        "-proot123",
        "--default-character-set=utf8mb4",
        "-D",
        "school_market",
    ]
    result = subprocess.run(command, input=sql_script, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(f"MySQL 同步失败：{result.stderr or result.stdout}")


def main() -> int:
    args = parse_args()
    raw_snapshot = load_snapshot(args.mode, args.batch_id)
    prepared_snapshot = prepare_snapshot(raw_snapshot)
    category_catalog = build_category_catalog_frame()
    if prepared_snapshot.empty:
        raise RuntimeError("没有可用于分析的数据")

    keyword_stats = build_keyword_stats(prepared_snapshot)
    shop_stats = build_shop_stats(prepared_snapshot)
    subtype_stats = build_subtype_stats(prepared_snapshot)
    subtype_trend = build_subtype_trend(prepared_snapshot)
    alerts = build_alerts(prepared_snapshot)
    recommendations = build_recommendations(prepared_snapshot, args.top_n)

    export_debug_artifacts(keyword_stats, shop_stats, subtype_stats, subtype_trend, alerts, recommendations)
    sync_to_mysql(prepared_snapshot, category_catalog, keyword_stats, shop_stats, subtype_stats, subtype_trend, alerts, recommendations)

    print(f"大类统计行数：{len(keyword_stats)}")
    print(f"小类统计行数：{len(subtype_stats)}")
    print(f"小类趋势行数：{len(subtype_trend)}")
    print(f"异常告警行数：{len(alerts)}")
    print(f"推荐结果行数：{len(recommendations)}")
    print(f"调试文件目录：{ANALYTICS_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
