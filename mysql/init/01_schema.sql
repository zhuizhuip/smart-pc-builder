CREATE DATABASE IF NOT EXISTS school_market
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;

USE school_market;

CREATE TABLE IF NOT EXISTS pc_part_market_snapshot (
  id BIGINT NOT NULL AUTO_INCREMENT,
  crawl_batch_id VARCHAR(32) NOT NULL,
  crawl_time DATETIME NOT NULL,
  keyword VARCHAR(128) NOT NULL,
  page_no INT NOT NULL,
  source_platform VARCHAR(32) NOT NULL,
  shop VARCHAR(255) NULL,
  sku_name VARCHAR(512) NOT NULL,
  coupon_price DECIMAL(10, 2) NULL,
  original_price DECIMAL(10, 2) NULL,
  source_url TEXT NULL,
  canonical_keyword VARCHAR(128) NULL,
  subtype_group VARCHAR(128) NULL,
  product_subtype VARCHAR(128) NULL,
  category_level1 VARCHAR(128) NULL,
  category_level2 VARCHAR(128) NULL,
  category_level3 VARCHAR(128) NULL,
  category_level4 VARCHAR(128) NULL,
  category_remark VARCHAR(255) NULL,
  flink_import_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_keyword_crawl_time (keyword, crawl_time),
  KEY idx_shop (shop),
  KEY idx_source_platform (source_platform),
  KEY idx_canonical_keyword (canonical_keyword),
  KEY idx_product_subtype (product_subtype),
  KEY idx_category_level1 (category_level1),
  KEY idx_category_level4 (category_level4)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

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

CREATE TABLE IF NOT EXISTS pc_build_market_item (
  id BIGINT NOT NULL AUTO_INCREMENT,
  source_name VARCHAR(255) NOT NULL,
  raw_keyword VARCHAR(128) NOT NULL,
  component_type VARCHAR(64) NOT NULL,
  component_subtype VARCHAR(128) NOT NULL,
  platform_tag VARCHAR(64) NULL,
  memory_gen VARCHAR(32) NULL,
  storage_tag VARCHAR(64) NULL,
  power_tag VARCHAR(64) NULL,
  case_tag VARCHAR(64) NULL,
  shop VARCHAR(255) NULL,
  sku_name VARCHAR(512) NOT NULL,
  effective_price DECIMAL(10, 2) NOT NULL,
  source_url TEXT NULL,
  source_platform VARCHAR(32) NOT NULL DEFAULT 'taobao',
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_component_type_subtype (component_type, component_subtype),
  KEY idx_component_price (component_type, effective_price)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS pc_build_case_sample (
  id BIGINT NOT NULL AUTO_INCREMENT,
  source_name VARCHAR(255) NOT NULL,
  title VARCHAR(512) NOT NULL,
  detail_url TEXT NULL,
  ref_price DECIMAL(10, 2) NULL,
  config_type VARCHAR(128) NULL,
  cpu_series VARCHAR(128) NULL,
  cpu_model VARCHAR(128) NULL,
  gpu_model VARCHAR(128) NULL,
  memory_model VARCHAR(128) NULL,
  storage_model VARCHAR(128) NULL,
  build_tokens_json TEXT NOT NULL,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_case_cpu_gpu (cpu_model, gpu_model)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS pc_build_association_rule (
  id BIGINT NOT NULL AUTO_INCREMENT,
  antecedent VARCHAR(128) NOT NULL,
  consequent VARCHAR(128) NOT NULL,
  support_count INT NOT NULL,
  support_ratio DECIMAL(10, 4) NOT NULL,
  confidence DECIMAL(10, 4) NOT NULL,
  lift DECIMAL(10, 4) NOT NULL,
  source_name VARCHAR(255) NOT NULL,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_rule_pair (antecedent, consequent),
  KEY idx_rule_antecedent (antecedent, confidence)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS pc_build_budget_template (
  id BIGINT NOT NULL AUTO_INCREMENT,
  scenario_code VARCHAR(32) NOT NULL,
  component_type VARCHAR(64) NOT NULL,
  ratio_value DECIMAL(10, 4) NOT NULL,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_budget_template (scenario_code, component_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS pc_build_compatibility_rule (
  id BIGINT NOT NULL AUTO_INCREMENT,
  component_type VARCHAR(64) NOT NULL,
  rule_key VARCHAR(64) NOT NULL,
  rule_value VARCHAR(128) NOT NULL,
  compatible_values_json TEXT NOT NULL,
  note VARCHAR(255) NULL,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_compat_rule (component_type, rule_key, rule_value)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS pc_build_task (
  id BIGINT NOT NULL AUTO_INCREMENT,
  user_id BIGINT NOT NULL,
  budget DECIMAL(10, 2) NOT NULL,
  usage_scenario VARCHAR(32) NOT NULL,
  selected_parts_json TEXT NULL,
  total_price DECIMAL(10, 2) NULL,
  build_summary VARCHAR(255) NULL,
  note TEXT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_build_task_user (user_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS pc_build_result_item (
  id BIGINT NOT NULL AUTO_INCREMENT,
  task_id BIGINT NOT NULL,
  component_type VARCHAR(64) NOT NULL,
  component_subtype VARCHAR(128) NOT NULL,
  shop VARCHAR(255) NULL,
  sku_name VARCHAR(512) NOT NULL,
  effective_price DECIMAL(10, 2) NOT NULL,
  source_url TEXT NULL,
  score DECIMAL(10, 4) NOT NULL,
  reason_text VARCHAR(512) NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_result_task_component (task_id, component_type),
  CONSTRAINT fk_build_result_task FOREIGN KEY (task_id) REFERENCES pc_build_task(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
