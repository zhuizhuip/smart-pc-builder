from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
MAIN_DB_PATH = BASE_DIR / "data" / "taobao_market.db"
ARCHIVE_DIR = BASE_DIR / "data" / "archive"
ARCHIVE_DB_PATH = ARCHIVE_DIR / "market_archive.db"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Archive old market snapshot batches from SQLite.")
    parser.add_argument("--keep-batches", type=int, default=15)
    return parser.parse_args()


def ensure_archive_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS taobao_pc_parts_snapshot (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            crawl_batch_id TEXT NOT NULL,
            crawl_time TEXT NOT NULL,
            keyword TEXT NOT NULL,
            page_no INTEGER NOT NULL,
            source_platform TEXT NOT NULL,
            shop TEXT,
            sku_name TEXT NOT NULL,
            coupon_price TEXT,
            original_price TEXT,
            source_url TEXT,
            archived_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_archive_batch_time
        ON taobao_pc_parts_snapshot (crawl_batch_id, crawl_time)
        """
    )
    conn.commit()


def main() -> int:
    args = parse_args()
    if not MAIN_DB_PATH.exists():
        print(f"Main SQLite DB not found: {MAIN_DB_PATH}")
        return 0

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(MAIN_DB_PATH) as main_conn:
        batch_rows = main_conn.execute(
            """
            SELECT crawl_batch_id, MAX(crawl_time) AS batch_time
            FROM taobao_pc_parts_snapshot
            GROUP BY crawl_batch_id
            ORDER BY batch_time DESC
            """
        ).fetchall()

        keep_count = max(args.keep_batches, 0)
        if len(batch_rows) <= keep_count:
            print(f"No archive needed. Current batches: {len(batch_rows)}, keep: {keep_count}")
            return 0

        archive_batch_ids = [row[0] for row in batch_rows[keep_count:]]
        placeholders = ",".join("?" for _ in archive_batch_ids)
        rows_to_archive = main_conn.execute(
            f"""
            SELECT
                crawl_batch_id,
                crawl_time,
                keyword,
                page_no,
                source_platform,
                shop,
                sku_name,
                coupon_price,
                original_price,
                source_url
            FROM taobao_pc_parts_snapshot
            WHERE crawl_batch_id IN ({placeholders})
            ORDER BY crawl_time, id
            """,
            archive_batch_ids,
        ).fetchall()

        if not rows_to_archive:
            print("No archive rows found.")
            return 0

        with sqlite3.connect(ARCHIVE_DB_PATH) as archive_conn:
            ensure_archive_schema(archive_conn)
            archive_conn.executemany(
                """
                INSERT INTO taobao_pc_parts_snapshot (
                    crawl_batch_id,
                    crawl_time,
                    keyword,
                    page_no,
                    source_platform,
                    shop,
                    sku_name,
                    coupon_price,
                    original_price,
                    source_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows_to_archive,
            )
            archive_conn.commit()

        main_conn.execute(
            f"DELETE FROM taobao_pc_parts_snapshot WHERE crawl_batch_id IN ({placeholders})",
            archive_batch_ids,
        )
        main_conn.commit()

    print(
        f"Archived {len(rows_to_archive)} rows from {len(archive_batch_ids)} old batches to {ARCHIVE_DB_PATH}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
