from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from backend.services.market_catalog import classify_product, load_catalog_rows


BASE_DIR = Path(__file__).resolve().parents[2]
SQLITE_PATH = BASE_DIR / "data" / "taobao_market.db"
ANALYTICS_DIR = BASE_DIR / "data" / "analytics"
COMPOSE_FILE = BASE_DIR / "docker-compose.yml"
UNKNOWN_SHOP = "未知店铺"
NUMBER_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)")
ALERT_THRESHOLD_MEDIUM = 0.15
ALERT_THRESHOLD_HIGH = 0.3


@dataclass
class PipelineArtifacts:
    prepared_snapshot: pd.DataFrame
    clean_snapshot: pd.DataFrame
    category_catalog: pd.DataFrame
    keyword_stats: pd.DataFrame
    subtype_stats: pd.DataFrame
    subtype_trend: pd.DataFrame
    alerts: pd.DataFrame
    subtype_extremes: pd.DataFrame
    recommendations: pd.DataFrame


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建电脑配件市场分类、统计和告警分析结果")
    parser.add_argument("--mode", choices=("all", "latest-batch"), default="all")
    parser.add_argument("--batch-id", default="")
    parser.add_argument("--top-n", type=int, default=20)
    return parser.parse_args()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


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
        raise FileNotFoundError(f"未找到 SQLite 数据库: {SQLITE_PATH}")
    with sqlite3.connect(SQLITE_PATH) as conn:
        sql, params = resolve_snapshot_sql(conn, mode, batch_id)
        return pd.read_sql_query(sql, conn, params=params)


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
    df["discount_rate"] = 1.0
    valid_mask = (df["baseline_price"].notna()) & (df["baseline_price"] > 0) & (df["effective_price"].notna())
    df.loc[valid_mask, "discount_rate"] = df.loc[valid_mask, "effective_price"] / df.loc[valid_mask, "baseline_price"]
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
    df["is_dirty_data"] = taxonomy["is_dirty_data"].astype(int)
    df["dirty_reason"] = taxonomy["dirty_reason"]
    return df


def build_category_catalog_frame() -> pd.DataFrame:
    rows = load_catalog_rows()
    return pd.DataFrame(
        [
            {
                "level1": row.level1,
                "level2": row.level2,
                "level3": row.level3,
                "level4": row.level4,
                "remark": row.remark,
            }
            for row in rows
        ]
    )


def filter_clean_snapshot(df: pd.DataFrame) -> pd.DataFrame:
    return df[(df["is_dirty_data"] == 0) & (df["product_subtype"] != "")].copy()


def resolve_category_levels(group: pd.DataFrame) -> tuple[str, str, str, str]:
    values: list[str] = []
    for column in ["category_level1", "category_level2", "category_level3", "category_level4"]:
        series = group[column].fillna("").astype(str)
        series = series[series != ""]
        values.append(series.mode().iloc[0] if not series.empty else "")
    return values[0], values[1], values[2], values[3]


def build_keyword_stats(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for keyword, group in df.groupby("keyword", sort=True):
        rows.append(
            {
                "keyword": keyword,
                "snapshot_count": int(len(group)),
                "subtype_count": int(group["product_subtype"].nunique()),
                "min_effective_price": round(float(group["effective_price"].min()), 2),
                "max_effective_price": round(float(group["effective_price"].max()), 2),
                "avg_effective_price": round(float(group["effective_price"].mean()), 2),
                "latest_crawl_time": group["crawl_time"].max().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    return pd.DataFrame(rows)


def build_subtype_stats(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    grouped = df.groupby(["keyword", "subtype_group", "product_subtype"], sort=True)
    for keys, group in grouped:
        keyword, subtype_group, product_subtype = keys
        level1, level2, level3, level4 = resolve_category_levels(group)
        rows.append(
            {
                "keyword": keyword,
                "subtype_group": subtype_group,
                "product_subtype": product_subtype,
                "category_level1": level1,
                "category_level2": level2,
                "category_level3": level3,
                "category_level4": level4,
                "snapshot_count": int(len(group)),
                "min_effective_price": round(float(group["effective_price"].min()), 2),
                "max_effective_price": round(float(group["effective_price"].max()), 2),
                "avg_effective_price": round(float(group["effective_price"].mean()), 2),
                "latest_crawl_time": group["crawl_time"].max().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    return pd.DataFrame(rows)


def build_subtype_trend(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    grouped = df.groupby(["crawl_time", "keyword", "product_subtype"], sort=True)
    for keys, group in grouped:
        crawl_time, keyword, product_subtype = keys
        level1, level2, level3, level4 = resolve_category_levels(group)
        rows.append(
            {
                "crawl_time": crawl_time.strftime("%Y-%m-%d %H:%M:%S"),
                "keyword": keyword,
                "product_subtype": product_subtype,
                "category_level1": level1,
                "category_level2": level2,
                "category_level3": level3,
                "category_level4": level4,
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
    for keys, group in grouped:
        keyword, product_subtype = keys
        level1, level2, level3, level4 = resolve_category_levels(group)
        trend = (
            group.groupby("crawl_time", sort=True)["effective_price"]
            .mean()
            .reset_index()
            .sort_values("crawl_time")
        )
        if len(trend) < 2:
            continue

        latest = trend.iloc[-1]
        previous = trend.iloc[-2]
        previous_avg = float(previous["effective_price"])
        latest_avg = float(latest["effective_price"])
        if previous_avg <= 0:
            continue

        delta_rate = abs(latest_avg - previous_avg) / previous_avg
        if delta_rate < ALERT_THRESHOLD_MEDIUM:
            continue

        latest_group = group[group["crawl_time"] == latest["crawl_time"]]
        level = "high" if delta_rate >= ALERT_THRESHOLD_HIGH else "medium"
        rows.append(
            {
                "keyword": keyword,
                "product_subtype": product_subtype,
                "category_level1": level1,
                "category_level2": level2,
                "category_level3": level3,
                "category_level4": level4,
                "previous_avg_price": round(previous_avg, 2),
                "latest_avg_price": round(latest_avg, 2),
                "delta_amount": round(latest_avg - previous_avg, 2),
                "delta_rate": round(delta_rate, 4),
                "current_min_price": round(float(latest_group["effective_price"].min()), 2),
                "current_max_price": round(float(latest_group["effective_price"].max()), 2),
                "previous_crawl_time": previous["crawl_time"].strftime("%Y-%m-%d %H:%M:%S"),
                "latest_crawl_time": latest["crawl_time"].strftime("%Y-%m-%d %H:%M:%S"),
                "sample_count": int(len(latest_group)),
                "level": level,
                "alert_type": "小类均值波动告警",
                "rule_name": "按相邻采集时点的小类均值波动阈值检测",
            }
        )

    columns = [
        "keyword",
        "product_subtype",
        "category_level1",
        "category_level2",
        "category_level3",
        "category_level4",
        "previous_avg_price",
        "latest_avg_price",
        "delta_amount",
        "delta_rate",
        "current_min_price",
        "current_max_price",
        "previous_crawl_time",
        "latest_crawl_time",
        "sample_count",
        "level",
        "alert_type",
        "rule_name",
    ]
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows).sort_values(by=["delta_rate", "latest_crawl_time"], ascending=[False, False])


def build_snapshot_extremes(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    grouped = df.groupby(["keyword", "product_subtype"], sort=True)
    for keys, group in grouped:
        keyword, product_subtype = keys
        level1, level2, level3, level4 = resolve_category_levels(group)
        latest_time = group["crawl_time"].max()
        latest_group = group[group["crawl_time"] == latest_time].copy()
        low_row = latest_group.sort_values(["effective_price", "sku_name"]).iloc[0]
        high_row = latest_group.sort_values(["effective_price", "sku_name"], ascending=[False, True]).iloc[0]
        rows.append(
            {
                "keyword": keyword,
                "product_subtype": product_subtype,
                "category_level1": level1,
                "category_level2": level2,
                "category_level3": level3,
                "category_level4": level4,
                "low_shop": low_row["shop"],
                "low_sku_name": low_row["sku_name"],
                "low_price": round(float(low_row["effective_price"]), 2),
                "high_shop": high_row["shop"],
                "high_sku_name": high_row["sku_name"],
                "high_price": round(float(high_row["effective_price"]), 2),
                "latest_crawl_time": latest_time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    return pd.DataFrame(rows)


def build_recommendations() -> pd.DataFrame:
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


def export_debug_artifacts(artifacts: PipelineArtifacts) -> None:
    ensure_dir(ANALYTICS_DIR)
    artifacts.prepared_snapshot.to_csv(ANALYTICS_DIR / "prepared_snapshot.csv", index=False, encoding="utf-8-sig")
    artifacts.clean_snapshot.to_csv(ANALYTICS_DIR / "clean_snapshot.csv", index=False, encoding="utf-8-sig")
    artifacts.keyword_stats.to_csv(ANALYTICS_DIR / "keyword_stats.csv", index=False, encoding="utf-8-sig")
    artifacts.subtype_stats.to_csv(ANALYTICS_DIR / "subtype_stats.csv", index=False, encoding="utf-8-sig")
    artifacts.subtype_trend.to_csv(ANALYTICS_DIR / "subtype_trend.csv", index=False, encoding="utf-8-sig")
    artifacts.alerts.to_csv(ANALYTICS_DIR / "alerts.csv", index=False, encoding="utf-8-sig")
    artifacts.subtype_extremes.to_csv(ANALYTICS_DIR / "subtype_extremes.csv", index=False, encoding="utf-8-sig")
    artifacts.category_catalog.to_csv(ANALYTICS_DIR / "category_catalog.csv", index=False, encoding="utf-8-sig")

    summary = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "prepared_snapshot_count": int(artifacts.prepared_snapshot.shape[0]),
        "clean_snapshot_count": int(artifacts.clean_snapshot.shape[0]),
        "keyword_count": int(artifacts.keyword_stats.shape[0]),
        "subtype_count": int(artifacts.subtype_stats.shape[0]),
        "alert_rows": int(artifacts.alerts.shape[0]),
    }
    (ANALYTICS_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def sql_literal(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "NULL"
    if isinstance(value, (int, bool)):
        return str(int(value))
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    text = str(value).replace("\\", "\\\\").replace("'", "''")
    return f"'{text}'"


def build_insert_sql(table_name: str, columns: list[str], frame: pd.DataFrame, chunk_size: int = 200) -> list[str]:
    if frame.empty:
        return []
    statements: list[str] = []
    for start in range(0, len(frame), chunk_size):
        chunk = frame.iloc[start : start + chunk_size]
        values = []
        for _, row in chunk.iterrows():
            values.append(f"({', '.join(sql_literal(row[column]) for column in columns)})")
        statements.append(f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES\n" + ",\n".join(values) + ";")
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
  category_remark = NULL,
  is_dirty_data = 0,
  dirty_reason = NULL;
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
            "is_dirty_data",
            "dirty_reason",
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
  category_remark = {sql_literal(row["category_remark"])},
  is_dirty_data = {sql_literal(int(row["is_dirty_data"]))},
  dirty_reason = {sql_literal(row["dirty_reason"])}
WHERE
  crawl_batch_id = {sql_literal(row["crawl_batch_id"])}
  AND keyword = {sql_literal(row["source_keyword"])}
  AND page_no = {sql_literal(int(row["page_no"]))}
  AND COALESCE(shop, '') = {sql_literal(row["shop_match"])}
  AND sku_name = {sql_literal(row["sku_name"])};
""".strip()
        )
    return statements


def sync_to_mysql(artifacts: PipelineArtifacts) -> None:
    schema_sql = """
USE school_market;

SET @sql = (
  SELECT IF(
    COUNT(*) = 0,
    'ALTER TABLE pc_part_market_snapshot ADD COLUMN canonical_keyword VARCHAR(128) NULL',
    'SELECT 1'
  )
  FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = 'school_market' AND TABLE_NAME = 'pc_part_market_snapshot' AND COLUMN_NAME = 'canonical_keyword'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = (
  SELECT IF(
    COUNT(*) = 0,
    'ALTER TABLE pc_part_market_snapshot ADD COLUMN subtype_group VARCHAR(128) NULL',
    'SELECT 1'
  )
  FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = 'school_market' AND TABLE_NAME = 'pc_part_market_snapshot' AND COLUMN_NAME = 'subtype_group'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = (
  SELECT IF(
    COUNT(*) = 0,
    'ALTER TABLE pc_part_market_snapshot ADD COLUMN product_subtype VARCHAR(128) NULL',
    'SELECT 1'
  )
  FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = 'school_market' AND TABLE_NAME = 'pc_part_market_snapshot' AND COLUMN_NAME = 'product_subtype'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = (
  SELECT IF(
    COUNT(*) = 0,
    'ALTER TABLE pc_part_market_snapshot ADD COLUMN category_level1 VARCHAR(128) NULL',
    'SELECT 1'
  )
  FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = 'school_market' AND TABLE_NAME = 'pc_part_market_snapshot' AND COLUMN_NAME = 'category_level1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = (
  SELECT IF(
    COUNT(*) = 0,
    'ALTER TABLE pc_part_market_snapshot ADD COLUMN category_level2 VARCHAR(128) NULL',
    'SELECT 1'
  )
  FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = 'school_market' AND TABLE_NAME = 'pc_part_market_snapshot' AND COLUMN_NAME = 'category_level2'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = (
  SELECT IF(
    COUNT(*) = 0,
    'ALTER TABLE pc_part_market_snapshot ADD COLUMN category_level3 VARCHAR(128) NULL',
    'SELECT 1'
  )
  FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = 'school_market' AND TABLE_NAME = 'pc_part_market_snapshot' AND COLUMN_NAME = 'category_level3'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = (
  SELECT IF(
    COUNT(*) = 0,
    'ALTER TABLE pc_part_market_snapshot ADD COLUMN category_level4 VARCHAR(128) NULL',
    'SELECT 1'
  )
  FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = 'school_market' AND TABLE_NAME = 'pc_part_market_snapshot' AND COLUMN_NAME = 'category_level4'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = (
  SELECT IF(
    COUNT(*) = 0,
    'ALTER TABLE pc_part_market_snapshot ADD COLUMN category_remark VARCHAR(255) NULL',
    'SELECT 1'
  )
  FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = 'school_market' AND TABLE_NAME = 'pc_part_market_snapshot' AND COLUMN_NAME = 'category_remark'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = (
  SELECT IF(
    COUNT(*) = 0,
    'ALTER TABLE pc_part_market_snapshot ADD COLUMN is_dirty_data TINYINT(1) NOT NULL DEFAULT 0',
    'SELECT 1'
  )
  FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = 'school_market' AND TABLE_NAME = 'pc_part_market_snapshot' AND COLUMN_NAME = 'is_dirty_data'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = (
  SELECT IF(
    COUNT(*) = 0,
    'ALTER TABLE pc_part_market_snapshot ADD COLUMN dirty_reason VARCHAR(255) NULL',
    'SELECT 1'
  )
  FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = 'school_market' AND TABLE_NAME = 'pc_part_market_snapshot' AND COLUMN_NAME = 'dirty_reason'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

DROP TABLE IF EXISTS pc_part_market_category_catalog;
CREATE TABLE pc_part_market_category_catalog (
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

DROP TABLE IF EXISTS pc_part_market_keyword_stats;
CREATE TABLE pc_part_market_keyword_stats (
  keyword VARCHAR(128) NOT NULL,
  snapshot_count INT NOT NULL,
  subtype_count INT NOT NULL,
  min_effective_price DECIMAL(10, 2) NOT NULL,
  max_effective_price DECIMAL(10, 2) NOT NULL,
  avg_effective_price DECIMAL(10, 2) NOT NULL,
  latest_crawl_time DATETIME NOT NULL,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (keyword)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

DROP TABLE IF EXISTS pc_part_market_subtype_stats;
CREATE TABLE pc_part_market_subtype_stats (
  id BIGINT NOT NULL AUTO_INCREMENT,
  keyword VARCHAR(128) NOT NULL,
  subtype_group VARCHAR(128) NOT NULL,
  product_subtype VARCHAR(128) NOT NULL,
  category_level1 VARCHAR(128) NOT NULL,
  category_level2 VARCHAR(128) NOT NULL,
  category_level3 VARCHAR(128) NOT NULL,
  category_level4 VARCHAR(128) NOT NULL,
  snapshot_count INT NOT NULL,
  min_effective_price DECIMAL(10, 2) NOT NULL,
  max_effective_price DECIMAL(10, 2) NOT NULL,
  avg_effective_price DECIMAL(10, 2) NOT NULL,
  latest_crawl_time DATETIME NOT NULL,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_keyword_subtype (keyword, product_subtype),
  KEY idx_keyword_snapshot_count (keyword, snapshot_count),
  KEY idx_keyword_avg_price (keyword, avg_effective_price)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

DROP TABLE IF EXISTS pc_part_market_subtype_trend;
CREATE TABLE pc_part_market_subtype_trend (
  id BIGINT NOT NULL AUTO_INCREMENT,
  crawl_time DATETIME NOT NULL,
  keyword VARCHAR(128) NOT NULL,
  product_subtype VARCHAR(128) NOT NULL,
  category_level1 VARCHAR(128) NOT NULL,
  category_level2 VARCHAR(128) NOT NULL,
  category_level3 VARCHAR(128) NOT NULL,
  category_level4 VARCHAR(128) NOT NULL,
  snapshot_count INT NOT NULL,
  avg_effective_price DECIMAL(10, 2) NOT NULL,
  min_effective_price DECIMAL(10, 2) NOT NULL,
  max_effective_price DECIMAL(10, 2) NOT NULL,
  PRIMARY KEY (id),
  KEY idx_keyword_crawl_time (keyword, crawl_time),
  KEY idx_keyword_subtype (keyword, product_subtype)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

DROP TABLE IF EXISTS pc_part_market_alerts;
CREATE TABLE pc_part_market_alerts (
  id BIGINT NOT NULL AUTO_INCREMENT,
  keyword VARCHAR(128) NOT NULL,
  product_subtype VARCHAR(128) NOT NULL,
  category_level1 VARCHAR(128) NOT NULL,
  category_level2 VARCHAR(128) NOT NULL,
  category_level3 VARCHAR(128) NOT NULL,
  category_level4 VARCHAR(128) NOT NULL,
  previous_avg_price DECIMAL(10, 2) NOT NULL,
  latest_avg_price DECIMAL(10, 2) NOT NULL,
  delta_amount DECIMAL(10, 2) NOT NULL,
  delta_rate DECIMAL(10, 4) NOT NULL,
  current_min_price DECIMAL(10, 2) NOT NULL,
  current_max_price DECIMAL(10, 2) NOT NULL,
  previous_crawl_time DATETIME NOT NULL,
  latest_crawl_time DATETIME NOT NULL,
  sample_count INT NOT NULL,
  level VARCHAR(16) NOT NULL,
  alert_type VARCHAR(64) NOT NULL,
  rule_name VARCHAR(128) NOT NULL,
  PRIMARY KEY (id),
  KEY idx_keyword_level (keyword, level),
  KEY idx_keyword_subtype (keyword, product_subtype)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

DROP TABLE IF EXISTS pc_part_market_subtype_extremes;
CREATE TABLE pc_part_market_subtype_extremes (
  id BIGINT NOT NULL AUTO_INCREMENT,
  keyword VARCHAR(128) NOT NULL,
  product_subtype VARCHAR(128) NOT NULL,
  category_level1 VARCHAR(128) NOT NULL,
  category_level2 VARCHAR(128) NOT NULL,
  category_level3 VARCHAR(128) NOT NULL,
  category_level4 VARCHAR(128) NOT NULL,
  low_shop VARCHAR(255) NOT NULL,
  low_sku_name VARCHAR(512) NOT NULL,
  low_price DECIMAL(10, 2) NOT NULL,
  high_shop VARCHAR(255) NOT NULL,
  high_sku_name VARCHAR(512) NOT NULL,
  high_price DECIMAL(10, 2) NOT NULL,
  latest_crawl_time DATETIME NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_keyword_subtype_extreme (keyword, product_subtype)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

DROP TABLE IF EXISTS pc_part_market_recommendation;
CREATE TABLE pc_part_market_recommendation (
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
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

    statements = [schema_sql]
    statements.extend(build_snapshot_update_sql(artifacts.prepared_snapshot))
    statements.extend(build_insert_sql("pc_part_market_category_catalog", ["level1", "level2", "level3", "level4", "remark"], artifacts.category_catalog))
    statements.extend(
        build_insert_sql(
            "pc_part_market_keyword_stats",
            ["keyword", "snapshot_count", "subtype_count", "min_effective_price", "max_effective_price", "avg_effective_price", "latest_crawl_time"],
            artifacts.keyword_stats,
        )
    )
    statements.extend(
        build_insert_sql(
            "pc_part_market_subtype_stats",
            [
                "keyword",
                "subtype_group",
                "product_subtype",
                "category_level1",
                "category_level2",
                "category_level3",
                "category_level4",
                "snapshot_count",
                "min_effective_price",
                "max_effective_price",
                "avg_effective_price",
                "latest_crawl_time",
            ],
            artifacts.subtype_stats,
        )
    )
    statements.extend(
        build_insert_sql(
            "pc_part_market_subtype_trend",
            [
                "crawl_time",
                "keyword",
                "product_subtype",
                "category_level1",
                "category_level2",
                "category_level3",
                "category_level4",
                "snapshot_count",
                "avg_effective_price",
                "min_effective_price",
                "max_effective_price",
            ],
            artifacts.subtype_trend,
        )
    )
    statements.extend(
        build_insert_sql(
            "pc_part_market_alerts",
            [
                "keyword",
                "product_subtype",
                "category_level1",
                "category_level2",
                "category_level3",
                "category_level4",
                "previous_avg_price",
                "latest_avg_price",
                "delta_amount",
                "delta_rate",
                "current_min_price",
                "current_max_price",
                "previous_crawl_time",
                "latest_crawl_time",
                "sample_count",
                "level",
                "alert_type",
                "rule_name",
            ],
            artifacts.alerts,
        )
    )
    statements.extend(
        build_insert_sql(
            "pc_part_market_subtype_extremes",
            [
                "keyword",
                "product_subtype",
                "category_level1",
                "category_level2",
                "category_level3",
                "category_level4",
                "low_shop",
                "low_sku_name",
                "low_price",
                "high_shop",
                "high_sku_name",
                "high_price",
                "latest_crawl_time",
            ],
            artifacts.subtype_extremes,
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
        raise RuntimeError(result.stderr or result.stdout or "MySQL 同步失败")


def build_artifacts(mode: str, batch_id: str) -> PipelineArtifacts:
    raw_snapshot = load_snapshot(mode, batch_id)
    prepared_snapshot = prepare_snapshot(raw_snapshot)
    if prepared_snapshot.empty:
        raise RuntimeError("没有可用于分析的数据")

    clean_snapshot = filter_clean_snapshot(prepared_snapshot)
    category_catalog = build_category_catalog_frame()
    keyword_stats = build_keyword_stats(clean_snapshot)
    subtype_stats = build_subtype_stats(clean_snapshot)
    subtype_trend = build_subtype_trend(clean_snapshot)
    alerts = build_alerts(clean_snapshot)
    subtype_extremes = build_snapshot_extremes(clean_snapshot)
    recommendations = build_recommendations()

    return PipelineArtifacts(
        prepared_snapshot=prepared_snapshot,
        clean_snapshot=clean_snapshot,
        category_catalog=category_catalog,
        keyword_stats=keyword_stats,
        subtype_stats=subtype_stats,
        subtype_trend=subtype_trend,
        alerts=alerts,
        subtype_extremes=subtype_extremes,
        recommendations=recommendations,
    )


def main() -> int:
    args = parse_args()
    artifacts = build_artifacts(args.mode, args.batch_id)
    export_debug_artifacts(artifacts)
    sync_to_mysql(artifacts)

    print(f"原始快照行数：{len(artifacts.prepared_snapshot)}")
    print(f"干净快照行数：{len(artifacts.clean_snapshot)}")
    print(f"大类统计行数：{len(artifacts.keyword_stats)}")
    print(f"小类统计行数：{len(artifacts.subtype_stats)}")
    print(f"小类趋势行数：{len(artifacts.subtype_trend)}")
    print(f"均值波动告警行数：{len(artifacts.alerts)}")
    print(f"小类极值快照行数：{len(artifacts.subtype_extremes)}")
    print(f"调试文件目录：{ANALYTICS_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
