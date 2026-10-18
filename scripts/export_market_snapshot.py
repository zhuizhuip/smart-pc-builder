import argparse
import csv
import sqlite3
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
SQLITE_PATH = BASE_DIR / "data" / "taobao_market.db"
OUTPUT_PATH = BASE_DIR / "data" / "flink_input" / "market_source.csv"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="导出 SQLite 采集数据为 Flink CSV 输入文件")
    parser.add_argument(
        "--mode",
        choices=("all", "latest-batch"),
        default="all",
        help="导出全部数据或仅导出最新批次",
    )
    parser.add_argument(
        "--batch-id",
        default="",
        help="指定批次号导出；指定后优先级高于 --mode",
    )
    parser.add_argument(
        "--output",
        default=str(OUTPUT_PATH),
        help="导出 CSV 路径",
    )
    return parser.parse_args()


def ensure_parent_dir(file_path: Path) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)


def resolve_query(conn: sqlite3.Connection, mode: str, batch_id: str) -> tuple[str, tuple]:
    base_sql = """
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


def main() -> int:
    args = parse_args()
    output_path = Path(args.output).resolve()

    if not SQLITE_PATH.exists():
        print(f"未找到 SQLite 数据库：{SQLITE_PATH}")
        return 1

    ensure_parent_dir(output_path)

    with sqlite3.connect(SQLITE_PATH) as conn:
        sql, params = resolve_query(conn, args.mode, args.batch_id)
        rows = conn.execute(sql, params).fetchall()

    if not rows:
        print("没有可导出的数据")
        return 2

    # Flink CSV connector 直接消费纯数据行，避免表头和 BOM 造成类型转换失败。
    with output_path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerows(rows)

    print(f"导出完成：{output_path}")
    print(f"记录数：{len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
