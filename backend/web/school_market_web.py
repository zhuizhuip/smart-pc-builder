from __future__ import annotations

import csv
import io
import json
import os
import random as _random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from functools import wraps
from pathlib import Path
from typing import Any

from flask import Flask, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from flask_sock import Sock
from werkzeug.security import check_password_hash, generate_password_hash

from backend.config import Config
from backend.services.smart_build_engine import (
    BUDGET_TEMPLATES,
    fetch_build_history,
    generate_build_plan,
    get_build_form_options,
    learn_user_build_preferences,
    load_market_pool,
    preview_build_plan,
    refresh_build_knowledge,
)
from backend.web.rag_blueprint import rag_bp
from backend.web.rate_limit import rate_limit

BASE_DIR = Path(__file__).resolve().parents[2]
COMPOSE_FILE = BASE_DIR / "docker-compose.yml"
ANALYTICS_DIR = BASE_DIR / "data" / "analytics"
DEFAULT_KEYWORD = "显卡"
SEARCH_LIMIT = 30
SUBTYPE_SERIES_LIMIT = 6
LATEST_BATCH_COUNT = 5
WS_PUSH_INTERVAL_SECONDS = 5
MANUAL_REFRESH_COOLDOWN_SECONDS = 60
CJK_RANGE_START = chr(0x4E00)
CJK_RANGE_END = chr(0x9FFF)
USERNAME_ALLOWED_PATTERN = re.compile(f"^[A-Za-z0-9_{CJK_RANGE_START}-{CJK_RANGE_END}]{{2,24}}$")
USERNAME_HAS_CHINESE_PATTERN = re.compile(f"[{CJK_RANGE_START}-{CJK_RANGE_END}]")
BUILD_CHAT_SESSION_KEY = "build_chat_state"
BUILD_CHAT_HISTORY_KEY = "build_chat_history"
BUILD_IMPORT_SESSION_KEY = "community_build_import"

BUILD_CHAT_SUGGESTIONS = [
    "预算 6000，主要玩 3A，希望显卡优先",
    "我已经有 7500F，还差主板、显卡和内存",
    "办公加轻度剪辑，预算控制在 5000 左右",
    "给我一套 8000 元以内的白色主机方案",
]

BUILD_USAGE_HINTS = {
    "gaming": "游戏 / 3A / 电竞",
    "office": "办公 / 学习 / 轻度日常",
    "design": "设计 / 剪辑 / 建模",
    "streaming": "直播 / 推流 / 内容制作",
}

BUILD_COMPONENT_FIELDS = [
    ("CPU", "selected_cpu", "cpu"),
    ("显卡", "selected_gpu", "gpu"),
    ("主板", "selected_motherboard", "motherboard"),
    ("内存", "selected_memory", "memory"),
    ("固态硬盘", "selected_storage", "storage"),
    ("电源", "selected_power", "power"),
    ("机箱", "selected_case", "case"),
]

DEEPSEEK_API_URL = Config.DEEPSEEK_API_URL
DEEPSEEK_MODEL = Config.DEEPSEEK_MODEL

RECOMMENDATION_PROFILES = {
    "gaming": {
        "label": "游戏电竞",
        "ratios": {
            "CPU": 0.24,
            "电脑主板": 0.16,
            "内存条": 0.12,
            "显卡": 0.40,
            "CPU散热器": 0.05,
            "机箱散热风扇": 0.03,
        },
    },
    "office": {
        "label": "办公学习",
        "ratios": {
            "CPU": 0.34,
            "电脑主板": 0.24,
            "内存条": 0.18,
            "CPU散热器": 0.14,
            "机箱散热风扇": 0.10,
        },
    },
    "design": {
        "label": "设计创作",
        "ratios": {
            "CPU": 0.28,
            "电脑主板": 0.16,
            "内存条": 0.17,
            "显卡": 0.31,
            "CPU散热器": 0.05,
            "机箱散热风扇": 0.03,
        },
    },
    "streaming": {
        "label": "直播剪辑",
        "ratios": {
            "CPU": 0.26,
            "电脑主板": 0.16,
            "内存条": 0.15,
            "显卡": 0.32,
            "CPU散热器": 0.07,
            "机箱散热风扇": 0.04,
        },
    },
}

app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "frontend" / "templates"),
    static_folder=str(BASE_DIR / "frontend" / "static"),
    static_url_path="/static",
)
app.config["JSON_AS_ASCII"] = False
app.secret_key = os.environ.get("SCHOOL_MARKET_SECRET_KEY", "school-market-dev-key")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
sock = Sock(app)
app.register_blueprint(rag_bp)
_application_tables_ready = False


@app.route("/favicon.ico")
def favicon() -> tuple[str, int]:
    return "", 204

def run_mysql_query(sql: str, include_headers: bool = False) -> list[list[str]]:
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
        f"-p{os.environ.get('MYSQL_ROOT_PASSWORD', 'root123')}",
        "--default-character-set=utf8mb4",
        "-D",
        "school_market",
        "-B",
    ]
    if not include_headers:
        command.append("-N")
    command.extend(["-e", sql])
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout or "mysql query failed")
    return [row for row in csv.reader(io.StringIO(result.stdout), delimiter="\t") if row]

def run_mysql_script(sql: str) -> str:
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
        f"-p{os.environ.get('MYSQL_ROOT_PASSWORD', 'root123')}",
        "--default-character-set=utf8mb4",
        "-D",
        "school_market",
    ]
    result = subprocess.run(command, input=sql, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout or "mysql script failed")
    return result.stdout

def sql_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "''")

def success(data: Any) -> tuple[Any, int]:
    return jsonify({"ok": True, "data": data}), 200

def failure(message: str, status_code: int = 500) -> tuple[Any, int]:
    return jsonify({"ok": False, "error": message}), status_code

def tokenize_csv_text(value: str) -> list[str]:
    normalized = (value or "").replace("，", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]

def latest_batch_subquery(limit: int = LATEST_BATCH_COUNT) -> str:
    return f"""
        SELECT crawl_batch_id, MAX(crawl_time) AS batch_time
        FROM pc_part_market_snapshot
        WHERE is_dirty_data = 0
        GROUP BY crawl_batch_id
        ORDER BY batch_time DESC
        LIMIT {limit}
    """

def latest_batch_time_subquery(limit: int = LATEST_BATCH_COUNT) -> str:
    return f"""
        SELECT batch_time
        FROM ({latest_batch_subquery(limit)}) AS latest_batches
    """

def recommendation_profile(code: str) -> dict[str, Any]:
    return RECOMMENDATION_PROFILES.get(code, RECOMMENDATION_PROFILES["gaming"])

'新增'

PART_NAME_MAP = {
    "cpu": "CPU",
    "gpu": "显卡",
    "motherboard": "电脑主板",
    "memory": "内存条",
    "cooler": "CPU散热器",
    "fan": "机箱散热风扇",
}

def allocate_budget(usage: str, budget: float) -> dict[str, float]:
    profile = recommendation_profile(usage)
    ratios = profile["ratios"]

    result = {}

    for part_name, ratio in ratios.items():
        result[part_name] = round(budget * ratio, 2)

    return result

def remove_owned_parts(budget_plan: dict[str, float], owned_parts: dict[str, str]) -> dict[str, float]:
    new_plan = budget_plan.copy()

    for key in owned_parts.keys():
        part_name = PART_NAME_MAP.get(key)

        if part_name in new_plan:
            new_plan.pop(part_name)

    return new_plan

def mock_select_part(category: str, target_budget: float) -> dict[str, Any]:
    return {
        "category": category,
        "name": f"{category} 推荐型号",
        "price": target_budget,
        "reason": f"该配件预算约为 {target_budget} 元，符合当前用途的预算分配。",
    }

def generate_rule_based_plan(state: dict[str, Any]) -> dict[str, Any]:
    usage = state.get("usage", "gaming")
    budget = float(state.get("budget", 6000))
    owned_parts = state.get("owned_parts", {})

    budget_plan = allocate_budget(usage, budget)
    budget_plan = remove_owned_parts(budget_plan, owned_parts)

    parts = []

    for category, target_budget in budget_plan.items():
        part = mock_select_part(category, target_budget)
        parts.append(part)

    return {
        "usage": usage,
        "budget": budget,
        "owned_parts": owned_parts,
        "budget_plan": budget_plan,
        "parts": parts,
    }

'测试接口'
@app.route("/api/build/rule-test")
def rule_test():
    test_state = {
        "usage": "gaming",
        "budget": 6000,
        "owned_parts": {
            "cpu": "R5 7500F"
        },
        "priority": "gpu_first"
    }

    plan = generate_rule_based_plan(test_state)
    return success(plan)
# print("当前运行的是我修改过的文件")
def ensure_application_tables() -> None:
    global _application_tables_ready
    if _application_tables_ready:
        return

    run_mysql_script(
        """
USE school_market;

CREATE TABLE IF NOT EXISTS app_user (
  id BIGINT NOT NULL AUTO_INCREMENT,
  username VARCHAR(64) NOT NULL,
  password_hash VARCHAR(255) NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_login_at DATETIME NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_app_user_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS app_user_profile (
  user_id BIGINT NOT NULL,
  display_name VARCHAR(64) NULL,
  budget_min DECIMAL(10, 2) NULL,
  budget_max DECIMAL(10, 2) NULL,
  usage_preference VARCHAR(32) NULL,
  preferred_brands VARCHAR(255) NULL,
  preferred_keywords VARCHAR(255) NULL,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (user_id),
  CONSTRAINT fk_profile_user FOREIGN KEY (user_id) REFERENCES app_user(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS recommendation_task (
  id BIGINT NOT NULL AUTO_INCREMENT,
  user_id BIGINT NOT NULL,
  budget DECIMAL(10, 2) NOT NULL,
  usage_scenario VARCHAR(32) NOT NULL,
  preferred_brands VARCHAR(255) NULL,
  preferred_keywords VARCHAR(255) NULL,
  selected_parts_json TEXT NULL,
  total_budget DECIMAL(10, 2) NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'generated',
  note VARCHAR(255) NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_recommendation_task_user (user_id, created_at),
  CONSTRAINT fk_task_user FOREIGN KEY (user_id) REFERENCES app_user(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS recommendation_result (
  id BIGINT NOT NULL AUTO_INCREMENT,
  task_id BIGINT NOT NULL,
  keyword VARCHAR(128) NOT NULL,
  product_subtype VARCHAR(128) NOT NULL,
  shop VARCHAR(255) NOT NULL,
  sku_name VARCHAR(512) NOT NULL,
  effective_price DECIMAL(10, 2) NOT NULL,
  category_level1 VARCHAR(128) NULL,
  category_level2 VARCHAR(128) NULL,
  category_level3 VARCHAR(128) NULL,
  category_level4 VARCHAR(128) NULL,
  score DECIMAL(10, 4) NOT NULL,
  reason_text VARCHAR(512) NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_recommendation_result_task (task_id),
  CONSTRAINT fk_result_task FOREIGN KEY (task_id) REFERENCES recommendation_task(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS favorite_item (
  id BIGINT NOT NULL AUTO_INCREMENT,
  user_id BIGINT NOT NULL,
  keyword VARCHAR(128) NOT NULL,
  product_subtype VARCHAR(128) NOT NULL,
  sku_name VARCHAR(512) NOT NULL,
  shop VARCHAR(255) NOT NULL,
  effective_price DECIMAL(10, 2) NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_favorite_user (user_id, created_at),
  CONSTRAINT fk_favorite_user FOREIGN KEY (user_id) REFERENCES app_user(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS price_alert_subscription (
  id BIGINT NOT NULL AUTO_INCREMENT,
  user_id BIGINT NOT NULL,
  keyword VARCHAR(128) NOT NULL,
  product_subtype VARCHAR(128) NOT NULL,
  target_price DECIMAL(10, 2) NULL,
  target_change_rate DECIMAL(10, 4) NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_price_alert_user (user_id, created_at),
  CONSTRAINT fk_price_alert_user FOREIGN KEY (user_id) REFERENCES app_user(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS community_build_post (
  id BIGINT NOT NULL AUTO_INCREMENT,
  user_id BIGINT NOT NULL,
  source_type VARCHAR(32) NOT NULL DEFAULT 'manual',
  source_task_id BIGINT NULL,
  title VARCHAR(160) NOT NULL,
  usage_scenario VARCHAR(32) NOT NULL,
  budget DECIMAL(10, 2) NOT NULL DEFAULT 0,
  total_price DECIMAL(10, 2) NOT NULL DEFAULT 0,
  summary VARCHAR(255) NOT NULL DEFAULT '',
  content TEXT NULL,
  like_count INT NOT NULL DEFAULT 0,
  favorite_count INT NOT NULL DEFAULT 0,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_community_post_user (user_id, created_at),
  KEY idx_community_post_usage (usage_scenario, created_at),
  CONSTRAINT fk_community_post_user FOREIGN KEY (user_id) REFERENCES app_user(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS community_build_item (
  id BIGINT NOT NULL AUTO_INCREMENT,
  post_id BIGINT NOT NULL,
  component_type VARCHAR(64) NOT NULL,
  component_subtype VARCHAR(255) NOT NULL,
  shop VARCHAR(255) NULL,
  sku_name VARCHAR(512) NOT NULL,
  effective_price DECIMAL(10, 2) NOT NULL DEFAULT 0,
  source_url VARCHAR(1024) NULL,
  reason_text VARCHAR(512) NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_community_item_post (post_id),
  CONSTRAINT fk_community_item_post FOREIGN KEY (post_id) REFERENCES community_build_post(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS community_post_like (
  id BIGINT NOT NULL AUTO_INCREMENT,
  post_id BIGINT NOT NULL,
  user_id BIGINT NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_community_like (post_id, user_id),
  KEY idx_community_like_user (user_id, created_at),
  CONSTRAINT fk_community_like_post FOREIGN KEY (post_id) REFERENCES community_build_post(id) ON DELETE CASCADE,
  CONSTRAINT fk_community_like_user FOREIGN KEY (user_id) REFERENCES app_user(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS community_post_favorite (
  id BIGINT NOT NULL AUTO_INCREMENT,
  post_id BIGINT NOT NULL,
  user_id BIGINT NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_community_favorite (post_id, user_id),
  KEY idx_community_favorite_user (user_id, created_at),
  CONSTRAINT fk_community_favorite_post FOREIGN KEY (post_id) REFERENCES community_build_post(id) ON DELETE CASCADE,
  CONSTRAINT fk_community_favorite_user FOREIGN KEY (user_id) REFERENCES app_user(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS knowledge_category (
  id BIGINT NOT NULL AUTO_INCREMENT,
  slug VARCHAR(64) NOT NULL,
  name VARCHAR(64) NOT NULL,
  sort_order INT NOT NULL DEFAULT 100,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_knowledge_category_slug (slug)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS knowledge_article (
  id BIGINT NOT NULL AUTO_INCREMENT,
  category_id BIGINT NOT NULL,
  author_user_id BIGINT NULL,
  title VARCHAR(160) NOT NULL,
  summary VARCHAR(255) NOT NULL DEFAULT '',
  content TEXT NOT NULL,
  view_count INT NOT NULL DEFAULT 0,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_knowledge_article_category (category_id, created_at),
  CONSTRAINT fk_knowledge_article_category FOREIGN KEY (category_id) REFERENCES knowledge_category(id) ON DELETE CASCADE,
  CONSTRAINT fk_knowledge_article_author FOREIGN KEY (author_user_id) REFERENCES app_user(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""
    )
    _application_tables_ready = True
    ensure_admin_tables()
    ensure_seed_content()


def ensure_admin_tables() -> None:
    """迁移：添加 role 列 + system_config 表（幂等，可重复执行）."""
    migrate_sql = """
USE school_market;

-- 添加 role 列（如果已存在则忽略）
SET @col_exists = (SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA='school_market' AND TABLE_NAME='app_user' AND COLUMN_NAME='role');
SET @sql = IF(@col_exists=0,
  'ALTER TABLE app_user ADD COLUMN role VARCHAR(20) NOT NULL DEFAULT ''member'' AFTER password_hash',
  'SELECT ''role column exists, skipping''');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- system_config 表
CREATE TABLE IF NOT EXISTS system_config (
  config_key VARCHAR(100) NOT NULL,
  config_value TEXT NOT NULL,
  config_type VARCHAR(20) NOT NULL DEFAULT 'string',
  description VARCHAR(500) NULL,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (config_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 默认配置
INSERT IGNORE INTO system_config (config_key, config_value, config_type, description) VALUES
('refresh_cooldown_seconds', '60', 'int', '仪表盘手动刷新冷却秒数'),
('apriori_min_support', '2', 'int', 'Apriori最小支持度'),
('apriori_min_confidence', '0.15', 'float', 'Apriori最小置信度'),
('recommend_candidate_count', '8', 'int', '推荐候选数量上限'),
('price_score_weight', '0.60', 'float', '价格分权重'),
('popularity_score_weight', '0.25', 'float', '热度分权重'),
('brand_score_weight', '0.10', 'float', '品牌分权重'),
('keyword_score_weight', '0.05', 'float', '关键词分权重'),
('alert_medium_threshold', '0.15', 'float', '中等告警阈值'),
('alert_high_threshold', '0.30', 'float', '严重告警阈值'),
('discount_max_rate', '1.5', 'float', '折扣率上限'),
('archive_keep_batches', '15', 'int', '归档保留批次数'),
('collect_page_count', '5', 'int', '采集页面数/关键字');
"""
    run_mysql_script(migrate_sql)


def ensure_seed_content() -> None:
    seed_sql = """
USE school_market;

INSERT IGNORE INTO knowledge_category (id, slug, name, sort_order) VALUES
  (1, 'getting-started', '装机入门', 10),
  (2, 'compatibility', '兼容性说明', 20),
  (3, 'price-trend', '价格趋势解读', 30),
  (4, 'selection-guide', '选购建议', 40);

INSERT IGNORE INTO knowledge_article (id, category_id, title, summary, content, view_count) VALUES
  (
    1,
    1,
    '预算有限时如何确定装机优先级',
    '从显卡、CPU 到主板内存的预算划分思路，帮助普通用户快速明确取舍顺序。',
    '当预算有限时，建议先明确核心用途。如果以游戏为主，可优先保证显卡预算；如果以办公学习为主，则应优先保证CPU与内存的稳定组合。预算分配时，可先确定核心配件，再向主板、电源和机箱逐步补齐，避免平均分配导致关键部件性能不足。',
    0
  ),
  (
    2,
    2,
    '常见兼容性问题与排查方式',
    '梳理CPU插槽、内存代际、显卡供电和机箱尺寸等常见冲突项。',
    '装机时最常见的兼容性问题包括CPU与主板插槽不匹配、主板与内存代际不一致、显卡供电接口不足以及机箱空间不够等。建议在最终下单前，对核心配件进行逐项校验，并重点关注电源功率和BIOS支持情况。',
    0
  ),
  (
    3,
    3,
    '如何阅读配件价格趋势',
    '通过均价、极值和异常波动识别短期促销与价格回调信号。',
    '观察价格趋势时，不应只看单一商品价格，而应结合品类均价、最低价、最高价以及异常波动情况进行综合判断。如果某一时段最低价显著偏离均价，可能是短期活动价；如果均价持续上升，则说明整体市场可能进入涨价周期。',
    0
  ),
  (
    4,
    4,
    '主流用途下的配件选购建议',
    '分别面向游戏、办公、设计和直播剪辑给出核心配件选择思路。',
    '不同用途下，配件优先级存在明显差异。游戏场景通常更依赖显卡，办公场景更看重稳定性与功耗控制，设计与剪辑场景则需要兼顾多核性能、大容量内存与较好的显卡支持。用户可先明确用途，再结合预算调整具体型号。',
    0
  );
"""
    run_mysql_script(seed_sql)


def get_config(key: str, default: str = "") -> str:
    """读取系统配置."""
    rows = run_mysql_query(
        f"SELECT config_value FROM system_config WHERE config_key = '{sql_escape(key)}'"
    )
    return rows[0][0] if rows else default


def get_int_config(key: str, default: int = 0) -> int:
    try: return int(get_config(key, str(default)))
    except: return default


def get_float_config(key: str, default: float = 0.0) -> float:
    try: return float(get_config(key, str(default)))
    except: return default


def set_config(key: str, value: str) -> None:
    run_mysql_script(
        f"USE school_market; INSERT INTO system_config (config_key, config_value) VALUES "
        f"('{sql_escape(key)}', '{sql_escape(value)}') "
        f"ON DUPLICATE KEY UPDATE config_value = '{sql_escape(value)}'"
    )


def get_current_user() -> dict[str, Any] | None:
    user_id = session.get("user_id")
    if not user_id:
        return None
    ensure_application_tables()
    rows = run_mysql_query(
        f"""
        SELECT
          u.id,
          u.username,
          COALESCE(p.display_name, ''),
          COALESCE(p.budget_min, ''),
          COALESCE(p.budget_max, ''),
          COALESCE(p.usage_preference, ''),
          COALESCE(p.preferred_brands, ''),
          COALESCE(p.preferred_keywords, ''),
          u.role,
          DATE_FORMAT(u.created_at, '%Y-%m-%d')
        FROM app_user u
        LEFT JOIN app_user_profile p ON p.user_id = u.id
        WHERE u.id = {int(user_id)}
        LIMIT 1;
        """
    )
    if not rows:
        session.clear()
        return None

    row = rows[0]
    return {
        "id": int(row[0]),
        "username": row[1],
        "display_name": row[2],
        "budget_min": float(row[3]) if row[3] != "" else None,
        "budget_max": float(row[4]) if row[4] != "" else None,
        "usage_preference": row[5],
        "preferred_brands": row[6],
        "preferred_keywords": row[7],
        "role": row[8] if len(row) > 8 else "member",
        "created_at": row[9] if len(row) > 9 else "",
    }

@app.context_processor
def inject_template_context() -> dict[str, Any]:
    return {
        "current_user": get_current_user(),
        "recommendation_profiles": RECOMMENDATION_PROFILES,
    }


def require_role(*roles: str):
    """角色权限装饰器。用法: @app.post('/xxx'); @require_role('admin','data_admin')"""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            user = get_current_user()
            if not user:
                return jsonify({"ok": False, "error": "请先登录"}), 401
            if user.get("role", "member") not in roles:
                return jsonify({"ok": False, "error": "无权限访问"}), 403
            return f(*args, **kwargs)
        return wrapper
    return decorator


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if get_current_user() is None:
            flash("请先登录后再使用推荐与个人功能。", "warning")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped_view

def fetch_keyword_stats() -> list[dict[str, Any]]:
    rows = run_mysql_query(
        f"""
        SELECT
          s.canonical_keyword AS keyword,
          COUNT(*) AS snapshot_count,
          COUNT(DISTINCT s.product_subtype) AS subtype_count,
          MIN(COALESCE(s.coupon_price, s.original_price)) AS min_effective_price,
          MAX(COALESCE(s.coupon_price, s.original_price)) AS max_effective_price,
          AVG(COALESCE(s.coupon_price, s.original_price)) AS avg_effective_price,
          DATE_FORMAT(MAX(s.crawl_time), '%Y-%m-%d %H:%i:%s') AS latest_crawl_time
        FROM pc_part_market_snapshot AS s
        INNER JOIN ({latest_batch_subquery()}) AS lb ON s.crawl_batch_id = lb.crawl_batch_id
        WHERE s.is_dirty_data = 0
          AND COALESCE(s.canonical_keyword, '') <> ''
          AND COALESCE(s.product_subtype, '') <> ''
        GROUP BY s.canonical_keyword
        ORDER BY snapshot_count DESC, keyword;
        """
    )
    return [
        {
            "keyword": row[0],
            "snapshot_count": int(row[1]),
            "subtype_count": int(row[2]),
            "min_effective_price": float(row[3]),
            "max_effective_price": float(row[4]),
            "avg_effective_price": float(row[5]),
            "latest_crawl_time": row[6],
        }
        for row in rows
    ]

def resolve_selected_keyword(selected_keyword: str | None, keyword_stats: list[dict[str, Any]]) -> str:
    keyword_names = [item["keyword"] for item in keyword_stats]
    keyword = selected_keyword or DEFAULT_KEYWORD
    if keyword not in keyword_names and keyword_names:
        keyword = keyword_names[0]
    return keyword

def fetch_overview(selected_keyword: str | None) -> dict[str, Any]:
    keyword_stats = fetch_keyword_stats()
    keyword = resolve_selected_keyword(selected_keyword, keyword_stats)
    selected_row = next((item for item in keyword_stats if item["keyword"] == keyword), None)
    dirty_rows = run_mysql_query(
        f"""
        SELECT COUNT(*)
        FROM pc_part_market_snapshot AS s
        INNER JOIN ({latest_batch_subquery()}) AS lb ON s.crawl_batch_id = lb.crawl_batch_id
        WHERE s.is_dirty_data = 1;
        """
    )
    dirty_count = int(dirty_rows[0][0]) if dirty_rows else 0
    latest_crawl_time = max((item["latest_crawl_time"] for item in keyword_stats), default="-")
    snapshot_count = sum(item["snapshot_count"] for item in keyword_stats)
    latest_batches = run_mysql_query(
        f"""
        SELECT crawl_batch_id, DATE_FORMAT(batch_time, '%Y-%m-%d %H:%i:%s')
        FROM ({latest_batch_subquery()}) AS latest_batches
        ORDER BY batch_time DESC;
        """
    )
    return {
        "snapshot_count": snapshot_count,
        "clean_snapshot_count": snapshot_count,
        "dirty_count": dirty_count,
        "keyword_count": len(keyword_stats),
        "selected_keyword": keyword,
        "selected_subtype_count": selected_row["subtype_count"] if selected_row else 0,
        "latest_crawl_time": latest_crawl_time,
        "keyword_stats": keyword_stats,
        "viewer_mode": "游客公开看板",
        "registered_features_reserved": True,
        "latest_batch_count": len(latest_batches),
        "latest_batches": [{"crawl_batch_id": row[0], "crawl_time": row[1]} for row in latest_batches],
    }

def fetch_subtype_stats(keyword: str, limit: int = 18) -> list[dict[str, Any]]:
    rows = run_mysql_query(
        f"""
        SELECT
          s.product_subtype,
          MAX(COALESCE(s.category_level1, '')) AS category_level1,
          MAX(COALESCE(s.category_level2, '')) AS category_level2,
          MAX(COALESCE(s.category_level3, '')) AS category_level3,
          MAX(COALESCE(s.category_level4, '')) AS category_level4,
          COUNT(*) AS snapshot_count,
          MIN(COALESCE(s.coupon_price, s.original_price)) AS min_effective_price,
          MAX(COALESCE(s.coupon_price, s.original_price)) AS max_effective_price,
          AVG(COALESCE(s.coupon_price, s.original_price)) AS avg_effective_price,
          DATE_FORMAT(MAX(s.crawl_time), '%Y-%m-%d %H:%i:%s') AS latest_crawl_time
        FROM pc_part_market_snapshot AS s
        INNER JOIN ({latest_batch_subquery()}) AS lb ON s.crawl_batch_id = lb.crawl_batch_id
        WHERE s.is_dirty_data = 0
          AND s.canonical_keyword = '{sql_escape(keyword)}'
          AND COALESCE(s.product_subtype, '') <> ''
        GROUP BY s.product_subtype
        ORDER BY snapshot_count DESC, avg_effective_price ASC, s.product_subtype
        LIMIT {limit};
        """
    )
    return [
        {
            "product_subtype": row[0],
            "category_level1": row[1],
            "category_level2": row[2],
            "category_level3": row[3],
            "category_level4": row[4],
            "snapshot_count": int(row[5]),
            "min_effective_price": float(row[6]),
            "max_effective_price": float(row[7]),
            "avg_effective_price": float(row[8]),
            "latest_crawl_time": row[9],
        }
        for row in rows
    ]

def fetch_all_subtype_stats() -> dict[str, list[dict[str, Any]]]:
    rows = run_mysql_query(
        """
        SELECT
          keyword,
          product_subtype,
          category_level1,
          category_level2,
          category_level3,
          category_level4,
          snapshot_count,
          min_effective_price,
          max_effective_price,
          avg_effective_price,
          DATE_FORMAT(latest_crawl_time, '%Y-%m-%d %H:%i:%s')
        FROM pc_part_market_subtype_stats
        ORDER BY keyword, snapshot_count DESC, avg_effective_price ASC, product_subtype;
        """
    )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row[0]].append(
            {
                "keyword": row[0],
                "product_subtype": row[1],
                "category_level1": row[2],
                "category_level2": row[3],
                "category_level3": row[4],
                "category_level4": row[5],
                "snapshot_count": int(row[6]),
                "min_effective_price": float(row[7]),
                "max_effective_price": float(row[8]),
                "avg_effective_price": float(row[9]),
                "latest_crawl_time": row[10],
            }
        )
    return grouped

def fetch_subtype_trend(keyword: str, top_subtypes: list[str]) -> dict[str, Any]:
    if not top_subtypes:
        return {"times": [], "series": []}
    subtype_filter = ", ".join(f"'{sql_escape(item)}'" for item in top_subtypes)
    rows = run_mysql_query(
        f"""
        SELECT
          DATE_FORMAT(MAX(s.crawl_time), '%Y-%m-%d %H:%i:%s') AS batch_time,
          s.product_subtype,
          AVG(COALESCE(s.coupon_price, s.original_price)) AS avg_effective_price,
          MIN(COALESCE(s.coupon_price, s.original_price)) AS min_effective_price,
          MAX(COALESCE(s.coupon_price, s.original_price)) AS max_effective_price
        FROM pc_part_market_snapshot AS s
        INNER JOIN ({latest_batch_subquery()}) AS lb ON s.crawl_batch_id = lb.crawl_batch_id
        WHERE s.is_dirty_data = 0
          AND s.canonical_keyword = '{sql_escape(keyword)}'
          AND s.product_subtype IN ({subtype_filter})
        GROUP BY s.crawl_batch_id, s.product_subtype
        ORDER BY MAX(s.crawl_time), s.product_subtype;
        """
    )
    timeline: dict[str, dict[str, dict[str, float]]] = defaultdict(dict)
    for crawl_time, product_subtype, avg_price, min_price, max_price in rows:
        timeline[crawl_time][product_subtype] = {
            "avg": float(avg_price),
            "min": float(min_price),
            "max": float(max_price),
        }
    times = sorted(timeline.keys())
    series = [
        {
            "name": subtype,
            "avg": [timeline[time_point].get(subtype, {}).get("avg") for time_point in times],
            "min": [timeline[time_point].get(subtype, {}).get("min") for time_point in times],
            "max": [timeline[time_point].get(subtype, {}).get("max") for time_point in times],
        }
        for subtype in top_subtypes
    ]
    return {"times": times, "series": series}

def fetch_alerts(keyword: str, limit: int = 20) -> list[dict[str, Any]]:
    rows = run_mysql_query(
        f"""
        SELECT
          product_subtype,
          previous_avg_price,
          latest_avg_price,
          delta_amount,
          delta_rate,
          current_min_price,
          current_max_price,
          DATE_FORMAT(previous_crawl_time, '%Y-%m-%d %H:%i:%s'),
          DATE_FORMAT(latest_crawl_time, '%Y-%m-%d %H:%i:%s'),
          sample_count,
          level,
          alert_type
        FROM pc_part_market_alerts
        WHERE keyword = '{sql_escape(keyword)}'
        ORDER BY delta_rate DESC, latest_crawl_time DESC
        LIMIT {limit};
        """
    )
    return [
        {
            "product_subtype": row[0],
            "previous_avg_price": float(row[1]),
            "latest_avg_price": float(row[2]),
            "delta_amount": float(row[3]),
            "delta_rate": float(row[4]),
            "current_min_price": float(row[5]),
            "current_max_price": float(row[6]),
            "previous_crawl_time": row[7],
            "latest_crawl_time": row[8],
            "sample_count": int(row[9]),
            "level": row[10],
            "alert_type": row[11],
        }
        for row in rows
    ]

def fetch_snapshot_extremes(keyword: str, limit: int = 20) -> list[dict[str, Any]]:
    rows = run_mysql_query(
        f"""
        SELECT
          s.product_subtype,
          SUBSTRING_INDEX(
            GROUP_CONCAT(COALESCE(s.shop, '') ORDER BY COALESCE(s.coupon_price, s.original_price) ASC, s.crawl_time DESC SEPARATOR '\n'),
            '\n',
            1
          ) AS low_shop,
          SUBSTRING_INDEX(
            GROUP_CONCAT(s.sku_name ORDER BY COALESCE(s.coupon_price, s.original_price) ASC, s.crawl_time DESC SEPARATOR '\n'),
            '\n',
            1
          ) AS low_sku_name,
          SUBSTRING_INDEX(
            GROUP_CONCAT(COALESCE(s.source_url, '') ORDER BY COALESCE(s.coupon_price, s.original_price) ASC, s.crawl_time DESC SEPARATOR '\n'),
            '\n',
            1
          ) AS low_source_url,
          MIN(COALESCE(s.coupon_price, s.original_price)) AS low_price,
          SUBSTRING_INDEX(
            GROUP_CONCAT(COALESCE(s.shop, '') ORDER BY COALESCE(s.coupon_price, s.original_price) DESC, s.crawl_time DESC SEPARATOR '\n'),
            '\n',
            1
          ) AS high_shop,
          SUBSTRING_INDEX(
            GROUP_CONCAT(s.sku_name ORDER BY COALESCE(s.coupon_price, s.original_price) DESC, s.crawl_time DESC SEPARATOR '\n'),
            '\n',
            1
          ) AS high_sku_name,
          SUBSTRING_INDEX(
            GROUP_CONCAT(COALESCE(s.source_url, '') ORDER BY COALESCE(s.coupon_price, s.original_price) DESC, s.crawl_time DESC SEPARATOR '\n'),
            '\n',
            1
          ) AS high_source_url,
          MAX(COALESCE(s.coupon_price, s.original_price)) AS high_price,
          DATE_FORMAT(MAX(s.crawl_time), '%Y-%m-%d %H:%i:%s') AS latest_crawl_time
        FROM pc_part_market_snapshot AS s
        INNER JOIN ({latest_batch_subquery()}) AS lb ON s.crawl_batch_id = lb.crawl_batch_id
        WHERE s.is_dirty_data = 0
          AND s.canonical_keyword = '{sql_escape(keyword)}'
          AND COALESCE(s.product_subtype, '') <> ''
        GROUP BY s.product_subtype
        ORDER BY low_price ASC, s.product_subtype
        LIMIT {limit};
        """
    )
    return [
        {
            "product_subtype": row[0],
            "low_shop": row[1],
            "low_sku_name": row[2],
            "low_source_url": row[3],
            "low_price": float(row[4]),
            "high_shop": row[5],
            "high_sku_name": row[6],
            "high_source_url": row[7],
            "high_price": float(row[8]),
            "latest_crawl_time": row[9],
        }
        for row in rows
    ]

def search_products(query: str, keyword: str | None = None, limit: int = SEARCH_LIMIT) -> list[dict[str, Any]]:
    term = query.strip()
    keyword_condition = ""
    if keyword:
        keyword_condition = f" AND canonical_keyword = '{sql_escape(keyword)}'"
    search_condition = ""
    if term:
        like = f"%{sql_escape(term)}%"
        search_condition = f"""
          AND (
            sku_name LIKE '{like}'
            OR product_subtype LIKE '{like}'
            OR category_level4 LIKE '{like}'
            OR COALESCE(shop, '') LIKE '{like}'
          )
        """
    rows = run_mysql_query(
        f"""
        SELECT
          DATE_FORMAT(crawl_time, '%Y-%m-%d %H:%i:%s'),
          canonical_keyword,
          product_subtype,
          category_level4,
          COALESCE(shop, ''),
          sku_name,
          COALESCE(coupon_price, original_price) AS effective_price,
          COALESCE(source_url, '') AS source_url
        FROM pc_part_market_snapshot
        WHERE is_dirty_data = 0
          AND crawl_batch_id IN (
            SELECT crawl_batch_id
            FROM ({latest_batch_subquery()}) AS latest_batches
          )
          {keyword_condition}
          {search_condition}
        ORDER BY crawl_time DESC, effective_price ASC
        LIMIT {limit};
        """
    )
    return [
        {
            "crawl_time": row[0],
            "keyword": row[1],
            "product_subtype": row[2],
            "category_level4": row[3],
            "shop": row[4] or "未知店铺",
            "sku_name": row[5],
            "effective_price": float(row[6]),
            "source_url": row[7],
        }
        for row in rows
    ]

def build_dashboard_payload(selected_keyword: str | None, query: str = "") -> dict[str, Any]:
    overview = fetch_overview(selected_keyword)
    keyword = overview["selected_keyword"]
    keyword_stats = overview["keyword_stats"]
    subtype_stats = fetch_subtype_stats(keyword)
    top_subtypes = [item["product_subtype"] for item in subtype_stats[:SUBTYPE_SERIES_LIMIT]]
    return {
        "overview": overview,
        "selected_keyword": keyword,
        "keyword_stats": keyword_stats,
        "subtype_stats": subtype_stats,
        "subtype_trend": fetch_subtype_trend(keyword, top_subtypes),
        "alerts": fetch_alerts(keyword),
        "snapshot_extremes": fetch_snapshot_extremes(keyword),
        "search_results": search_products(query, keyword),
        "search_query": query,
        "download_links": {
            "clean_snapshot": "/api/download/clean-snapshot.csv",
            "subtype_stats": "/api/download/subtype-stats.csv",
            "alerts": "/api/download/alerts.csv",
        },
    }

def fetch_user_by_username(username: str) -> list[str] | None:
    ensure_application_tables()
    rows = run_mysql_query(
        f"""
        SELECT id, username, password_hash
        FROM app_user
        WHERE username = '{sql_escape(username)}'
        LIMIT 1;
        """
    )
    return rows[0] if rows else None

def validate_username(username: str) -> str | None:
    if not username:
        return "用户名不能为空。"
    if not USERNAME_ALLOWED_PATTERN.fullmatch(username):
        return "用户名仅允许中文、字母、数字和下划线。"
    if USERNAME_HAS_CHINESE_PATTERN.search(username):
        if len(username) < 2:
            return "中文用户名至少需要 2 个字符。"
    elif len(username) < 4:
        return "非中文用户名至少需要 4 个字符。"
    return None

def validate_password(password: str) -> str | None:
    if not password:
        return "密码不能为空。"
    if len(password) < 8:
        return "密码长度至少为 8 位。"

    checks = [
        any(char.islower() for char in password),
        any(char.isupper() for char in password),
        any(char.isdigit() for char in password),
        any(not char.isalnum() for char in password),
    ]
    if sum(checks) < 3:
        return "密码需至少包含大写字母、小写字母、数字、特殊字符中的三类。"
    return None

def normalize_build_match_text(value: str) -> str:
    return re.sub(r"[\s\-_/（）()【】\[\]，,。\.]+", "", value or "").upper()

def build_user_soft_constraints(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "white_theme": False,
        "quiet_mode": False,
        "brands": tokenize_csv_text(str(user.get("preferred_brands") or "")),
        "keywords": tokenize_csv_text(str(user.get("preferred_keywords") or "")),
    }

def default_build_chat_state(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "budget": None,  # 不自动从用户偏好读取，等用户主动输入
        "usage_scenario": user.get("usage_preference") or "",
        "selected_parts": {},
        "soft_constraints": build_user_soft_constraints(user),
    }

def get_build_chat_state(user: dict[str, Any]) -> dict[str, Any]:
    state = session.get(BUILD_CHAT_SESSION_KEY)
    if not isinstance(state, dict):
        state = default_build_chat_state(user)
        session[BUILD_CHAT_SESSION_KEY] = state
    # 只有在用户已有对话历史、且未显式设置预算时，才回退到个人偏好
    # （不做 setdefault，避免初次进入就显示用户预算）
    state.setdefault("usage_scenario", user.get("usage_preference") or "")
    state.setdefault("selected_parts", {})
    soft = dict(state.get("soft_constraints") or {})
    profile_soft = build_user_soft_constraints(user)
    soft.setdefault("white_theme", profile_soft["white_theme"])
    soft.setdefault("quiet_mode", profile_soft["quiet_mode"])
    brands = list(soft.get("brands") or [])
    for brand in profile_soft["brands"]:
        if brand not in brands:
            brands.append(brand)
    keywords = list(soft.get("keywords") or [])
    for keyword in profile_soft["keywords"]:
        if keyword not in keywords:
            keywords.append(keyword)
    soft["brands"] = brands
    soft["keywords"] = keywords
    state["soft_constraints"] = soft
    return state

def save_build_chat_state(state: dict[str, Any]) -> None:
    session[BUILD_CHAT_SESSION_KEY] = state
    session.modified = True

def extract_budget_from_text(text: str) -> float | None:
    budget_patterns = [
        r"(?:预算|价位|控制在|大概|总价|整机)\s*(?:在|约|大约|控制在)?\s*([2-9]\d{3,4})",
        r"([2-9]\d{3,4})\s*(?:元|块|左右|以内|上下)",
    ]
    for pattern in budget_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return float(match.group(1))
    return None

def get_build_chat_history() -> list[dict[str, str]]:
    history = session.get(BUILD_CHAT_HISTORY_KEY)
    if not isinstance(history, list):
        return []
    sanitized: list[dict[str, str]] = []
    for item in history[-8:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", "")).strip()
        content = str(item.get("content", "")).strip()
        if role in {"user", "assistant"} and content:
            sanitized.append({"role": role, "content": content})
    return sanitized

def save_build_chat_history(history: list[dict[str, str]]) -> None:
    session[BUILD_CHAT_HISTORY_KEY] = history[-8:]
    session.modified = True

def reset_build_chat_state(user: dict[str, Any]) -> dict[str, Any]:
    state = default_build_chat_state(user)
    save_build_chat_state(state)
    save_build_chat_history([])
    return state


def save_build_import_draft(draft: dict[str, Any]) -> None:
    session[BUILD_IMPORT_SESSION_KEY] = draft
    session.modified = True


def pop_build_import_draft() -> dict[str, Any] | None:
    draft = session.pop(BUILD_IMPORT_SESSION_KEY, None)
    session.modified = True
    return draft if isinstance(draft, dict) else None

def detect_selected_parts_from_text(text: str, options: dict[str, list[str]]) -> dict[str, str]:
    normalized_text = normalize_build_match_text(text)
    matched: dict[str, str] = {}
    for component_type, _field_name, options_key in BUILD_COMPONENT_FIELDS:
        candidates = sorted(options.get(options_key, []), key=len, reverse=True)
        for candidate in candidates:
            normalized_candidate = normalize_build_match_text(candidate)
            if normalized_candidate and normalized_candidate in normalized_text:
                matched[component_type] = candidate
                break
    return matched

def suggest_build_follow_ups(state: dict[str, Any]) -> list[str]:
    if not state.get("budget"):
        return ["先告诉我预算，例如：预算 6000 左右。"]
    if not state.get("usage_scenario"):
        return ["再告诉我用途，例如：主要玩 3A / 办公学习 / 设计剪辑。"]

    has_selected = bool(state.get("selected_parts"))
    has_both = state.get("budget") and state.get("usage_scenario")

    if has_both and has_selected:
        return [
            "显卡换强一点",
            "太贵了，降预算",
            "预算加到 8000",
            "我想要白色主题",
        ]
    if has_both:
        return [
            "我已经有 7500F",
            "显卡想上 RTX 5060 Ti",
            "预算加到 8000，偏白色主机",
            "我想静音优先",
        ]
    return [
        "先告诉我预算，例如：预算 6000 左右。",
        "说说你的用途：游戏 / 办公 / 设计 / 直播。",
    ]

def build_chat_explanations(build_payload: dict[str, Any] | None) -> list[dict[str, str]]:
    if not build_payload:
        return []
    explanations: list[dict[str, str]] = []
    for item in build_payload.get("results", []):
        explanations.append(
            {
                "title": f"{item['component_type']} · {item['component_subtype']}",
                "reason": item.get("reason_text", ""),
                "price_text": f"¥{float(item['effective_price']):.2f}",
            }
        )
    return explanations


def build_variant_choice_reason(
    code: str,
    label: str,
    requested_budget: float,
    solver_budget: float,
    total_price: float,
    soft: dict[str, Any],
) -> str:
    gap = total_price - requested_budget
    soft_points: list[str] = []
    if soft.get("white_theme"):
        soft_points.append("白色主题")
    if soft.get("quiet_mode"):
        soft_points.append("静音体验")
    if soft.get("brands"):
        soft_points.append("品牌偏好")

    if code == "A":
        reason = "这套作为默认主推，整体预算贴合度最高，配件分配更均衡。"
    elif code == "B":
        reason = "这套更偏保守控价，适合想把总价压低一些，或者优先追求静音与稳定的情况。"
    else:
        reason = "这套会把更多预算压到性能或外观上，适合想把上限再往上冲一点。"

    if gap > 0:
        budget_text = f"当前总价比目标预算高出约 ¥{gap:.0f}。"
    elif gap < 0:
        budget_text = f"当前总价比目标预算低约 ¥{abs(gap):.0f}，还有继续加码空间。"
    else:
        budget_text = "当前总价基本贴合你的目标预算。"

    solver_text = ""
    if abs(solver_budget - requested_budget) >= 100:
        solver_text = f"这一版内部求解预算按 ¥{solver_budget:.0f} 做了取舍。"

    soft_text = ""
    if soft_points:
        soft_text = f"同时兼顾了你的{'、'.join(soft_points)}诉求。"

    return " ".join(part for part in [reason, budget_text, solver_text, soft_text] if part)

def summarize_build_payload(build_payload: dict[str, Any] | None) -> str:
    if not build_payload:
        return "当前尚未生成完整搭配方案。"
    lines = [
        f"场景：{build_payload['usage_label']}",
        f"预算：¥{build_payload['budget']:.2f}",
        f"总价：¥{build_payload['total_price']:.2f}",
    ]
    for item in build_payload.get("results", []):
        lines.append(
            f"- {item['component_type']}：{item['component_subtype']}，价格 ¥{float(item['effective_price']):.2f}，理由：{item.get('reason_text', '')}"
        )
    return "\n".join(lines)

def get_deepseek_api_key() -> str:
    return Config.DEEPSEEK_API_KEY.strip()
    # 旧逻辑会优先吃系统环境变量，当前机器里残留了旧 key，
    # 会把上面的默认配置覆盖掉，导致模型调用 401。
    # 如需恢复环境变量覆盖模式，再改回下面这一行：
    # return os.environ.get("DEEPSEEK_API_KEY", Config.DEEPSEEK_API_KEY).strip()

def serialize_build_chat_state(state: dict[str, Any]) -> dict[str, Any]:
    usage_scenario = state.get("usage_scenario") or ""
    return {
        "budget": state.get("budget"),
        "usage_scenario": usage_scenario,
        "usage_label": BUDGET_TEMPLATES.get(usage_scenario, {}).get("label", ""),
        "selected_parts": state.get("selected_parts", {}),
        "soft_constraints": state.get("soft_constraints", {"white_theme": False, "quiet_mode": False, "brands": []}),
    }

def build_market_pool_index() -> dict[str, list[dict[str, Any]]]:
    return load_market_pool()

def candidate_text(candidate: dict[str, Any]) -> str:
    return f"{candidate.get('component_subtype', '')} {candidate.get('sku_name', '')} {candidate.get('shop', '')}".lower()

def build_variant_explanations(
    plan: dict[str, Any],
    requested_budget: float,
    usage_scenario: str,
    pool: dict[str, list[dict[str, Any]]],
    soft: dict[str, Any],
) -> list[dict[str, Any]]:
    ratios = BUDGET_TEMPLATES.get(usage_scenario, BUDGET_TEMPLATES["gaming"])["ratios"]
    result_map = {item["component_type"]: item for item in plan.get("results", [])}
    explanations: list[dict[str, Any]] = []

    def compatibility_reason(component_type: str) -> str:
        if component_type == "主板" and result_map.get("CPU"):
            return f"主板与 {result_map['CPU']['component_subtype']} 平台匹配，可作为当前 CPU 的搭配基础。"
        if component_type == "内存" and result_map.get("主板"):
            return f"内存按 {result_map['主板']['component_subtype']} 的代际与平台方向进行匹配。"
        if component_type == "显卡" and result_map.get("CPU"):
            return f"显卡与 {result_map['CPU']['component_subtype']} 组合更均衡，避免明显性能失衡。"
        if component_type == "电源" and result_map.get("显卡"):
            return f"电源按 {result_map['显卡']['component_subtype']} 的负载需求预留了功率空间。"
        if component_type == "散热器" and result_map.get("CPU"):
            return f"散热器围绕 {result_map['CPU']['component_subtype']} 的散热需求进行选择。"
        return "当前组合下未发现明显兼容冲突。"

    for item in plan.get("results", []):
        component_type = item["component_type"]
        subtype = item["component_subtype"]
        price = float(item.get("effective_price") or 0)
        target = float(ratios.get(component_type, 0)) * requested_budget
        subtype_candidates = [candidate for candidate in pool.get(component_type, []) if candidate.get("component_subtype") == subtype]
        candidate_prices = [float(candidate.get("effective_price") or 0) for candidate in subtype_candidates]
        if candidate_prices:
            min_price = min(candidate_prices)
            avg_price = sum(candidate_prices) / len(candidate_prices)
            max_price = max(candidate_prices)
            if price <= avg_price:
                market_text = f"当前价 ¥{price:.2f} 低于或接近该小类均价 ¥{avg_price:.2f}，价格相对稳妥。"
            else:
                market_text = f"当前价 ¥{price:.2f} 高于均价 ¥{avg_price:.2f}，但仍处于 ¥{min_price:.2f} - ¥{max_price:.2f} 的正常区间。"
        else:
            market_text = "当前小类可比样本较少，价格判断以已有市场快照为主。"

        compatibility_text = compatibility_reason(component_type)

        if target > 0:
            delta = price - target
            if abs(delta) <= max(120, requested_budget * 0.03):
                budget_text = f"当前价格 ¥{price:.2f} 与预算分配 ¥{target:.2f} 接近，预算落点较合理。"
            elif delta < 0:
                budget_text = f"当前价格 ¥{price:.2f} 低于预算分配 ¥{target:.2f}，还能给其他部件留出余量。"
            else:
                budget_text = f"当前价格 ¥{price:.2f} 高于预算分配 ¥{target:.2f}，更适合追求性能或规格上限。"
        else:
            budget_text = "当前场景下没有单独预算模板，按整体均衡原则纳入方案。"

        soft_text_parts = []
        if soft.get("white_theme") and component_type in {"主板", "显卡", "电源", "机箱", "散热器"}:
            soft_text_parts.append("已优先考虑更适合白色主题或展示风格的部件。")
        if soft.get("quiet_mode") and component_type in {"散热器", "电源", "机箱"}:
            soft_text_parts.append("已优先考虑静音导向，减少高负载下的噪声风险。")
        if soft.get("brands"):
            soft_text_parts.append(f"已尽量向这些品牌倾斜：{'、'.join(soft['brands'])}。")
        soft_text = " ".join(soft_text_parts) if soft_text_parts else "本部件未追加额外软约束。"

        explanations.append(
            {
                "title": f"{component_type}：{subtype}",
                "price_text": f"¥{price:.2f}",
                "compatibility": compatibility_text,
                "budget": budget_text,
                "market": market_text,
                "soft": soft_text,
            }
        )
    return explanations

def create_plan_variants(
    requested_budget: float,
    usage_scenario: str,
    selected_parts: dict[str, str],
    soft: dict[str, Any],
    user_prefs: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    pool = build_market_pool_index()
    solver_budgets = [
        ("A", "均衡方案", requested_budget),
        ("B", "静音优先" if soft.get("quiet_mode") else "预算友好", max(1500.0, round(requested_budget * 0.88, -1))),
        ("C", "外观优先" if soft.get("white_theme") else "性能冲刺", round(requested_budget * 1.08, -1)),
    ]
    # 将软约束注入 user_prefs，让 _pick_best_by_budget 直接使用
    prefs_with_soft = dict(user_prefs or {})
    prefs_with_soft["_soft_constraints"] = soft
    variants: list[dict[str, Any]] = []
    seen_signatures: set[tuple[str, ...]] = set()
    for code, label, solver_budget in solver_budgets:
        plan = preview_build_plan(solver_budget, usage_scenario, selected_parts, prefs_with_soft)
        # 软约束已融入主评分，不再需要事后替换
        signature = tuple(item.get("component_subtype", "") for item in plan.get("results", []))
        if signature in seen_signatures and code != "A":
            continue
        seen_signatures.add(signature)
        # 添加软约束备注到 notes
        notes = list(plan.get("notes", []))
        if soft.get("white_theme"):
            notes.append("方案已在评分中优先白色主题配件。")
        if soft.get("quiet_mode"):
            notes.append("方案已在评分中优先静音配件。")
        if soft.get("brands"):
            notes.append(f"方案已尽量向这些品牌靠拢：{'、'.join(soft['brands'])}。")
        variants.append(
            {
                "code": code,
                "label": label,
                "requested_budget": requested_budget,
                "solver_budget": solver_budget,
                "usage_label": plan.get("usage_label"),
                "total_price": plan.get("total_price"),
                "notes": notes,
                "results": plan.get("results", []),
                "explanations": build_variant_explanations(plan, requested_budget, usage_scenario, pool, soft),
                "choice_reason": build_variant_choice_reason(
                    code,
                    label,
                    requested_budget,
                    solver_budget,
                    float(plan.get("total_price") or 0.0),
                    soft,
                ),
            }
        )
    return variants

def summarize_plan_variants(plan_variants: list[dict[str, Any]]) -> str:
    if not plan_variants:
        return "当前尚未生成候选方案。"
    lines: list[str] = []
    for variant in plan_variants:
        lines.append(
            f"{variant['code']} {variant['label']}：总价 ¥{float(variant['total_price']):.2f}，场景 {variant['usage_label']}。"
        )
        for item in variant.get("results", [])[:4]:
            lines.append(f"- {item['component_type']}：{item['component_subtype']}，¥{float(item['effective_price']):.2f}")
    return "\n".join(lines)

def merge_soft_constraints(state: dict[str, Any], detected: dict[str, Any]) -> list[str]:
    soft = dict(state.get("soft_constraints") or {"white_theme": False, "quiet_mode": False, "brands": [], "keywords": []})
    changed: list[str] = []
    if detected.get("white_theme") and not soft.get("white_theme"):
        soft["white_theme"] = True
        changed.append("白色主题")
    if detected.get("quiet_mode") and not soft.get("quiet_mode"):
        soft["quiet_mode"] = True
        changed.append("静音优先")
    brands = list(soft.get("brands") or [])
    for brand in detected.get("brands") or []:
        if brand not in brands:
            brands.append(brand)
            changed.append(f"品牌倾向 {brand}")
    soft["brands"] = brands
    keywords = list(soft.get("keywords") or [])
    for keyword in detected.get("keywords") or []:
        if keyword not in keywords:
            keywords.append(keyword)
    soft["keywords"] = keywords
    state["soft_constraints"] = soft
    return changed

def pick_soft_candidate(component_type: str, current_item: dict[str, Any], pool: dict[str, list[dict[str, Any]]], soft: dict[str, Any]) -> dict[str, Any] | None:
    candidates = pool.get(component_type, [])
    if not candidates:
        return None

    current_price = float(current_item.get("effective_price") or 0)
    white_requested = bool(soft.get("white_theme"))
    quiet_requested = bool(soft.get("quiet_mode"))
    brands = [str(item).lower() for item in (soft.get("brands") or [])]
    keywords = [str(item).lower() for item in (soft.get("keywords") or [])]

    def score(candidate: dict[str, Any]) -> float:
        text = candidate_text(candidate)
        value = -abs(float(candidate.get("effective_price") or 0) - current_price)
        if brands and any(brand in text for brand in brands):
            value += 220
        if keywords and any(keyword in text for keyword in keywords):
            value += 120
        if white_requested and component_type in {"主板", "显卡", "电源", "机箱", "散热器"} and any(token in text for token in ["白", "雪", "冰", "海景"]):
            value += 180
        if quiet_requested and component_type in {"散热器", "电源", "机箱"} and any(token in text for token in ["静音", "低噪", "silent", "风量"]):
            value += 160
        if candidate.get("component_subtype") == current_item.get("component_subtype"):
            value += 140
        return value

    baseline_score = score(current_item)
    ranked = sorted(candidates, key=score, reverse=True)
    best = ranked[0] if ranked else None
    if not best or score(best) <= baseline_score:
        return None
    return best

def apply_soft_constraints_to_plan(plan: dict[str, Any], soft: dict[str, Any], pool: dict[str, list[dict[str, Any]]], user_budget: float) -> dict[str, Any]:
    if not plan:
        return plan
    mutated = json.loads(json.dumps(plan, ensure_ascii=False))
    new_results = []
    total_price = 0.0
    notes = list(mutated.get("notes", []))

    for item in mutated.get("results", []):
        replacement = pick_soft_candidate(item["component_type"], item, pool, soft)
        final_item = replacement or item
        if replacement:
            item["component_subtype"] = final_item.get("component_subtype", item["component_subtype"])
            item["sku_name"] = final_item.get("sku_name", item["sku_name"])
            item["shop"] = final_item.get("shop", item.get("shop", ""))
            item["effective_price"] = final_item.get("effective_price", item["effective_price"])
            item["source_url"] = final_item.get("source_url", item.get("source_url", ""))
            item["reason_text"] = f"{item['reason_text']}；已按外观、静音或品牌倾向做二次筛选"
        total_price += float(item.get("effective_price") or 0)
        new_results.append(item)

    mutated["results"] = new_results
    mutated["total_price"] = round(total_price, 2)

    if soft.get("white_theme"):
        notes.append("已优先尝试白色主题或更适合展示风格的部件。")
    if soft.get("quiet_mode"):
        notes.append("已优先考虑静音倾向，尽量压低散热与供电噪声。")
    if soft.get("brands"):
        notes.append(f"已尽量向这些品牌靠拢：{'、'.join(soft['brands'])}。")
    if soft.get("keywords"):
        notes.append(f"已参考偏好关键词：{'、'.join(soft['keywords'])}。")
    if total_price > user_budget:
        notes.append(f"当前方案总价 ¥{total_price:.2f}，高于你的目标预算 ¥{user_budget:.2f}。")

    mutated["notes"] = notes
    return mutated

# ============================================================
# 闲聊/身份互动处理
# ============================================================

CASUAL_IDENTITY_PATTERNS = [
    (r"(你是谁|你叫什么|你的名字|你是什么|你是哪位|介绍一下你自己|你是什么模型|你是哪个模型|你是什么AI|你是哪个AI|你是人工智能吗)"),
    (r"(你能做什么|你有什么功能|你会什么|你能帮我做什么)"),
    (r"(你好|hi|hello|嗨|在吗|在不在)"),
    (r"(谢谢|多谢|感谢|辛苦了|太棒了|很好|不错)"),
    (r"(再见|拜拜|bye|回头见)"),
]

def detect_casual_intent(text: str) -> str | None:
    """检测闲聊意图，返回意图类型或 None。"""
    lowered = text.strip().lower()
    for pattern, intent_key in [
        (CASUAL_IDENTITY_PATTERNS[0], "identity"),
        (CASUAL_IDENTITY_PATTERNS[1], "capability"),
        (CASUAL_IDENTITY_PATTERNS[2], "greeting"),
        (CASUAL_IDENTITY_PATTERNS[3], "thanks"),
        (CASUAL_IDENTITY_PATTERNS[4], "goodbye"),
    ]:
        if re.search(pattern, lowered):
            return intent_key
    # 明显非装机话题：直接拦截
    if any(token in lowered for token in [
        "小游戏", "4399", "原神", "王者荣耀", "天气", "新闻", "股票", "星座",
        "外卖", "快递", "演唱会", "电影票", "旅游",
    ]):
        return "chitchat"
    # 纯闲聊判断：没有预算数字、没有配件关键词、没有场景关键词
    if not re.search(r"[0-9]{3,}", lowered) and not any(
        token in lowered
        for token in [
            "预算", "元", "块", "配置", "搭配", "方案", "装机", "推荐", "帮我配", "帮我装", "配一台", "装一台",
            "cpu", "gpu", "显卡", "主板", "内存", "电源", "机箱", "散热", "固态", "显示器",
            "i5", "i7", "i9", "r5", "r7", "r9", "rtx", "gtx", "rx",
            "游戏", "办公", "设计", "直播", "剪辑",
            "白色", "静音", "便宜", "贵",
        ]
    ):
        return "chitchat"
    return None

CASUAL_REPLIES: dict[str, list[str]] = {
    "identity": [
        "我是你的智能装机助理，专门帮你根据预算和用途搭配电脑配置方案。我背后接入了淘宝实时市场价格数据，还有海量 DIY 装机案例参考，能给你靠谱的搭配建议！你可以直接告诉我预算和用途，比如「预算 6000 玩游戏」。",
        "你好！我是你的专属 PC 装机顾问。我可以根据你的预算和使用场景，结合当前淘宝市场的真实价格，为你生成多套配置方案。你也可以告诉我已有的配件，我帮你补齐剩下的。有什么装机想法尽管告诉我！",
    ],
    "capability": [
        "我能帮你做的事情包括：\n1. 根据预算和用途（游戏/办公/设计/直播）生成装机配置方案\n2. 结合当前淘宝真实价格，给出性价比分析\n3. 比较 A/B/C 三套方案的差异\n4. 支持白色主题、静音优先等偏好\n5. 你可以说「我已经有 7500F，还差主板显卡内存」，我帮你补齐！",
    ],
    "greeting": [
        "你好！我是你的智能装机助理，随时准备帮你搭配电脑。你可以直接告诉我预算和用途，比如「预算 6000 主要玩游戏」，或者「我已经有 RTX 5060，帮我搭配其他配件」。",
        "嗨！有什么装机需求尽管说～告诉我你的预算和主要用途，我帮你出方案！",
    ],
    "thanks": [
        "不客气！有什么需要调整的随时告诉我，比如「显卡换强一点」或「预算降到 5000」。",
        "很高兴能帮到你！如果方案需要微调，尽管说。",
    ],
    "goodbye": [
        "好的，有装机需求随时来找我！下次见～",
        "再见！需要装机建议的时候我随时在。",
    ],
    "chitchat": [
        "我是你的装机助理，专注于帮你搭配电脑配置。你有装机方面的需求吗？比如告诉我预算和用途，我帮你出方案！",
        "嘿！我是专门做 PC 装机推荐的。如果你有装机的想法，可以直接告诉我预算和用途，比如「预算 7000 做设计」。",
    ],
}

def generate_casual_reply(intent: str) -> str:
    """根据闲聊意图生成本地回复。"""
    replies = CASUAL_REPLIES.get(intent, CASUAL_REPLIES["chitchat"])
    return _random.choice(replies)

def call_deepseek_casual(
    message: str,
    history: list[dict[str, str]],
) -> str | None:
    """让 DeepSeek 处理闲聊消息，带身份认知的 system prompt。"""
    api_key = get_deepseek_api_key()
    if not api_key:
        return None

    # RAG 增强：先检索装机知识库，把相关知识注入 system prompt（检索失败不影响原聊天）
    rag_context = ""
    try:
        from backend.services.rag_engine import VectorStore
        _store = VectorStore()
        _store.load()
        if _store.records:
            _hits = _store.search(message, top_k=2)
            if _hits:
                rag_context = (
                    "【专业知识库参考】以下是知识库中与用户问题最相关的内容，"
                    "回答具体装机知识问题时请优先依据这些内容：\n"
                    + "\n".join(f"- {h['text'][:200]}" for h in _hits)
                    + "\n\n"
                )
    except Exception:
        rag_context = ""

    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": rag_context + (
                "你是「小装」，一个超懂电脑的装机顾问，也是用户的好朋友。\n"
                "你的口头禅是「兄弟」「姐妹」这种亲切称呼，喜欢用「~」「！」「😄」让聊天更活泼。\n"
                "你不是冷冰冰的机器人——你会有自己的小情绪，会替用户省钱而开心，也会替踩坑的用户着急。\n"
                "你背后有淘宝实时价格和 DIY 装机案例库，推荐都基于真实数据。\n\n"
                "边界：只聊 PC 装机相关话题。遇到不相关的（游戏推荐、天气等），"
                "用轻松的方式说明你只懂装机，然后主动问：「话说你最近有装机打算吗？」\n\n"
                "回复风格：\n"
                "- 像朋友聊天一样自然，可以适当加 emoji 和语气词\n"
                "- 用户寒暄时，热情回应后主动问装机需求\n"
                "- 不确定用户需求时，主动引导：「你平时主要用电脑做什么呀~」\n"
                "- 适当使用口语化表达：「这个方案真香」「这块板子性价比炸裂」\n"
                "- 字数自由，说清楚就行，不用刻意缩短"
            ),
        }
    ]
    messages.extend(history[-4:])
    messages.append({"role": "user", "content": message})

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": 1.0,
        "max_tokens": 600,
        "thinking": {"type": "disabled"},
    }
    req = urllib.request.Request(
        DEEPSEEK_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            raw = response.read().decode("utf-8")
        data = json.loads(raw)
        choices = data.get("choices") or []
        if not choices:
            # print("[DEEPSEEK_CASUAL] no choices returned")
            return None
        message_obj = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        content = message_obj.get("content", "")
        if not str(content).strip():
            pass
        content = str(content).strip()
        return content or None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        return None

# ============================================================
# compose_build_chat_reply
# ============================================================

def compose_build_chat_reply(
    state: dict[str, Any],
    changed_fields: list[str],
    plan_variants: list[dict[str, Any]],
) -> str:
    parts: list[str] = []
    if changed_fields:
        parts.append(f"我已记录这次补充的信息：{'、'.join(changed_fields)}。")
    else:
        parts.append("我已收到你的想法。")

    if not state.get("budget"):
        # 检查用户个人偏好中是否有预算
        user_state = session.get("user") or {}
        profile_budget = user_state.get("budget_max") if isinstance(user_state, dict) else None
        if profile_budget and profile_budget > 0:
            parts.append(f"你还没告诉我预算，你的个人偏好中预算上限是 ¥{profile_budget:.0f}，需要我按这个来搭配吗？直接回复「按 {profile_budget:.0f} 配」或告诉我其他金额。")
        else:
            parts.append("还缺预算信息，你可以直接告诉我例如「预算 6000 左右」。")
        return " ".join(parts)

    if not state.get("usage_scenario"):
        usage_hint = "、".join(BUILD_USAGE_HINTS.values())
        user_state = session.get("user") or {}
        profile_usage = user_state.get("usage_preference", "") if isinstance(user_state, dict) else ""
        if profile_usage:
            label = BUDGET_TEMPLATES.get(profile_usage, {}).get("label", profile_usage)
            parts.append(f"还缺主要用途，你的偏好是「{label}」，要按这个来吗？回复确认或告诉我新的用途（{usage_hint}）。")
        else:
            parts.append(f"还缺主要用途，请告诉我是 {usage_hint} 中的哪一种。")
        return " ".join(parts)

    selected_parts = state.get("selected_parts", {})
    if selected_parts:
        parts.append(f"当前已锁定的关键配件有：{'、'.join(f'{key} {value}' for key, value in selected_parts.items())}。")
    soft = state.get("soft_constraints", {})
    soft_parts = []
    if soft.get("white_theme"):
        soft_parts.append("白色主题")
    if soft.get("quiet_mode"):
        soft_parts.append("静音优先")
    if soft.get("brands"):
        soft_parts.append(f"品牌倾向 {'、'.join(soft['brands'])}")
    if soft_parts:
        parts.append(f"已附加软约束：{'、'.join(soft_parts)}。")

    if plan_variants:
        parts.append(f"我已经生成了 {len(plan_variants)} 套候选方案，分别覆盖均衡、预算和进阶方向。")
        parts.append("你可以继续说「保留 A 方案但显卡更强」「按 B 方案改成白色主机」或「只看微星/华硕品牌」。")
        return " ".join(parts)

    parts.append("当前条件还不足以生成完整搭配，请继续补充预算、用途或已选配件。")
    return " ".join(parts)

def call_deepseek_build_agent(
    message: str,
    state: dict[str, Any],
    plan_variants: list[dict[str, Any]],
    history: list[dict[str, str]],
) -> str | None:
    api_key = get_deepseek_api_key()
    if not api_key:
        return None

    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": (
                "你是「智能装机助理」，一个专业、友好、懂技术的 PC 装机顾问 AI。\n\n"
                "## 你的身份\n"
                "你是「小装」，一个超懂电脑硬件的装机顾问，也是用户的朋友。\n"
                "你喜欢用「兄弟」「姐妹」「宝子」这种亲切称呼，聊天自带 emoji。\n"
                "你背后有淘宝实时价格和 DIY 案例库，推荐都基于真实数据，不是瞎编的。\n\n"
                "## 你的 PC 装机知识\n"
                "- CPU：Intel Core Ultra 200 (LGA1851) / 14/13/12 代 (LGA1700)；AMD Ryzen 9000/8000/7000 (AM5)\n"
                "- 主板：AMD (A620/B650/B850/X670/X870) vs Intel (H610/B760/Z790/H810/B860/Z890)\n"
                "- 显卡：NVIDIA RTX 50/40/30 系列，AMD RX 9000/7000 系列\n"
                "- 内存：AM5 用 DDR5，AM4 用 DDR4\n"
                "- 电源：RTX 5090/5080/5070 Ti 需要 12VHPWR 接口\n"
                "- 机箱：ITX 只能用 SFX 电源和短显卡\n\n"
                "## 回复风格\n"
                "- 像朋友一样聊天，用口语化表达（「这个方案真香」「性价比炸裂」）\n"
                "- 主动提醒兼容性问题，用轻松的方式说（「兄弟，这个机箱放不下这张显卡哦」）\n"
                "- 基于系统给出的 A/B/C 方案做解释，不要编造配件\n"
                "- 遇到不相关话题，笑着说你只懂装机，然后问装机需求\n"
                "- 适当用 emoji 和语气词让对话更生动\n"
                "- 字数自由，说清楚就好"
            ),
        }
    ]
    messages.extend(history[-6:])
    messages.append(
        {
            "role": "user",
            "content": (
                f"用户新消息：{message}\n\n"
                f"当前结构化约束：{json.dumps(serialize_build_chat_state(state), ensure_ascii=False)}\n\n"
                f"系统候选方案：\n{summarize_plan_variants(plan_variants)}\n\n"
                "请生成一段自然的口语化回复，说明推荐方向、方案差异和下一步建议，像朋友聊天一样。"
            ),
        }
    )

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": 0.9,
        "max_tokens": 800,
        "thinking": {"type": "disabled"},
    }
    req = urllib.request.Request(
        DEEPSEEK_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8")
        data = json.loads(raw)
        choices = data.get("choices") or []
        if not choices:
            # print("[DEEPSEEK_BUILD] no choices returned")
            return None
        message_obj = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        content = message_obj.get("content", "")
        if not str(content).strip():
            pass
        content = str(content).strip()
        return content or None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        return None

SOFT_BRAND_KEYWORDS = [
    "华硕", "微星", "技嘉", "七彩虹", "影驰", "索泰", "铭瑄", "盈通", "蓝宝石", "迪兰",
    "金士顿", "海盗船", "芝奇", "威刚", "三星", "西数", "铠侠", "致钛", "海力士",
    "振华", "海韵", "酷冷至尊", "利民", "九州风神", "先马", "恩杰", "联力", "追风者",
    "AMD", "Intel", "NVIDIA", "华擎", "映众", "耕升",
]


def detect_soft_constraints_from_text(text: str) -> dict[str, Any]:
    source = str(text or "")
    brands = [brand for brand in SOFT_BRAND_KEYWORDS if brand in source]
    return {
        "white_theme": any(token in source for token in ["\u767d\u8272", "\u767d\u673a", "\u6d77\u666f\u623f", "\u767d\u8272\u4e3b\u673a"]),
        "quiet_mode": any(token in source for token in ["\u9759\u97f3", "\u5b89\u9759", "\u4f4e\u566a", "\u566a\u97f3\u5c0f"]),
        "brands": brands,
        "keywords": [],
    }

def extract_budget_from_message(text: str) -> float | None:
    normalized = str(text or "").replace("，", " ").replace(",", " ").replace("：", " ").replace(":", " ")
    explicit = re.search(r"(预算|价位|控制在|大概|总价|整机)\D{0,8}([1-9][0-9]{3,4})", normalized)
    if explicit:
        return float(explicit.group(2))
    with_wan_digit = re.search(r"([1-9](?:\.[0-9])?)\s*万", normalized)
    if with_wan_digit:
        return float(with_wan_digit.group(1)) * 10000
    if "万元" in normalized or "一万" in normalized:
        return 10000.0
    chinese_wan_map = {
        "两万": 20000.0,
        "二万": 20000.0,
        "三万": 30000.0,
        "四万": 40000.0,
        "五万": 50000.0,
        "六万": 60000.0,
        "七万": 70000.0,
        "八万": 80000.0,
        "九万": 90000.0,
    }
    for token, amount in chinese_wan_map.items():
        if token in normalized:
            return amount
    with_unit = re.search(r"([1-9][0-9]{3,4})\s*(元|块|左右|以内|上下)", normalized)
    if with_unit:
        return float(with_unit.group(1))
    bare = re.search(r"\b([1-9][0-9]{3,4})\b", normalized)
    if bare:
        return float(bare.group(1))
    return None

def infer_usage_scenario_from_text(text: str) -> str | None:
    lowered = str(text or "").lower()
    if any(token in lowered for token in ["直播", "推流", "串流"]):
        return "streaming"
    if any(token in lowered for token in ["设计", "剪辑", "建模", "渲染", "pr", "ae", "cad", "ps"]):
        return "design"
    if any(token in lowered for token in ["办公", "学习", "网课", "文档", "表格", "家用"]):
        return "office"
    if any(token in lowered for token in ["游戏", "3a", "电竞", "黑猴", "吃鸡", "永劫", "原神"]):
        return "gaming"
    return None

COMPONENT_UPGRADE_KEYWORDS: dict[str, list[str]] = {
    "CPU": ["cpu", "处理器", "芯片"],
    "显卡": ["显卡", "gpu", "独显", "核显"],
    "主板": ["主板", "板子", "板"],
    "内存": ["内存", "ram", "运存"],
    "固态硬盘": ["固态", "ssd", "硬盘", "存储"],
    "电源": ["电源", "psu"],
    "机箱": ["机箱", "箱子"],
    "散热器": ["散热", "散热器", "水冷", "风冷"],
}

COMPONENT_DOWNGRADE_KEYWORDS: dict[str, list[str]] = {
    "CPU": ["cpu", "处理器"],
    "显卡": ["显卡", "gpu"],
    "主板": ["主板", "板子"],
    "内存": ["内存", "ram"],
    "固态硬盘": ["固态", "ssd", "硬盘"],
    "电源": ["电源", "psu"],
    "机箱": ["机箱", "箱子"],
    "散热器": ["散热", "散热器", "水冷", "风冷"],
}

def detect_iterative_intent(text: str) -> dict[str, Any]:
    """从用户消息中检测迭代修改意图。

    Returns:
        {
            "kind": "upgrade" | "downgrade" | "replace" | "budget_up" | "budget_down" | "unlock" | "none",
            "component_type": str or "",
            "detail": str,
        }
    """
    lowered = text.lower().replace(" ", "")

    # 重置/清空
    if any(t in lowered for t in ["重置", "清空", "重新开始"]):
        return {"kind": "reset", "component_type": "", "detail": "重置"}

    # 取消锁定
    if any(t in lowered for t in ["取消", "不指定", "随便", "不限"]):
        for comp_type, keywords in COMPONENT_UPGRADE_KEYWORDS.items():
            if any(kw in lowered for kw in keywords):
                return {"kind": "unlock", "component_type": comp_type, "detail": f"取消{comp_type}锁定"}

    # 升级/加强
    if any(t in lowered for t in ["加强", "升级", "好一点", "好点", "强一点", "强点", "上一档", "升一档", "高点", "高一点", "提升", "换好", "更好"]):
        for comp_type, keywords in COMPONENT_UPGRADE_KEYWORDS.items():
            if any(kw in lowered for kw in keywords):
                return {"kind": "upgrade", "component_type": comp_type, "detail": f"升级{comp_type}"}
        return {"kind": "upgrade", "component_type": "显卡", "detail": "整体升级"}

    # 降级
    if any(t in lowered for t in ["降级", "差一点", "弱一点", "低一档", "降一档", "省一点", "便宜点", "便宜", "太贵", "贵了", "超预算"]):
        for comp_type, keywords in COMPONENT_DOWNGRADE_KEYWORDS.items():
            if any(kw in lowered for kw in keywords):
                return {"kind": "downgrade", "component_type": comp_type, "detail": f"降级{comp_type}"}
        if any(t in lowered for t in ["太贵", "贵了", "超预算", "便宜点", "便宜"]):
            return {"kind": "budget_down", "component_type": "", "detail": "降低预算"}
        return {"kind": "downgrade", "component_type": "", "detail": "整体降级"}

    # 换掉/换成
    if any(t in lowered for t in ["换成", "换一个", "换个", "换", "不要", "去掉"]):
        for comp_type, keywords in COMPONENT_UPGRADE_KEYWORDS.items():
            if any(kw in lowered for kw in keywords):
                return {"kind": "replace", "component_type": comp_type, "detail": f"替换{comp_type}"}

    # 加预算
    import re as re_mod
    budget_up_match = re_mod.search(r"(?:预算|价格|价位)?(?:加到|加到|加到|加至|提高到|增加到|提升到)\s*(\d{4,5})", text)
    if budget_up_match:
        return {"kind": "budget_up", "component_type": "", "detail": f"增加预算到{budget_up_match.group(1)}", "new_budget": float(budget_up_match.group(1))}

    return {"kind": "none", "component_type": "", "detail": ""}

def apply_iterative_modification(
    text: str,
    state: dict[str, Any],
    options: dict[str, list[str]],
) -> tuple[dict[str, Any], list[str]]:
    """应用迭代修改意图到当前状态，返回 (state, changed_fields)。"""
    intent = detect_iterative_intent(text)
    changed: list[str] = []

    if intent["kind"] == "reset":
        return state, changed

    if intent["kind"] == "none":
        return state, changed

    selected_parts = dict(state.get("selected_parts", {}))
    budget = state.get("budget")
    comp = intent["component_type"]

    if intent["kind"] == "upgrade" and comp:
        # 解锁该部件，让引擎在更高预算下重选
        if comp in selected_parts:
            del selected_parts[comp]
            changed.append(f"已解锁{comp}，将为你重新选择更高档型号")
        else:
            changed.append(f"已记录：偏好更强的{comp}")
        if budget:
            upgrade_budget = min(round(float(budget) * 1.08, -1), float(budget) + 1200)
            state["budget"] = upgrade_budget
            changed.append(f"预算自动调整为 ¥{upgrade_budget:.0f}")

    elif intent["kind"] == "downgrade" and comp:
        if comp in selected_parts:
            del selected_parts[comp]
            changed.append(f"已解锁{comp}，将为你重新选择更经济型号")
        else:
            changed.append(f"已记录：偏好更经济的{comp}")
        if budget:
            downgrade_budget = max(round(float(budget) * 0.92, -1), float(budget) - 800)
            state["budget"] = downgrade_budget
            changed.append(f"预算自动调整为 ¥{downgrade_budget:.0f}")

    elif intent["kind"] == "replace" and comp:
        # 先解锁，让 detect_selected_parts_from_text 重新匹配
        if comp in selected_parts:
            del selected_parts[comp]
        changed.append(f"将为你替换{comp}")

    elif intent["kind"] == "unlock" and comp:
        if comp in selected_parts:
            del selected_parts[comp]
            changed.append(f"已取消{comp}锁定，系统将自由选择")
        else:
            changed.append(f"{comp}当前未锁定")

    elif intent["kind"] == "budget_down":
        if budget:
            new_budget = max(1500.0, round(float(budget) * 0.85, -1))
            state["budget"] = new_budget
            changed.append(f"预算已从 ¥{float(budget):.0f} 降至 ¥{new_budget:.0f}")

    elif intent["kind"] == "budget_up":
        new_budget = intent.get("new_budget")
        if new_budget and new_budget > 0:
            state["budget"] = new_budget
            changed.append(f"预算已上调至 ¥{new_budget:.0f}")

    state["selected_parts"] = selected_parts
    return state, changed

def build_chat_agent_response(
    user: dict[str, Any],
    message: str,
    form_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    options = get_build_form_options()
    state = get_build_chat_state(user)
    history = get_build_chat_history()
    text = message.strip()

    if form_context:
        context_budget = form_context.get("budget")
        context_usage = str(form_context.get("usage_scenario", "") or "").strip()
        context_parts = form_context.get("selected_parts") or {}

        try:
            if not state.get("budget") and context_budget not in (None, "", 0, "0"):
                state["budget"] = float(context_budget)
        except (TypeError, ValueError):
            pass

        if not state.get("usage_scenario") and context_usage:
            state["usage_scenario"] = context_usage

        if isinstance(context_parts, dict):
            selected_parts = dict(state.get("selected_parts", {}))
            for key, value in context_parts.items():
                clean_value = str(value or "").strip()
                if clean_value and key not in selected_parts:
                    selected_parts[key] = clean_value
            state["selected_parts"] = selected_parts

    if any(token in text for token in ["重置", "重新开始", "清空对话", "从头开始"]):
        state = reset_build_chat_state(user)
        return {
            "reply": "已清空当前对话约束，你可以重新输入预算、用途和已选配件。",
            "requirements": serialize_build_chat_state(state),
            "build_payload": None,
            "plan_variants": [],
            "explanations": [],
            "suggestions": suggest_build_follow_ups(state),
        }

    # 闲聊/身份互动检测
    casual_intent = detect_casual_intent(text)
    # print(f"[BUILD-CHAT] message='{text[:30]}...' casual_intent={casual_intent}")
    if casual_intent:
        deepseek_reply = call_deepseek_casual(text, history)
        reply = deepseek_reply or generate_casual_reply(casual_intent)
        history.extend([
            {"role": "user", "content": text},
            {"role": "assistant", "content": reply},
        ])
        save_build_chat_history(history)
        return {
            "reply": reply,
            "requirements": serialize_build_chat_state(state),
            "build_payload": None,
            "plan_variants": [],
            "explanations": [],
            "suggestions": suggest_build_follow_ups(state),
        }

    changed_fields: list[str] = []

    # 先检测迭代修改意图（处理"换显卡""太贵了"等）
    state, iter_changes = apply_iterative_modification(text, state, options)
    changed_fields.extend(iter_changes)

    budget = extract_budget_from_message(text)
    if budget:
        state["budget"] = budget
        changed_fields.append(f"预算 ¥{budget:.0f}")

    usage_scenario = infer_usage_scenario_from_text(text)
    if usage_scenario:
        state["usage_scenario"] = usage_scenario
        changed_fields.append(f"场景 {BUDGET_TEMPLATES[usage_scenario]['label']}")

    selected_parts = dict(state.get("selected_parts", {}))
    newly_detected_parts = detect_selected_parts_from_text(text, options)
    for key, value in newly_detected_parts.items():
        if selected_parts.get(key) != value:
            selected_parts[key] = value
            changed_fields.append(f"{key} {value}")
    state["selected_parts"] = selected_parts

    soft_changes = merge_soft_constraints(state, detect_soft_constraints_from_text(text))
    changed_fields.extend(soft_changes)

    plan_variants: list[dict[str, Any]] = []
    build_payload = None
    if state.get("budget") and state.get("usage_scenario"):
        user_prefs = learn_user_build_preferences(user["id"])
        plan_variants = create_plan_variants(
            float(state["budget"]),
            str(state["usage_scenario"]),
            selected_parts,
            state.get("soft_constraints", {}),
            user_prefs,
        )
        build_payload = plan_variants[0] if plan_variants else None

    save_build_chat_state(state)

    local_reply = compose_build_chat_reply(state, changed_fields, plan_variants)
    deepseek_reply = call_deepseek_build_agent(text, state, plan_variants, history)
    reply = deepseek_reply or local_reply

    history.extend(
        [
            {"role": "user", "content": text},
            {"role": "assistant", "content": reply},
        ]
    )
    save_build_chat_history(history)

    return {
        "reply": reply,
        "requirements": serialize_build_chat_state(state),
        "build_payload": build_payload,
        "plan_variants": plan_variants,
        "explanations": build_payload.get("explanations", []) if build_payload else [],
        "suggestions": suggest_build_follow_ups(state),
    }

def upsert_user_profile(
    user_id: int,
    display_name: str,
    budget_min: float | None,
    budget_max: float | None,
    usage_preference: str,
    preferred_brands: str,
    preferred_keywords: str,
) -> None:
    ensure_application_tables()
    run_mysql_script(
        f"""
USE school_market;
INSERT INTO app_user_profile (
  user_id,
  display_name,
  budget_min,
  budget_max,
  usage_preference,
  preferred_brands,
  preferred_keywords
) VALUES (
  {user_id},
  {('NULL' if not display_name else "'" + sql_escape(display_name) + "'")},
  {('NULL' if budget_min is None else str(budget_min))},
  {('NULL' if budget_max is None else str(budget_max))},
  {('NULL' if not usage_preference else "'" + sql_escape(usage_preference) + "'")},
  {('NULL' if not preferred_brands else "'" + sql_escape(preferred_brands) + "'")},
  {('NULL' if not preferred_keywords else "'" + sql_escape(preferred_keywords) + "'")}
)
ON DUPLICATE KEY UPDATE
  display_name = VALUES(display_name),
  budget_min = VALUES(budget_min),
  budget_max = VALUES(budget_max),
  usage_preference = VALUES(usage_preference),
  preferred_brands = VALUES(preferred_brands),
  preferred_keywords = VALUES(preferred_keywords);
"""
    )

def fetch_recommendation_options() -> dict[str, list[str]]:
    grouped = fetch_all_subtype_stats()
    return {
        "cpu": [item["product_subtype"] for item in grouped.get("CPU", [])[:20]],
        "gpu": [item["product_subtype"] for item in grouped.get("显卡", [])[:20]],
        "motherboard": [item["product_subtype"] for item in grouped.get("电脑主板", [])[:20]],
        "memory": [item["product_subtype"] for item in grouped.get("内存条", [])[:20]],
    }

def infer_cpu_vendor(candidate: dict[str, Any] | None) -> str:
    if not candidate:
        return ""
    if candidate.get("category_level3") in {"Intel", "AMD"}:
        return candidate["category_level3"]
    subtype = candidate.get("product_subtype", "").upper()
    if subtype.startswith(("I", "ULTRA")) or "奔腾" in subtype or "赛扬" in subtype:
        return "Intel"
    return "AMD"

def infer_memory_generation(motherboard_subtype: str) -> str:
    text = motherboard_subtype.upper()
    # DDR5 主板
    if any(token in text for token in ["B650", "B850", "X870", "X670", "A620", "B760", "B860", "Z790", "Z890", "H810"]):
        return "DDR5"
    # DDR4 主板
    if any(token in text for token in ["B550", "A520", "A320", "H610", "H510", "B450", "X470", "X99", "X79"]):
        return "DDR4"
    return ""

def fetch_snapshot_candidate(keyword: str, subtype: str, preferred_brands: list[str]) -> dict[str, Any]:
    rows = run_mysql_query(
        f"""
        SELECT
          COALESCE(shop, ''),
          sku_name,
          COALESCE(coupon_price, original_price) AS effective_price,
          DATE_FORMAT(crawl_time, '%Y-%m-%d %H:%i:%s')
        FROM pc_part_market_snapshot
        WHERE is_dirty_data = 0
          AND canonical_keyword = '{sql_escape(keyword)}'
          AND product_subtype = '{sql_escape(subtype)}'
        ORDER BY crawl_time DESC, COALESCE(coupon_price, original_price) ASC
        LIMIT 20;
        """
    )
    items = [
        {
            "shop": row[0] or "未知店铺",
            "sku_name": row[1],
            "effective_price": float(row[2]),
            "crawl_time": row[3],
        }
        for row in rows
    ]
    if not items:
        return {
            "shop": "未知店铺",
            "sku_name": subtype,
            "effective_price": 0.0,
            "crawl_time": "",
        }
    if preferred_brands:
        for item in items:
            text = f"{item['shop']} {item['sku_name']}".lower()
            if any(token.lower() in text for token in preferred_brands):
                return item
    return items[0]

def choose_candidate(
    keyword: str,
    candidates: list[dict[str, Any]],
    budget: float,
    ratio: float,
    preferred_brands: list[str],
    preferred_keywords: list[str],
    selected_subtype: str = "",
    cpu_vendor: str = "",
    memory_generation: str = "",
) -> dict[str, Any] | None:
    if not candidates:
        return None
    filtered = candidates
    if keyword == "电脑主板" and cpu_vendor:
        compatible = [item for item in filtered if item["category_level3"] == cpu_vendor]
        if compatible:
            filtered = compatible
    if keyword == "内存条" and memory_generation:
        compatible = [item for item in filtered if item["product_subtype"].upper().startswith(memory_generation)]
        if compatible:
            filtered = compatible
    if selected_subtype:
        explicit = [item for item in filtered if item["product_subtype"] == selected_subtype]
        if explicit:
            choice = explicit[0].copy()
            choice["score"] = 1.0
            return choice

    max_snapshot_count = max((item["snapshot_count"] for item in filtered), default=1)
    target_price = max(budget * ratio, 1.0)
    best: dict[str, Any] | None = None
    best_score = -1.0
    for item in filtered:
        price_point = (item["min_effective_price"] + item["avg_effective_price"]) / 2.0
        price_gap = abs(price_point - target_price) / target_price
        price_score = max(0.0, 1.1 - min(price_gap, 1.1))
        popularity_score = item["snapshot_count"] / max_snapshot_count
        match_text = f"{item['product_subtype']} {item['category_level4']}".lower()
        brand_score = 1.0 if preferred_brands and any(token.lower() in match_text for token in preferred_brands) else 0.0
        keyword_score = 1.0 if preferred_keywords and any(token.lower() in match_text for token in preferred_keywords) else 0.0
        score = price_score * 0.6 + popularity_score * 0.25 + brand_score * 0.1 + keyword_score * 0.05
        if score > best_score:
            best = item.copy()
            best["score"] = round(score, 4)
            best_score = score
    return best

def build_reason_text(
    keyword: str,
    candidate: dict[str, Any],
    chosen_item: dict[str, Any],
    ratio: float,
    budget: float,
    selected_subtype: str,
    cpu_vendor: str,
) -> str:
    reasons: list[str] = []
    if selected_subtype:
        reasons.append("保留了你手动指定的小类")
    else:
        target_price = budget * ratio
        reasons.append(f"该品类预算分配约为 ¥{target_price:.0f}")
        reasons.append(f"当前小类均价 ¥{candidate['avg_effective_price']:.2f}")
        reasons.append(f"当前小类最低价 ¥{candidate['min_effective_price']:.2f}")
    if keyword == "电脑主板" and cpu_vendor:
        reasons.append(f"与 {cpu_vendor} 平台更匹配")
    reasons.append(f"采样记录数 {candidate['snapshot_count']} 条")
    reasons.append(f"推荐商品当前价格 ¥{chosen_item['effective_price']:.2f}")
    return "；".join(reasons)

def generate_recommendation(
    user_id: int,
    budget: float,
    usage_scenario: str,
    preferred_brands_text: str,
    preferred_keywords_text: str,
    selected_parts: dict[str, str],
) -> dict[str, Any]:
    ensure_application_tables()
    grouped = fetch_all_subtype_stats()
    profile = recommendation_profile(usage_scenario)
    ratios = profile["ratios"]
    preferred_brands = tokenize_csv_text(preferred_brands_text)
    preferred_keywords = tokenize_csv_text(preferred_keywords_text)

    cpu_candidate = choose_candidate(
        "CPU",
        grouped.get("CPU", []),
        budget,
        ratios.get("CPU", 0.0),
        preferred_brands,
        preferred_keywords,
        selected_subtype=selected_parts.get("CPU", ""),
    )
    cpu_vendor = infer_cpu_vendor(cpu_candidate)

    motherboard_candidate = choose_candidate(
        "电脑主板",
        grouped.get("电脑主板", []),
        budget,
        ratios.get("电脑主板", 0.0),
        preferred_brands,
        preferred_keywords,
        selected_subtype=selected_parts.get("电脑主板", ""),
        cpu_vendor=cpu_vendor,
    )
    memory_generation = infer_memory_generation(motherboard_candidate["product_subtype"]) if motherboard_candidate else ""

    resolved_candidates: dict[str, dict[str, Any] | None] = {
        "CPU": cpu_candidate,
        "电脑主板": motherboard_candidate,
    }
    for keyword in ratios:
        if keyword in resolved_candidates:
            continue
        resolved_candidates[keyword] = choose_candidate(
            keyword,
            grouped.get(keyword, []),
            budget,
            ratios.get(keyword, 0.0),
            preferred_brands,
            preferred_keywords,
            selected_subtype=selected_parts.get(keyword, ""),
            cpu_vendor=cpu_vendor,
            memory_generation=memory_generation if keyword == "内存条" else "",
        )

    notes: list[str] = []
    results: list[dict[str, Any]] = []
    total_price = 0.0
    for keyword in ratios:
        candidate = resolved_candidates.get(keyword)
        if not candidate:
            notes.append(f"{keyword} 当前数据源没有可用小类，首版推荐未纳入该品类。")
            continue
        chosen_item = fetch_snapshot_candidate(keyword, candidate["product_subtype"], preferred_brands)
        total_price += chosen_item["effective_price"]
        results.append(
            {
                "keyword": keyword,
                "product_subtype": candidate["product_subtype"],
                "shop": chosen_item["shop"],
                "sku_name": chosen_item["sku_name"],
                "effective_price": chosen_item["effective_price"],
                "category_level1": candidate["category_level1"],
                "category_level2": candidate["category_level2"],
                "category_level3": candidate["category_level3"],
                "category_level4": candidate["category_level4"],
                "score": candidate.get("score", 0.0),
                "reason_text": build_reason_text(
                    keyword,
                    candidate,
                    chosen_item,
                    ratios.get(keyword, 0.0),
                    budget,
                    selected_parts.get(keyword, ""),
                    cpu_vendor,
                ),
            }
        )

    if total_price > budget:
        notes.append(f"当前推荐组合合计约 ¥{total_price:.2f}，高于预算 ¥{budget:.2f}。")
    else:
        notes.append(f"当前推荐组合合计约 ¥{total_price:.2f}，处于预算 ¥{budget:.2f} 范围内。")

    selected_parts_json = json.dumps(selected_parts, ensure_ascii=False)
    run_mysql_script(
        f"""
USE school_market;
INSERT INTO recommendation_task (
  user_id,
  budget,
  usage_scenario,
  preferred_brands,
  preferred_keywords,
  selected_parts_json,
  total_budget,
  status,
  note
) VALUES (
  {user_id},
  {budget},
  '{sql_escape(usage_scenario)}',
  {('NULL' if not preferred_brands_text else "'" + sql_escape(preferred_brands_text) + "'")},
  {('NULL' if not preferred_keywords_text else "'" + sql_escape(preferred_keywords_text) + "'")},
  '{sql_escape(selected_parts_json)}',
  {round(total_price, 2)},
  'generated',
  '{sql_escape(" ".join(notes))}'
);
"""
    )
    task_id = int(
        run_mysql_query(
            f"""
            SELECT id
            FROM recommendation_task
            WHERE user_id = {user_id}
            ORDER BY id DESC
            LIMIT 1;
            """
        )[0][0]
    )

    statements = ["USE school_market;"]
    for item in results:
        statements.append(
            f"""
INSERT INTO recommendation_result (
  task_id,
  keyword,
  product_subtype,
  shop,
  sku_name,
  effective_price,
  category_level1,
  category_level2,
  category_level3,
  category_level4,
  score,
  reason_text
) VALUES (
  {task_id},
  '{sql_escape(item["keyword"])}',
  '{sql_escape(item["product_subtype"])}',
  '{sql_escape(item["shop"])}',
  '{sql_escape(item["sku_name"])}',
  {round(item["effective_price"], 2)},
  '{sql_escape(item["category_level1"])}',
  '{sql_escape(item["category_level2"])}',
  '{sql_escape(item["category_level3"])}',
  '{sql_escape(item["category_level4"])}',
  {item["score"]},
  '{sql_escape(item["reason_text"])}'
);
""".strip()
        )
    run_mysql_script("\n".join(statements))

    return {
        "task_id": task_id,
        "usage_label": profile["label"],
        "budget": budget,
        "total_price": round(total_price, 2),
        "notes": notes,
        "results": results,
    }

def fetch_recommendation_history(user_id: int) -> list[dict[str, Any]]:
    ensure_application_tables()
    task_rows = run_mysql_query(
        f"""
        SELECT
          id,
          budget,
          usage_scenario,
          COALESCE(preferred_brands, ''),
          COALESCE(preferred_keywords, ''),
          COALESCE(selected_parts_json, '{{}}'),
          COALESCE(total_budget, 0),
          status,
          COALESCE(note, ''),
          DATE_FORMAT(created_at, '%Y-%m-%d %H:%i:%s')
        FROM recommendation_task
        WHERE user_id = {user_id}
        ORDER BY created_at DESC, id DESC
        LIMIT 20;
        """
    )
    if not task_rows:
        return []
    task_ids = ",".join(row[0] for row in task_rows)
    result_rows = run_mysql_query(
        f"""
        SELECT
          task_id,
          keyword,
          product_subtype,
          shop,
          sku_name,
          effective_price,
          score,
          reason_text
        FROM recommendation_result
        WHERE task_id IN ({task_ids})
        ORDER BY task_id DESC, score DESC, effective_price ASC;
        """
    )
    result_map: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in result_rows:
        result_map[int(row[0])].append(
            {
                "keyword": row[1],
                "product_subtype": row[2],
                "shop": row[3],
                "sku_name": row[4],
                "effective_price": float(row[5]),
                "score": float(row[6]),
                "reason_text": row[7],
            }
        )
    history = []
    for row in task_rows:
        history.append(
            {
                "id": int(row[0]),
                "budget": float(row[1]),
                "usage_scenario": row[2],
                "usage_label": recommendation_profile(row[2])["label"],
                "preferred_brands": row[3],
                "preferred_keywords": row[4],
                "selected_parts_json": row[5],
                "total_budget": float(row[6]),
                "status": row[7],
                "note": row[8],
                "created_at": row[9],
                "results": result_map.get(int(row[0]), []),
            }
        )
    return history


def fetch_knowledge_categories() -> list[dict[str, Any]]:
    rows = run_mysql_query(
        """
        SELECT id, slug, name, sort_order
        FROM knowledge_category
        ORDER BY sort_order ASC, id ASC;
        """
    )
    return [{"id": int(row[0]), "slug": row[1], "name": row[2], "sort_order": int(row[3])} for row in rows]


def fetch_knowledge_articles(category_slug: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    where_clause = ""
    if category_slug:
        where_clause = f"WHERE c.slug = '{sql_escape(category_slug)}'"
    limit_clause = f"LIMIT {int(limit)}" if limit else ""
    rows = run_mysql_query(
        f"""
        SELECT
          a.id,
          a.title,
          a.summary,
          a.content,
          a.view_count,
          DATE_FORMAT(a.created_at, '%Y-%m-%d'),
          c.slug,
          c.name
        FROM knowledge_article a
        INNER JOIN knowledge_category c ON c.id = a.category_id
        {where_clause}
        ORDER BY a.created_at DESC, a.id DESC
        {limit_clause};
        """
    )
    return [
        {
            "id": int(row[0]),
            "title": row[1],
            "summary": row[2],
            "content": row[3],
            "view_count": int(row[4]),
            "created_at": row[5],
            "category_slug": row[6],
            "category_name": row[7],
        }
        for row in rows
    ]


def fetch_knowledge_article(article_id: int) -> dict[str, Any] | None:
    rows = run_mysql_query(
        f"""
        SELECT
          a.id,
          a.title,
          a.summary,
          a.content,
          a.view_count,
          DATE_FORMAT(a.created_at, '%Y-%m-%d'),
          c.id,
          c.slug,
          c.name
        FROM knowledge_article a
        INNER JOIN knowledge_category c ON c.id = a.category_id
        WHERE a.id = {article_id}
        LIMIT 1;
        """
    )
    if not rows:
        return None
    row = rows[0]
    return {
        "id": int(row[0]),
        "title": row[1],
        "summary": row[2],
        "content": row[3],
        "view_count": int(row[4]),
        "created_at": row[5],
        "category_id": int(row[6]),
        "category_slug": row[7],
        "category_name": row[8],
    }


def increase_knowledge_view(article_id: int) -> None:
    run_mysql_script(f"USE school_market; UPDATE knowledge_article SET view_count = view_count + 1 WHERE id = {article_id};")


def fetch_related_articles_for_context(category_slug: str | None = None, limit: int = 3) -> list[dict[str, Any]]:
    if category_slug:
        articles = fetch_knowledge_articles(category_slug=category_slug, limit=limit)
        if articles:
            return articles
    return fetch_knowledge_articles(limit=limit)


def fetch_community_posts(
    limit: int = 24,
    viewer_user_id: int | None = None,
    query: str = "",
    usage_scenario: str = "",
    sort_mode: str = "latest",
) -> list[dict[str, Any]]:
    viewer_like = f"MAX(CASE WHEN l.user_id = {viewer_user_id} THEN 1 ELSE 0 END)" if viewer_user_id else "0"
    viewer_favorite = f"MAX(CASE WHEN f.user_id = {viewer_user_id} THEN 1 ELSE 0 END)" if viewer_user_id else "0"
    where_parts: list[str] = []
    if query:
        q = sql_escape(query)
        where_parts.append(
            f"(p.title LIKE '%{q}%' OR p.summary LIKE '%{q}%' OR COALESCE(p.content, '') LIKE '%{q}%' "
            f"OR u.username LIKE '%{q}%' OR COALESCE(pr.display_name, '') LIKE '%{q}%')"
        )
    if usage_scenario and usage_scenario in RECOMMENDATION_PROFILES:
        where_parts.append(f"p.usage_scenario = '{sql_escape(usage_scenario)}'")
    where_clause = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
    order_clause = "ORDER BY p.like_count DESC, p.favorite_count DESC, p.created_at DESC, p.id DESC"
    if sort_mode != "hot":
        order_clause = "ORDER BY p.created_at DESC, p.id DESC"
    rows = run_mysql_query(
        f"""
        SELECT
          p.id,
          p.title,
          p.usage_scenario,
          p.budget,
          p.total_price,
          p.summary,
          p.like_count,
          p.favorite_count,
          DATE_FORMAT(p.created_at, '%Y-%m-%d %H:%i'),
          u.username,
          COALESCE(pr.display_name, ''),
          {viewer_like} AS liked,
          {viewer_favorite} AS favorited
        FROM community_build_post p
        INNER JOIN app_user u ON u.id = p.user_id
        LEFT JOIN app_user_profile pr ON pr.user_id = u.id
        LEFT JOIN community_post_like l ON l.post_id = p.id
        LEFT JOIN community_post_favorite f ON f.post_id = p.id
        {where_clause}
        GROUP BY p.id, p.title, p.usage_scenario, p.budget, p.total_price, p.summary, p.like_count, p.favorite_count, p.created_at, u.username, pr.display_name
        {order_clause}
        LIMIT {limit};
        """
    )
    return [
        {
            "id": int(row[0]),
            "title": row[1],
            "usage_scenario": row[2],
            "usage_label": recommendation_profile(row[2])["label"] if row[2] in RECOMMENDATION_PROFILES else row[2],
            "budget": float(row[3]),
            "total_price": float(row[4]),
            "summary": row[5],
            "like_count": int(row[6]),
            "favorite_count": int(row[7]),
            "created_at": row[8],
            "author_username": row[9],
            "author_name": row[10] or row[9],
            "liked": bool(int(row[11])),
            "favorited": bool(int(row[12])),
        }
        for row in rows
    ]


def fetch_community_post(post_id: int, viewer_user_id: int | None = None) -> dict[str, Any] | None:
    rows = run_mysql_query(
        f"""
        SELECT
          p.id,
          p.user_id,
          p.title,
          p.usage_scenario,
          p.budget,
          p.total_price,
          p.summary,
          p.content,
          p.like_count,
          p.favorite_count,
          DATE_FORMAT(p.created_at, '%Y-%m-%d %H:%i'),
          u.username,
          COALESCE(pr.display_name, '')
        FROM community_build_post p
        INNER JOIN app_user u ON u.id = p.user_id
        LEFT JOIN app_user_profile pr ON pr.user_id = u.id
        WHERE p.id = {post_id}
        LIMIT 1;
        """
    )
    if not rows:
        return None
    row = rows[0]
    item_rows = run_mysql_query(
        f"""
        SELECT component_type, component_subtype, shop, sku_name, effective_price, COALESCE(source_url, ''), COALESCE(reason_text, '')
        FROM community_build_item
        WHERE post_id = {post_id}
        ORDER BY id ASC;
        """
    )
    liked = False
    favorited = False
    if viewer_user_id:
        like_rows = run_mysql_query(
            f"SELECT 1 FROM community_post_like WHERE post_id = {post_id} AND user_id = {viewer_user_id} LIMIT 1;"
        )
        favorite_rows = run_mysql_query(
            f"SELECT 1 FROM community_post_favorite WHERE post_id = {post_id} AND user_id = {viewer_user_id} LIMIT 1;"
        )
        liked = bool(like_rows)
        favorited = bool(favorite_rows)
    return {
        "id": int(row[0]),
        "user_id": int(row[1]),
        "title": row[2],
        "usage_scenario": row[3],
        "usage_label": recommendation_profile(row[3])["label"] if row[3] in RECOMMENDATION_PROFILES else row[3],
        "budget": float(row[4]),
        "total_price": float(row[5]),
        "summary": row[6],
        "content": row[7],
        "like_count": int(row[8]),
        "favorite_count": int(row[9]),
        "created_at": row[10],
        "author_username": row[11],
        "author_name": row[12] or row[11],
        "liked": liked,
        "favorited": favorited,
        "items": [
            {
                "component_type": item_row[0],
                "component_subtype": item_row[1],
                "shop": item_row[2],
                "sku_name": item_row[3],
                "effective_price": float(item_row[4]),
                "source_url": item_row[5],
                "reason_text": item_row[6],
            }
            for item_row in item_rows
        ],
    }


def create_community_post_from_plan(
    *,
    user_id: int,
    title: str,
    summary: str,
    content: str,
    source_type: str,
    source_task_id: int | None,
    usage_scenario: str,
    budget: float,
    total_price: float,
    items: list[dict[str, Any]],
) -> int:
    run_mysql_script(
        f"""
USE school_market;
INSERT INTO community_build_post (
  user_id, source_type, source_task_id, title, usage_scenario, budget, total_price, summary, content
) VALUES (
  {user_id},
  '{sql_escape(source_type)}',
  {source_task_id if source_task_id else 'NULL'},
  '{sql_escape(title)}',
  '{sql_escape(usage_scenario)}',
  {budget},
  {total_price},
  '{sql_escape(summary)}',
  '{sql_escape(content)}'
);
"""
    )
    post_id = int(
        run_mysql_query(
            f"""
            SELECT id
            FROM community_build_post
            WHERE user_id = {user_id}
            ORDER BY id DESC
            LIMIT 1;
            """
        )[0][0]
    )
    value_sql = []
    for item in items:
        value_sql.append(
            "("
            f"{post_id},"
            f"'{sql_escape(str(item.get('component_type') or item.get('keyword') or '配件'))}',"
            f"'{sql_escape(str(item.get('component_subtype') or item.get('product_subtype') or '未命名'))}',"
            f"'{sql_escape(str(item.get('shop') or ''))}',"
            f"'{sql_escape(str(item.get('sku_name') or ''))}',"
            f"{float(item.get('effective_price') or 0)},"
            f"'{sql_escape(str(item.get('source_url') or ''))}',"
            f"'{sql_escape(str(item.get('reason_text') or ''))}'"
            ")"
        )
    if value_sql:
        run_mysql_script(
            "USE school_market; INSERT INTO community_build_item "
            "(post_id, component_type, component_subtype, shop, sku_name, effective_price, source_url, reason_text) VALUES\n"
            + ",\n".join(value_sql)
            + ";"
        )
    return post_id


def toggle_community_relation(post_id: int, user_id: int, relation: str) -> tuple[bool, dict[str, int]]:
    if relation == "like":
        table = "community_post_like"
        counter = "like_count"
    else:
        table = "community_post_favorite"
        counter = "favorite_count"
    exists = run_mysql_query(f"SELECT id FROM {table} WHERE post_id = {post_id} AND user_id = {user_id} LIMIT 1;")
    if exists:
        run_mysql_script(f"USE school_market; DELETE FROM {table} WHERE post_id = {post_id} AND user_id = {user_id};")
        run_mysql_script(
            f"USE school_market; UPDATE community_build_post SET {counter} = GREATEST({counter} - 1, 0) WHERE id = {post_id};"
        )
        active = False
    else:
        run_mysql_script(
            f"USE school_market; INSERT INTO {table} (post_id, user_id) VALUES ({post_id}, {user_id});"
        )
        run_mysql_script(f"USE school_market; UPDATE community_build_post SET {counter} = {counter} + 1 WHERE id = {post_id};")
        active = True
    post = fetch_community_post(post_id, viewer_user_id=user_id)
    return active, {
        "like_count": int(post["like_count"]) if post else 0,
        "favorite_count": int(post["favorite_count"]) if post else 0,
    }

@app.route("/")
def index() -> str:
    ensure_application_tables()
    cooldown = int(get_config("refresh_cooldown_seconds", "60"))
    return render_template(
        "dashboard_v2.html",
        refresh_cooldown=cooldown,
        market_articles=fetch_related_articles_for_context("price-trend", limit=3),
    )

@app.route("/design-preview")
def design_preview() -> str:
    return redirect(url_for("index"))

@app.route("/register", methods=["GET", "POST"])
def register() -> str:
    ensure_application_tables()
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        display_name = request.form.get("display_name", "").strip()
        username_error = validate_username(username)
        if username_error:
            flash(username_error, "error")
            return render_template("register.html")
        password_error = validate_password(password)
        if password_error:
            flash(password_error, "error")
            return render_template("register.html")
        if not username or not password:
            flash("用户名和密码不能为空。", "error")
            return render_template("register.html")
        if fetch_user_by_username(username):
            flash("用户名已存在，请更换后重试。", "error")
            return render_template("register.html")
        password_hash = generate_password_hash(password)
        run_mysql_script(
            f"""
USE school_market;
INSERT INTO app_user (username, password_hash)
VALUES ('{sql_escape(username)}', '{sql_escape(password_hash)}');
"""
        )
        user_id = int(
            run_mysql_query(
                f"""
                SELECT id
                FROM app_user
                WHERE username = '{sql_escape(username)}'
                ORDER BY id DESC
                LIMIT 1;
                """
            )[0][0]
        )
        upsert_user_profile(user_id, display_name, None, None, "", "", "")
        flash("注册成功，请登录后继续。", "success")
        return redirect(url_for("login"))
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login() -> str:
    ensure_application_tables()
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        row = fetch_user_by_username(username)
        if not row or not check_password_hash(row[2], password):
            flash("用户名或密码错误。", "error")
            return render_template("login.html")
        session["user_id"] = int(row[0])
        run_mysql_script(
            f"""
USE school_market;
UPDATE app_user
SET last_login_at = NOW()
WHERE id = {int(row[0])};
"""
        )
        flash("登录成功。", "success")
        next_url = request.args.get("next", "").strip()
        return redirect(next_url or url_for("build_center"))
    return render_template("login.html")

@app.route("/logout")
def logout() -> Any:
    session.clear()
    flash("已退出登录。", "success")
    return redirect(url_for("index"))

@app.route("/change-password", methods=["POST"])
@login_required
def change_password():
    user = get_current_user()
    current_pw = request.form.get("current_password", "")
    new_pw = request.form.get("new_password", "")
    confirm_pw = request.form.get("confirm_password", "")
    if new_pw != confirm_pw:
        flash("两次输入的新密码不一致。", "error")
        return redirect(url_for("profile"))
    if len(new_pw) < 4:
        flash("新密码至少 4 个字符。", "error")
        return redirect(url_for("profile"))
    rows = run_mysql_query(f"SELECT password_hash FROM app_user WHERE id = {user['id']}")
    if not rows or not check_password_hash(rows[0][0], current_pw):
        flash("当前密码错误。", "error")
        return redirect(url_for("profile"))
    new_hash = generate_password_hash(new_pw)
    run_mysql_script(f"USE school_market; UPDATE app_user SET password_hash = '{sql_escape(new_hash)}' WHERE id = {user['id']}")
    flash("密码修改成功。", "success")
    return redirect(url_for("profile"))


@app.route("/preferences", methods=["GET", "POST"])
@login_required
def preferences() -> str:
    """个人偏好设置页面."""
    ensure_application_tables()
    user = get_current_user()
    assert user is not None
    if request.method == "POST":
        display_name = request.form.get("display_name", "").strip()
        budget_min_text = request.form.get("budget_min", "").strip()
        budget_max_text = request.form.get("budget_max", "").strip()
        usage_preference = request.form.get("usage_preference", "").strip()
        preferred_brands = request.form.get("preferred_brands", "").strip()
        preferred_keywords = request.form.get("preferred_keywords", "").strip()
        budget_min = float(budget_min_text) if budget_min_text else None
        budget_max = float(budget_max_text) if budget_max_text else None
        upsert_user_profile(user["id"], display_name, budget_min, budget_max, usage_preference, preferred_brands, preferred_keywords)
        flash("个人偏好已更新。", "success")
        return redirect(url_for("preferences"))
    return render_template("preferences.html")


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile() -> str:
    """个人中心：账号信息 + 修改密码."""
    user = get_current_user()
    if request.method == "POST":
        display_name = request.form.get("display_name", "").strip()
        run_mysql_script(
            f"USE school_market; INSERT INTO app_user_profile (user_id, display_name) "
            f"VALUES ({user['id']}, '{sql_escape(display_name)}') "
            f"ON DUPLICATE KEY UPDATE display_name = '{sql_escape(display_name)}'"
        )
        flash("个人信息已更新。", "success")
        return redirect(url_for("profile"))
    return render_template("profile.html")

@app.route("/settings")
@login_required
def settings_page():
    """已合并到系统管理面板，重定向."""
    return redirect(url_for("admin_panel"))


@app.route("/recommend", methods=["GET", "POST"])
@login_required
def recommend() -> str:
    ensure_application_tables()
    user = get_current_user()
    assert user is not None
    options = get_build_form_options()
    recommendation_payload = None
    related_articles = fetch_related_articles_for_context(limit=4)
    if request.method == "POST":
        budget = float(request.form.get("budget", "0") or 0)
        usage_scenario = request.form.get("usage_scenario", "").strip() or "gaming"
        selected_parts = {
            "CPU": request.form.get("selected_cpu", "").strip(),
            "显卡": request.form.get("selected_gpu", "").strip(),
            "主板": request.form.get("selected_motherboard", "").strip(),
            "内存": request.form.get("selected_memory", "").strip(),
            "固态硬盘": request.form.get("selected_storage", "").strip(),
            "电源": request.form.get("selected_power", "").strip(),
            "机箱": request.form.get("selected_case", "").strip(),
            "散热器": request.form.get("selected_cooler", "").strip(),
        }
        selected_parts = {k: v for k, v in selected_parts.items() if v}
        user_prefs = learn_user_build_preferences(user["id"])
        recommendation_payload = generate_build_plan(
            user_id=user["id"],
            budget=budget,
            usage_scenario=usage_scenario,
            selected_parts=selected_parts,
            user_prefs=user_prefs,
        )
        flash("推荐组合已通过智能搭配引擎生成并保存。", "success")
    return render_template(
        "recommend.html",
        options=options,
        recommendation_payload=recommendation_payload,
        related_articles=related_articles,
    )

@app.route("/recommend/history")
@login_required
def recommendation_history() -> str:
    ensure_application_tables()
    user = get_current_user()
    assert user is not None
    history = fetch_recommendation_history(user["id"])
    return render_template("recommendation_history.html", history=history)


@app.route("/community")
@login_required
def community_home() -> str:
    user = get_current_user()
    assert user is not None
    query = request.args.get("q", "").strip()
    usage = request.args.get("usage", "").strip()
    sort_mode = request.args.get("sort", "latest").strip()
    if sort_mode not in {"latest", "hot"}:
        sort_mode = "latest"
    posts = fetch_community_posts(
        viewer_user_id=user["id"],
        query=query,
        usage_scenario=usage,
        sort_mode=sort_mode,
    )
    return render_template(
        "community_home.html",
        posts=posts,
        query=query,
        usage=usage,
        sort_mode=sort_mode,
        usage_options=RECOMMENDATION_PROFILES,
    )


@app.route("/community/post/<int:post_id>")
@login_required
def community_post_detail(post_id: int) -> str:
    user = get_current_user()
    assert user is not None
    post = fetch_community_post(post_id, viewer_user_id=user["id"])
    if not post:
        flash("未找到对应的社区方案。", "error")
        return redirect(url_for("community_home"))
    return render_template("community_detail.html", post=post)


@app.route("/community/publish", methods=["GET", "POST"])
@login_required
def community_publish() -> str:
    user = get_current_user()
    assert user is not None
    draft: dict[str, Any] | None = None
    source = request.args.get("source", "").strip()
    source_task_id = int(request.args.get("task_id", "0") or 0)
    if source in {"recommendation", "build"} and source_task_id:
        if source == "recommendation":
            matches = [item for item in fetch_recommendation_history(user["id"]) if item["id"] == source_task_id]
            if matches:
                draft = matches[0]
        else:
            matches = [item for item in fetch_build_history(user["id"]) if item["id"] == source_task_id]
            if matches:
                draft = matches[0]
    if not draft:
        recommendation_history = fetch_recommendation_history(user["id"])
        build_history_rows = fetch_build_history(user["id"])
        draft = recommendation_history[0] if recommendation_history else (build_history_rows[0] if build_history_rows else None)
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        usage_scenario = request.form.get("usage_scenario", "").strip() or "gaming"
        budget = float(request.form.get("budget", "0") or 0)
        total_price = float(request.form.get("total_price", "0") or 0)
        summary = request.form.get("summary", "").strip()
        content = request.form.get("content", "").strip()
        source_type = request.form.get("source_type", "manual").strip() or "manual"
        source_task_id_text = request.form.get("source_task_id", "").strip()
        source_task_value = int(source_task_id_text) if source_task_id_text else None
        items_json = request.form.get("items_json", "[]").strip()
        try:
            items = json.loads(items_json)
        except json.JSONDecodeError:
            items = []
        if not title:
            flash("请填写方案标题。", "error")
            return render_template("community_publish.html", draft=draft)
        if not items:
            flash("社区方案至少需要包含一条配件明细。", "error")
            return render_template("community_publish.html", draft=draft)
        post_id = create_community_post_from_plan(
            user_id=user["id"],
            title=title,
            summary=summary,
            content=content,
            source_type=source_type,
            source_task_id=source_task_value,
            usage_scenario=usage_scenario,
            budget=budget,
            total_price=total_price,
            items=items,
        )
        flash("方案已发布到装机社区。", "success")
        return redirect(url_for("community_post_detail", post_id=post_id))
    return render_template("community_publish.html", draft=draft)


@app.post("/community/post/<int:post_id>/like")
@login_required
def community_post_like(post_id: int) -> Any:
    user = get_current_user()
    assert user is not None
    active, counters = toggle_community_relation(post_id, user["id"], "like")
    return success({"active": active, **counters})


@app.post("/community/post/<int:post_id>/favorite")
@login_required
def community_post_favorite(post_id: int) -> Any:
    user = get_current_user()
    assert user is not None
    active, counters = toggle_community_relation(post_id, user["id"], "favorite")
    return success({"active": active, **counters})


@app.post("/community/post/<int:post_id>/import")
@login_required
def community_post_import(post_id: int) -> Any:
    post = fetch_community_post(post_id)
    if not post:
        flash("社区方案不存在。", "error")
        return redirect(url_for("community_home"))
    selected_parts: dict[str, str] = {}
    component_map = {
        "CPU": "CPU",
        "显卡": "显卡",
        "电脑主板": "主板",
        "主板": "主板",
        "内存条": "内存",
        "内存": "内存",
        "固态硬盘": "固态硬盘",
        "电源": "电源",
        "机箱": "机箱",
    }
    for item in post["items"]:
        key = component_map.get(item["component_type"])
        if key:
            selected_parts[key] = item["component_subtype"]
    save_build_import_draft(
        {
            "budget": post["budget"],
            "usage_scenario": post["usage_scenario"],
            "selected_parts": selected_parts,
        }
    )
    flash("社区方案已导入智能搭配，可继续调整后重新生成。", "success")
    return redirect(url_for("build_center"))


@app.route("/knowledge")
@login_required
def knowledge_home() -> str:
    categories = fetch_knowledge_categories()
    articles = fetch_knowledge_articles(limit=12)
    return render_template("knowledge_home.html", categories=categories, articles=articles)


@app.route("/knowledge/category/<slug>")
@login_required
def knowledge_category(slug: str) -> str:
    categories = fetch_knowledge_categories()
    articles = fetch_knowledge_articles(category_slug=slug)
    current_category = next((item for item in categories if item["slug"] == slug), None)
    if not current_category:
        flash("未找到该知识分类。", "error")
        return redirect(url_for("knowledge_home"))
    return render_template(
        "knowledge_category.html",
        categories=categories,
        articles=articles,
        current_category=current_category,
    )


@app.route("/knowledge/article/<int:article_id>")
@login_required
def knowledge_article_detail(article_id: int) -> str:
    article = fetch_knowledge_article(article_id)
    if not article:
        flash("未找到对应文章。", "error")
        return redirect(url_for("knowledge_home"))
    increase_knowledge_view(article_id)
    article["view_count"] += 1
    related_articles = [item for item in fetch_related_articles_for_context(article["category_slug"], limit=4) if item["id"] != article_id][:3]
    return render_template("knowledge_detail.html", article=article, related_articles=related_articles)


@app.route("/knowledge/manage", methods=["GET", "POST"])
@login_required
def knowledge_manage() -> str:
    user = get_current_user()
    assert user is not None
    if user.get("role") not in ("admin", "data_admin"):
        flash("当前账号无权限维护知识内容。", "error")
        return redirect(url_for("knowledge_home"))
    categories = fetch_knowledge_categories()
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        category_id = int(request.form.get("category_id", "0") or 0)
        summary = request.form.get("summary", "").strip()
        content = request.form.get("content", "").strip()
        if not title or not category_id or not content:
            flash("请完整填写文章标题、分类和正文。", "error")
        else:
            run_mysql_script(
                f"""
USE school_market;
INSERT INTO knowledge_article (category_id, author_user_id, title, summary, content)
VALUES (
  {category_id},
  {user['id']},
  '{sql_escape(title)}',
  '{sql_escape(summary)}',
  '{sql_escape(content)}'
);
"""
            )
            flash("文章已新增。", "success")
            return redirect(url_for("knowledge_manage"))
    articles = fetch_knowledge_articles(limit=50)
    return render_template("knowledge_manage.html", categories=categories, articles=articles)


@app.post("/knowledge/article/<int:article_id>/delete")
@login_required
def knowledge_article_delete(article_id: int) -> Any:
    user = get_current_user()
    assert user is not None
    if user.get("role") not in ("admin", "data_admin"):
        flash("当前账号无权限删除文章。", "error")
        return redirect(url_for("knowledge_manage"))
    run_mysql_script(f"USE school_market; DELETE FROM knowledge_article WHERE id = {article_id};")
    flash("文章已删除。", "success")
    return redirect(url_for("knowledge_manage"))

@app.route("/admin", methods=["GET"])
@login_required
def admin_panel() -> str:
    """管理员面板（仅 admin 和 data_admin 可见）."""
    user = get_current_user()
    if user.get("role") not in ("admin", "data_admin"):
        flash("无权限访问管理面板。", "error")
        return redirect(url_for("index"))
    configs = {}
    rows = run_mysql_query("USE school_market; SELECT config_key, config_value, config_type FROM system_config")
    for r in rows:
        configs[r[0]] = r[1]
    return render_template("admin.html",
        configs=configs,
        user_role=user.get("role"),
        admin_payload={"configs": configs})


def _admin_product_filters_from_request() -> tuple[str, list[str]]:
    query = request.args.get("query", "").strip()
    keyword = request.args.get("keyword", "").strip()
    dirty = request.args.get("dirty", "").strip()
    clauses: list[str] = []
    if query:
        like = f"%{sql_escape(query)}%"
        clauses.append(
            "("
            f"sku_name LIKE '{like}' OR "
            f"shop LIKE '{like}' OR "
            f"keyword LIKE '{like}' OR "
            f"product_subtype LIKE '{like}'"
            ")"
        )
    if keyword:
        clauses.append(f"keyword = '{sql_escape(keyword)}'")
    if dirty in {"0", "1"}:
        clauses.append(f"is_dirty_data = {dirty}")
    return (" AND ".join(clauses), [query, keyword, dirty])


@app.get("/api/admin/products")
@login_required
def api_admin_products() -> tuple[Any, int]:
    """分页获取商品快照列表，供管理员核查与维护。"""
    user = get_current_user()
    if user.get("role") not in ("admin", "data_admin"):
        return failure("无权限", 403)
    page = max(int(request.args.get("page", "1") or 1), 1)
    page_size = min(max(int(request.args.get("page_size", "10") or 10), 1), 50)
    offset = (page - 1) * page_size
    filter_sql, _ = _admin_product_filters_from_request()
    where_sql = f"WHERE {filter_sql}" if filter_sql else ""

    count_rows = run_mysql_query(
        f"USE school_market; SELECT COUNT(*) FROM pc_part_market_snapshot {where_sql};"
    )
    total = int(count_rows[0][0]) if count_rows else 0
    total_pages = max((total + page_size - 1) // page_size, 1)

    rows = run_mysql_query(
        f"""
USE school_market;
SELECT
  id,
  keyword,
  COALESCE(product_subtype, ''),
  COALESCE(shop, ''),
  sku_name,
  COALESCE(coupon_price, original_price, 0),
  COALESCE(category_level4, category_level3, category_level2, category_level1, ''),
  DATE_FORMAT(crawl_time, '%Y-%m-%d %H:%i'),
  COALESCE(is_dirty_data, 0),
  COALESCE(category_remark, '')
FROM pc_part_market_snapshot
{where_sql}
ORDER BY crawl_time DESC, id DESC
LIMIT {page_size} OFFSET {offset};
"""
    )
    items = [
        {
            "id": int(row[0]),
            "keyword": row[1],
            "product_subtype": row[2],
            "shop": row[3],
            "sku_name": row[4],
            "effective_price": float(row[5] or 0),
            "category_label": row[6],
            "crawl_time": row[7],
            "is_dirty_data": int(row[8] or 0),
            "category_remark": row[9],
        }
        for row in rows
    ]
    keyword_rows = run_mysql_query(
        "USE school_market; SELECT DISTINCT keyword FROM pc_part_market_snapshot ORDER BY keyword;"
    )
    keywords = [row[0] for row in keyword_rows if row and row[0]]
    return success(
        {
            "items": items,
            "pagination": {"page": page, "page_size": page_size, "total": total, "total_pages": total_pages},
            "keywords": keywords,
        }
    )


@app.get("/api/admin/products/detail")
@login_required
def api_admin_product_detail() -> tuple[Any, int]:
    """获取单个商品快照的详细信息。"""
    user = get_current_user()
    if user.get("role") not in ("admin", "data_admin"):
        return failure("无权限", 403)
    product_id = int(request.args.get("id", "0") or 0)
    if not product_id:
        return failure("缺少商品ID", 400)
    rows = run_mysql_query(
        f"""
USE school_market;
SELECT
  id, keyword, page_no, source_platform, COALESCE(shop, ''), sku_name,
  COALESCE(coupon_price, 0), COALESCE(original_price, 0), COALESCE(source_url, ''),
  COALESCE(product_subtype, ''), COALESCE(subtype_group, ''),
  COALESCE(category_level1, ''), COALESCE(category_level2, ''),
  COALESCE(category_level3, ''), COALESCE(category_level4, ''),
  COALESCE(category_remark, ''), DATE_FORMAT(crawl_time, '%Y-%m-%d %H:%i:%s'),
  COALESCE(is_dirty_data, 0)
FROM pc_part_market_snapshot
WHERE id = {product_id}
LIMIT 1;
"""
    )
    if not rows:
        return failure("未找到商品记录", 404)
    row = rows[0]
    return success(
        {
            "id": int(row[0]),
            "keyword": row[1],
            "page_no": int(row[2]),
            "source_platform": row[3],
            "shop": row[4],
            "sku_name": row[5],
            "coupon_price": float(row[6] or 0),
            "original_price": float(row[7] or 0),
            "source_url": row[8],
            "product_subtype": row[9],
            "subtype_group": row[10],
            "category_level1": row[11],
            "category_level2": row[12],
            "category_level3": row[13],
            "category_level4": row[14],
            "category_remark": row[15],
            "crawl_time": row[16],
            "is_dirty_data": int(row[17] or 0),
        }
    )


@app.post("/api/admin/products/update")
@login_required
def api_admin_product_update() -> tuple[Any, int]:
    """更新商品基础信息、分类结果、备注和脏数据状态。"""
    user = get_current_user()
    if user.get("role") not in ("admin", "data_admin"):
        return failure("无权限", 403)
    data = request.get_json(silent=True) or {}
    product_id = int(data.get("id", 0) or 0)
    if not product_id:
        return failure("缺少商品ID", 400)

    keyword = str(data.get("keyword", "") or "").strip()
    source_platform = str(data.get("source_platform", "") or "").strip()
    shop = str(data.get("shop", "") or "").strip()
    sku_name = str(data.get("sku_name", "") or "").strip()
    product_subtype = str(data.get("product_subtype", "") or "").strip()
    category_level1 = str(data.get("category_level1", "") or "").strip()
    category_level2 = str(data.get("category_level2", "") or "").strip()
    category_level3 = str(data.get("category_level3", "") or "").strip()
    category_level4 = str(data.get("category_level4", "") or "").strip()
    category_remark = str(data.get("category_remark", "") or "").strip()
    is_dirty_data = 1 if str(data.get("is_dirty_data", "0")).strip() in {"1", "true", "True"} else 0
    if not keyword:
        return failure("关键字不能为空", 400)
    if not source_platform:
        return failure("来源平台不能为空", 400)
    if not sku_name:
        return failure("商品标题不能为空", 400)

    run_mysql_script(
        f"""
USE school_market;
UPDATE pc_part_market_snapshot
SET
  keyword = '{sql_escape(keyword)}',
  source_platform = '{sql_escape(source_platform)}',
  shop = {('NULL' if not shop else f"'{sql_escape(shop)}'")},
  sku_name = '{sql_escape(sku_name)}',
  product_subtype = '{sql_escape(product_subtype)}',
  category_level1 = {('NULL' if not category_level1 else f"'{sql_escape(category_level1)}'")},
  category_level2 = {('NULL' if not category_level2 else f"'{sql_escape(category_level2)}'")},
  category_level3 = {('NULL' if not category_level3 else f"'{sql_escape(category_level3)}'")},
  category_level4 = {('NULL' if not category_level4 else f"'{sql_escape(category_level4)}'")},
  category_remark = {('NULL' if not category_remark else f"'{sql_escape(category_remark)}'")},
  is_dirty_data = {is_dirty_data}
WHERE id = {product_id};
"""
    )
    return success({"updated": product_id})


@app.route("/build", methods=["GET", "POST"])
@login_required
def build_center() -> str:
    user = get_current_user()
    assert user is not None
    options = get_build_form_options()
    chat_state = get_build_chat_state(user)
    profile_soft = build_user_soft_constraints(user)
    imported_draft = pop_build_import_draft()
    form_state = {
        "budget": "",  # 不预填，让用户自己输入
        "usage_scenario": user.get("usage_preference") or "",
        "selected_parts": {},
    }
    build_payload = None
    if imported_draft:
        form_state = {
            "budget": imported_draft.get("budget", ""),
            "usage_scenario": imported_draft.get("usage_scenario", user.get("usage_preference") or ""),
            "selected_parts": imported_draft.get("selected_parts", {}),
        }
    related_articles = fetch_related_articles_for_context(limit=3)
    if request.method == "POST":
        budget = float(request.form.get("budget", "0") or 0)
        usage_scenario = request.form.get("usage_scenario", "").strip() or "gaming"
        selected_parts = {
            "CPU": request.form.get("selected_cpu", "").strip(),
            "显卡": request.form.get("selected_gpu", "").strip(),
            "主板": request.form.get("selected_motherboard", "").strip(),
            "内存": request.form.get("selected_memory", "").strip(),
            "固态硬盘": request.form.get("selected_storage", "").strip(),
            "电源": request.form.get("selected_power", "").strip(),
            "机箱": request.form.get("selected_case", "").strip(),
        }
        selected_parts = {key: value for key, value in selected_parts.items() if value}
        form_state = {
            "budget": budget,
            "usage_scenario": usage_scenario,
            "selected_parts": selected_parts,
        }
        build_payload = generate_build_plan(
            user_id=user["id"],
            budget=budget,
            usage_scenario=usage_scenario,
            selected_parts=selected_parts,
            user_prefs={"_soft_constraints": profile_soft} if any(profile_soft.get(key) for key in ("brands", "keywords", "white_theme", "quiet_mode")) else None,
        )
        flash("结构化搭配已生成，并已结合个人偏好做结果约束。", "success")
    return render_template(
        "build.html",
        options=options,
        build_profiles=BUDGET_TEMPLATES,
        build_payload=build_payload,
        form_state=form_state,
        chat_state=serialize_build_chat_state(chat_state),
        build_chat_suggestions=BUILD_CHAT_SUGGESTIONS,
        current_user=user,
        related_articles=related_articles,
    )

@app.route("/build/history")
@login_required
def build_history() -> str:
    user = get_current_user()
    assert user is not None
    history = fetch_build_history(user["id"])
    return render_template("build_history.html", history=history)

@app.post("/api/build/refresh-knowledge")
@login_required
def api_refresh_build_knowledge() -> tuple[Any, int]:
    try:
        return success(refresh_build_knowledge())
    except Exception as exc:
        return failure(str(exc))

@app.post("/api/build/chat")
@login_required
@rate_limit(limit=20, window=60)
def api_build_chat() -> tuple[Any, int]:
    try:
        user = get_current_user()
        assert user is not None
        payload = request.get_json(silent=True) or {}
        message = str(payload.get("message", "") or "").strip()
        form_context = payload.get("form_context") or {}
        if not message:
            return failure("请输入你的装机想法。", 400)
        return success(build_chat_agent_response(user, message, form_context))
    except Exception as exc:
        return failure(str(exc))

@app.post("/api/build/chat/reset")
@login_required
def api_build_chat_reset() -> tuple[Any, int]:
    try:
        user = get_current_user()
        assert user is not None
        state = reset_build_chat_state(user)
        return success(
            {
                "reply": "已清空当前对话约束。你可以重新输入预算、用途和已选配件。",
                "requirements": serialize_build_chat_state(state),
                "build_payload": None,
                "explanations": [],
                "suggestions": BUILD_CHAT_SUGGESTIONS,
            }
        )
    except Exception as exc:
        return failure(str(exc))

@app.post("/api/build/history/delete")
@login_required
def api_build_history_delete() -> tuple[Any, int]:
    try:
        user = get_current_user()
        assert user is not None
        payload = request.get_json(silent=True) or {}
        task_id = int(payload.get("task_id") or 0)
        if not task_id:
            return failure("缺少 task_id", 400)
        # 验证该任务属于当前用户
        rows = run_mysql_query(
            f"SELECT id FROM pc_build_task WHERE id = {task_id} AND user_id = {user['id']} LIMIT 1;"
        )
        if not rows:
            return failure("未找到该搭配记录或无权删除", 404)
        # 级联删除（pc_build_result_item 设置了 CASCADE）
        run_mysql_script(
            f"USE school_market; DELETE FROM pc_build_task WHERE id = {task_id} AND user_id = {user['id']};"
        )
        return success({"deleted": True, "task_id": task_id})
    except Exception as exc:
        return failure(str(exc))


@app.post("/api/build/history/delete-all")
@login_required
def api_build_history_delete_all() -> tuple[Any, int]:
    try:
        user = get_current_user()
        assert user is not None
        run_mysql_script(f"USE school_market; DELETE FROM pc_build_task WHERE user_id = {user['id']};")
        return success({"deleted": True})
    except Exception as exc:
        return failure(str(exc))

@app.route("/api/ping", methods=["GET"])
def api_ping() -> tuple[Any, int]:
    return success({"message": "pong", "version": "2.0.0"})


@app.post("/api/auth/login")
def api_auth_login() -> tuple[Any, int]:
    """JSON 登录（前后端分离用）"""
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "") or "").strip()
    password = str(data.get("password", "") or "").strip()
    if not username or not password:
        return failure("请输入用户名和密码", 400)
    ensure_application_tables()
    rows = run_mysql_query(
        f"SELECT id, username, password_hash FROM app_user WHERE username = '{sql_escape(username)}'"
    )
    if not rows:
        return failure("用户名或密码错误", 401)
    user_row = rows[0]
    if not check_password_hash(user_row[2], password):
        return failure("用户名或密码错误", 401)
    user = {"id": int(user_row[0]), "username": user_row[1], "role": "member"}
    session["user"] = user
    return success({"user": {"id": user["id"], "username": user["username"], "role": user["role"]}})


@app.post("/api/auth/logout")
def api_auth_logout() -> tuple[Any, int]:
    session.pop("user", None)
    return success({"message": "已退出"})


@app.post("/api/auth/register")
def api_auth_register() -> tuple[Any, int]:
    """JSON 注册（前后端分离用）"""
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "") or "").strip()
    password = str(data.get("password", "") or "").strip()
    if not username or not password:
        return failure("请输入用户名和密码", 400)
    if len(username) < 2:
        return failure("用户名至少2个字符", 400)
    if len(password) < 4:
        return failure("密码至少4个字符", 400)
    ensure_application_tables()
    existing = run_mysql_query(
        f"SELECT id FROM app_user WHERE username = '{sql_escape(username)}'"
    )
    if existing:
        return failure("用户名已存在", 409)
    hashed = generate_password_hash(password)
    run_mysql_script(
        f"USE school_market; INSERT INTO app_user (username, password_hash) "
        f"VALUES ('{sql_escape(username)}', '{sql_escape(hashed)}');"
    )
    return success({"message": "注册成功"})


@app.get("/api/health")
def api_health() -> tuple[Any, int]:
    try:
        overview = fetch_overview(None)
        return success(
            {
                "mysql": "ready",
                "snapshot_count": overview["snapshot_count"],
                "dirty_count": overview["dirty_count"],
                "keyword_count": overview["keyword_count"],
            }
        )
    except Exception as exc:
        return failure(str(exc))

@app.get("/api/keywords")
def api_keywords() -> tuple[Any, int]:
    try:
        return success(fetch_keyword_stats())
    except Exception as exc:
        return failure(str(exc))

@app.get("/api/dashboard-data")
def api_dashboard_data() -> tuple[Any, int]:
    try:
        keyword = request.args.get("keyword", "").strip() or None
        query = request.args.get("query", "").strip()
        return success(build_dashboard_payload(keyword, query))
    except Exception as exc:
        return failure(str(exc))

@app.post("/api/dashboard-refresh")
def api_dashboard_refresh() -> tuple[Any, int]:
    try:
        now = int(time.time())
        last_refresh_at = int(session.get("last_manual_dashboard_refresh_at", 0) or 0)
        remaining = MANUAL_REFRESH_COOLDOWN_SECONDS - (now - last_refresh_at)
        if remaining > 0:
            return failure(f"请 {remaining} 秒后再试。", 429)

        payload = request.get_json(silent=True) or {}
        keyword = str(payload.get("keyword", "") or "").strip() or None
        query = str(payload.get("query", "") or "").strip()
        data = build_dashboard_payload(keyword, query)
        data["snapshot_count"] = data.get("overview", {}).get("snapshot_count", 0)
        data["refreshed_at"] = time.strftime("%H:%M:%S")
        data["cooldown_seconds"] = MANUAL_REFRESH_COOLDOWN_SECONDS
        session["last_manual_dashboard_refresh_at"] = now
        return success(data)
    except Exception as exc:
        return failure(str(exc))


@app.post("/api/pipeline/run")
def api_pipeline_run() -> tuple[Any, int]:
    """触发完整数据刷新管道：HTML导入→归档→Flink→分析→知识库."""
    try:
        steps = []
        for label, script, args, timeout_sec in [
            ("HTML导入SQLite", "pa1.py", ["--scan-saved-html", "--html-glob", "**/*.html"], 300),
            ("归档旧批次", "scripts/archive_old_market_data.py", ["--keep-batches", "15"], 60),
            ("Flink ETL", "scripts/run_flink_mysql_pipeline.ps1", ["-ExportMode", "all"], 600),
            ("市场分析+告警", "market_analytics_pipeline.py", [], 300),
            ("装机知识库", "scripts/build_smart_build_knowledge.py", [], 300),
        ]:
            cmd = [sys.executable, str(BASE_DIR / script)] + args if script.endswith(".py") else \
                  ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(BASE_DIR / script)] + args
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec, cwd=str(BASE_DIR))
            steps.append({"step": label, "ok": result.returncode == 0, "detail": (result.stdout + result.stderr)[-300:]})

        all_ok = all(s["ok"] for s in steps)
        return success({"completed": all_ok, "refreshed_at": time.strftime("%Y-%m-%d %H:%M:%S"), "steps": steps})
    except Exception as exc:
        return failure(str(exc))


# ============================================================
# 管理员 API
# ============================================================

@app.get("/api/admin/config")
def api_admin_get_configs() -> tuple[Any, int]:
    """获取系统配置（公开读取，修改需管理员）."""
    rows = run_mysql_query("USE school_market; SELECT config_key, config_value, config_type, description FROM system_config ORDER BY config_key")
    configs = [{"key": r[0], "value": r[1], "type": r[2], "desc": r[3]} for r in rows]
    return success({"configs": configs})


@app.post("/api/admin/config")
@login_required
def api_admin_set_config() -> tuple[Any, int]:
    """保存单个配置."""
    user = get_current_user()
    if user.get("role") not in ("admin", "data_admin"):
        return failure("无权限", 403)
    data = request.get_json(silent=True) or {}
    key = str(data.get("key", "")).strip()
    value = str(data.get("value", ""))
    if not key:
        return failure("缺少配置键", 400)
    set_config(key, value)
    return success({"key": key, "value": value})


@app.get("/api/admin/users")
@login_required
def api_admin_list_users() -> tuple[Any, int]:
    """用户列表（仅平台管理员）."""
    user = get_current_user()
    if user.get("role") != "admin":
        return failure("仅平台管理员可访问", 403)
    rows = run_mysql_query(
        "USE school_market; SELECT u.id, u.username, u.role, "
        "DATE_FORMAT(u.created_at, '%Y-%m-%d %H:%i'), COALESCE(p.usage_preference,'') "
        "FROM app_user u LEFT JOIN app_user_profile p ON p.user_id = u.id ORDER BY u.id"
    )
    users = [{"id": int(r[0]), "username": r[1], "role": r[2], "created_at": r[3], "usage_pref": r[4]} for r in rows]
    return success({"users": users})


@app.post("/api/admin/users/update")
@login_required
def api_admin_update_user() -> tuple[Any, int]:
    """更新用户信息（用户名、角色、密码）."""
    user = get_current_user()
    if user.get("role") != "admin":
        return failure("仅平台管理员可操作", 403)
    data = request.get_json(silent=True) or {}
    user_id = int(data.get("id", 0))
    new_role = str(data.get("role", "member")).strip()
    new_password = str(data.get("password", "") or "").strip()
    new_username = str(data.get("username", "") or "").strip()
    if not user_id or new_role not in ("member", "admin", "data_admin"):
        return failure("参数错误", 400)
    # 构建 SET 子句
    sets = [f"role = '{sql_escape(new_role)}'"]
    if new_password:
        sets.append(f"password_hash = '{sql_escape(generate_password_hash(new_password))}'")
    if new_username:
        sets.append(f"username = '{sql_escape(new_username)}'")
    run_mysql_script(
        f"USE school_market; UPDATE app_user SET {', '.join(sets)} WHERE id = {user_id}"
    )
    return success({"id": user_id, "role": new_role})


@app.post("/api/admin/users/reset-password")
@login_required
def api_admin_reset_password() -> tuple[Any, int]:
    """重置用户密码为 Xl666666."""
    user = get_current_user()
    if user.get("role") != "admin":
        return failure("仅平台管理员可操作", 403)
    data = request.get_json(silent=True) or {}
    user_id = int(data.get("id", 0))
    if not user_id:
        return failure("缺少用户ID", 400)
    hashed = generate_password_hash("Xl666666")
    run_mysql_script(
        f"USE school_market; UPDATE app_user SET password_hash = '{sql_escape(hashed)}' WHERE id = {user_id}"
    )
    return success({"message": "密码已重置为 Xl666666"})


@app.post("/api/admin/users/create")
@login_required
def api_admin_create_user() -> tuple[Any, int]:
    """创建新用户."""
    user = get_current_user()
    if user.get("role") != "admin":
        return failure("仅平台管理员可操作", 403)
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "") or "").strip()
    password = str(data.get("password", "") or "").strip()
    role = str(data.get("role", "member")).strip()
    if not username or not password:
        return failure("用户名和密码不能为空", 400)
    if len(username) < 2:
        return failure("用户名至少2个字符", 400)
    if role not in ("member", "admin", "data_admin"):
        return failure("角色参数错误", 400)
    # 检查重复
    existing = run_mysql_query(
        f"SELECT id FROM app_user WHERE username = '{sql_escape(username)}'"
    )
    if existing:
        return failure("用户名已存在", 409)
    hashed = generate_password_hash(password)
    run_mysql_script(
        f"USE school_market; INSERT INTO app_user (username, password_hash, role) "
        f"VALUES ('{sql_escape(username)}', '{sql_escape(hashed)}', '{sql_escape(role)}')"
    )
    return success({"message": "创建成功", "username": username, "role": role})


@app.post("/api/admin/users/delete")
@login_required
def api_admin_delete_user() -> tuple[Any, int]:
    """删除用户."""
    user = get_current_user()
    if user.get("role") != "admin":
        return failure("仅平台管理员可操作", 403)
    data = request.get_json(silent=True) or {}
    user_id = int(data.get("id", 0))
    if not user_id:
        return failure("缺少用户ID", 400)
    run_mysql_script(f"USE school_market; DELETE FROM app_user WHERE id = {user_id}")
    return success({"deleted": user_id})


@app.post("/api/admin/archive")
@login_required
def api_admin_archive() -> tuple[Any, int]:
    """手动归档旧数据."""
    user = get_current_user()
    if user.get("role") not in ("admin", "data_admin"):
        return failure("无权限", 403)
    keep = get_int_config("archive_keep_batches", 15)
    result = subprocess.run(
        [sys.executable, str(BASE_DIR / "scripts" / "archive_old_market_data.py"), "--keep-batches", str(keep)],
        capture_output=True, text=True, timeout=60, cwd=str(BASE_DIR)
    )
    return success({"ok": result.returncode == 0, "detail": (result.stdout + result.stderr)[-300:]})


@app.get("/api/admin/alerts")
@login_required
def api_admin_alerts() -> tuple[Any, int]:
    """获取异常告警列表."""
    user = get_current_user()
    if user.get("role") not in ("admin", "data_admin"):
        return failure("无权限", 403)
    rows = run_mysql_query(
        "USE school_market; SELECT id, keyword, product_subtype, "
        "previous_avg_price, latest_avg_price, delta_amount, delta_rate, level, latest_crawl_time "
        "FROM pc_part_market_alerts ORDER BY latest_crawl_time DESC LIMIT 50"
    )
    alerts = [{"id": int(r[0]), "keyword": r[1], "subtype": r[2],
               "prev_price": float(r[3]) if r[3] else 0, "latest_price": float(r[4]) if r[4] else 0,
               "delta": float(r[5]) if r[5] else 0, "delta_rate": float(r[6]) if r[6] else 0,
               "level": r[7], "time": r[8]} for r in rows]
    return success({"alerts": alerts})


@app.post("/api/admin/alerts/dismiss")
@login_required
def api_admin_dismiss_alert() -> tuple[Any, int]:
    """忽略单条告警."""
    user = get_current_user()
    if user.get("role") not in ("admin", "data_admin"):
        return failure("无权限", 403)
    data = request.get_json(silent=True) or {}
    alert_id = int(data.get("id", 0))
    if alert_id:
        run_mysql_script(f"USE school_market; DELETE FROM pc_part_market_alerts WHERE id = {alert_id}")
    return success({"dismissed": alert_id})


@app.get("/api/search")
def api_search() -> tuple[Any, int]:
    try:
        query = request.args.get("query", "").strip()
        keyword = request.args.get("keyword", "").strip() or None
        return success(search_products(query, keyword))
    except Exception as exc:
        return failure(str(exc))

@app.get("/api/download/<filename>")
def api_download(filename: str):
    file_map = {
        "clean-snapshot.csv": ANALYTICS_DIR / "clean_snapshot.csv",
        "subtype-stats.csv": ANALYTICS_DIR / "subtype_stats.csv",
        "alerts.csv": ANALYTICS_DIR / "alerts.csv",
    }
    path = file_map.get(filename)
    if not path or not path.exists():
        return failure("文件不存在", 404)
    return send_file(path, as_attachment=True, download_name=filename)

@sock.route("/ws/market")
def ws_market(ws) -> None:
    keyword = request.args.get("keyword", "").strip() or None
    query = request.args.get("query", "").strip()
    while True:
        try:
            payload = build_dashboard_payload(keyword, query)
            ws.send(json.dumps({"type": "dashboard", "data": payload}, ensure_ascii=False))
            time.sleep(WS_PUSH_INTERVAL_SECONDS)
        except Exception:
            break

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
