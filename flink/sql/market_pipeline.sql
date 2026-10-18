SET 'execution.runtime-mode' = 'batch';
SET 'parallelism.default' = '1';
SET 'pipeline.name' = 'school-market-to-mysql';
SET 'pipeline.jars' = 'file:///opt/project/docker/flink/lib/flink-connector-jdbc-3.2.0-1.18.jar;file:///opt/project/docker/flink/lib/mysql-connector-j-8.4.0.jar';

CREATE TEMPORARY TABLE source_market_csv (
  crawl_batch_id STRING,
  crawl_time STRING,
  keyword STRING,
  page_no INT,
  source_platform STRING,
  shop STRING,
  sku_name STRING,
  coupon_price STRING,
  original_price STRING,
  source_url STRING
) WITH (
  'connector' = 'filesystem',
  'path' = 'file:///opt/project/data/flink_input/market_source.csv',
  'format' = 'csv',
  'csv.ignore-parse-errors' = 'true'
);

CREATE TEMPORARY TABLE mysql_market_snapshot (
  crawl_batch_id STRING,
  crawl_time TIMESTAMP(3),
  keyword STRING,
  page_no INT,
  source_platform STRING,
  shop STRING,
  sku_name STRING,
  coupon_price DECIMAL(10, 2),
  original_price DECIMAL(10, 2),
  source_url STRING
) WITH (
  'connector' = 'jdbc',
  'url' = 'jdbc:mysql://mysql:3306/school_market?useUnicode=true&characterEncoding=utf8&serverTimezone=Asia%2FShanghai',
  'table-name' = 'pc_part_market_snapshot',
  'username' = 'school',
  'password' = 'school123',
  'driver' = 'com.mysql.cj.jdbc.Driver'
);

INSERT INTO mysql_market_snapshot
SELECT
  crawl_batch_id,
  TO_TIMESTAMP(crawl_time, 'yyyy-MM-dd HH:mm:ss') AS crawl_time,
  TRIM(keyword) AS keyword,
  page_no,
  TRIM(source_platform) AS source_platform,
  NULLIF(TRIM(shop), '') AS shop,
  TRIM(sku_name) AS sku_name,
  CAST(NULLIF(TRIM(coupon_price), '') AS DECIMAL(10, 2)) AS coupon_price,
  CAST(NULLIF(TRIM(original_price), '') AS DECIMAL(10, 2)) AS original_price,
  NULLIF(TRIM(source_url), '') AS source_url
FROM source_market_csv;
