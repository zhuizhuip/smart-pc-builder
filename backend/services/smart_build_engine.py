from __future__ import annotations

import json
import math
import re
import subprocess
from collections import Counter, defaultdict
from itertools import combinations
import os
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from collectors.pa1 import extract_rows_from_html, infer_keyword_from_html_filename, normalize_price, normalize_source_url, normalize_text
from backend.services.compatibility_rules import get_compatibility_engine


BASE_DIR = Path(__file__).resolve().parents[2]
COMPOSE_FILE = BASE_DIR / "docker-compose.yml"
TAOBAO_DIR = BASE_DIR / "淘宝数据"
ZOL_DIR = BASE_DIR / "中关村数据"

CPU_PATTERN = re.compile(r"\b(i[3579]-?\d{4,5}[A-Z]{0,2}|ULTRA\s*\d{3}[A-Z]?|\d{4,5}X3D|\d{4,5}X|\d{4,5}F|\d{4,5}KF|\d{4,5}K|\d{4,5}G|G\d{4})\b", re.I)
GPU_PATTERN = re.compile(r"\b(RTX\s*\d{4}\s*(?:TI|SUPER)?|GTX\s*\d{3,4}\s*(?:TI|SUPER)?|RX\s*\d{4,5}\s*(?:XT|GRE)?|ARC\s*[AB]\d{3}|GT\s*\d{3,4})\b", re.I)
MOTHERBOARD_PATTERN = re.compile(r"\b(X870E|X870|X670E|X670|X570|B860|B850M|B850|B760M|B760|B650M|B650|B550M|B550|H710|H610M|H610|H510M|H510|Z890|Z790|Z690|A620|A520M|A520|A320)\b", re.I)
MEMORY_GEN_PATTERN = re.compile(r"DDR\s*([345])L?", re.I)
MEMORY_CAP_PATTERN = re.compile(r"(4|8|12|16|24|32|48|64|96|128)\s*(?:G|GB)", re.I)
MEMORY_KIT_PATTERN = re.compile(r"(4|8|12|16|24|32|48|64)\s*(?:G|GB)\s*[*Xx×]\s*(2|4)", re.I)
STORAGE_PATTERN = re.compile(r"\b(120|128|240|250|256|480|500|512|960)\s*(?:G|GB)\b|\b(1|2|4|6|8)\s*T(?:B)?\b", re.I)
PSU_WATT_PATTERN = re.compile(r"\b(300|350|400|450|500|550|600|650|700|750|800|850|1000|1200)\s*W\b", re.I)
CASE_SIZE_PATTERN = re.compile(r"\b(E-ATX|ATX|M-ATX|MATX|MICRO-ATX|ITX|MINI-ITX)\b", re.I)
COOLER_WATER_PATTERN = re.compile(r"\b(120|240|280|360|420)\s*(?:冷排|水冷)?\b")
HEATPIPE_PATTERN = re.compile(r"([45678])\s*热管")
PRICE_NUMBER_PATTERN = re.compile(r"([0-9]+(?:\.[0-9]{1,2})?)")

COMPONENT_ORDER = ["CPU", "主板", "内存", "显卡", "固态硬盘", "电源", "机箱", "散热器"]

BUDGET_TEMPLATES: dict[str, dict[str, Any]] = {
    "gaming": {
        "label": "游戏电竞",
        "ratios": {"CPU": 0.21, "主板": 0.13, "内存": 0.11, "显卡": 0.35, "固态硬盘": 0.09, "电源": 0.06, "机箱": 0.03, "散热器": 0.02},
        "memory_capacity": 32,
        "storage_capacity": "1TB",
        "discrete_gpu": True,
    },
    "office": {
        "label": "办公学习",
        "ratios": {"CPU": 0.30, "主板": 0.18, "内存": 0.16, "固态硬盘": 0.16, "电源": 0.09, "机箱": 0.06, "散热器": 0.05},
        "memory_capacity": 16,
        "storage_capacity": "512GB",
        "discrete_gpu": False,
    },
    "design": {
        "label": "设计创作",
        "ratios": {"CPU": 0.24, "主板": 0.12, "内存": 0.15, "显卡": 0.28, "固态硬盘": 0.11, "电源": 0.05, "机箱": 0.03, "散热器": 0.02},
        "memory_capacity": 32,
        "storage_capacity": "1TB",
        "discrete_gpu": True,
    },
    "streaming": {
        "label": "直播剪辑",
        "ratios": {"CPU": 0.23, "主板": 0.12, "内存": 0.14, "显卡": 0.27, "固态硬盘": 0.12, "电源": 0.06, "机箱": 0.04, "散热器": 0.02},
        "memory_capacity": 32,
        "storage_capacity": "1TB",
        "discrete_gpu": True,
    },
}

COMPATIBILITY_RULES = [
    {"component_type": "CPU", "rule_key": "vendor", "rule_value": "AMD", "compatible_values": ["B650M", "B650", "B850", "B850M", "X670", "X670E", "X870", "X870E", "B550M", "B550", "A520", "A520M", "A620"]},
    {"component_type": "CPU", "rule_key": "vendor", "rule_value": "Intel", "compatible_values": ["B760M", "B760", "Z790", "Z690", "H610", "H610M", "H710", "B860", "Z890"]},
    {"component_type": "CPU", "rule_key": "memory_gen", "rule_value": "AM5", "compatible_values": ["DDR5"]},
    {"component_type": "CPU", "rule_key": "memory_gen", "rule_value": "AM4", "compatible_values": ["DDR4"]},
    {"component_type": "CPU", "rule_key": "memory_gen", "rule_value": "LGA1700", "compatible_values": ["DDR4", "DDR5"]},
]


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
    rows = [line.split("\t") for line in result.stdout.splitlines() if line.strip()]
    return rows


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


def ensure_build_tables() -> None:
    run_mysql_script(
        """
USE school_market;

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
"""
    )
    has_source_url = run_mysql_query(
        """
        SELECT COUNT(*)
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = 'school_market'
          AND TABLE_NAME = 'pc_build_result_item'
          AND COLUMN_NAME = 'source_url';
        """
    )
    if not has_source_url or int(has_source_url[0][0]) == 0:
        run_mysql_script(
            """
USE school_market;
ALTER TABLE pc_build_result_item
  ADD COLUMN source_url TEXT NULL AFTER effective_price;
"""
        )


def normalize_component_type(raw_keyword: str, sku_name: str) -> str:
    key = normalize_text(raw_keyword).lower()
    title = normalize_text(sku_name).lower()
    if "cpu" in key or key == "处理器":
        return "CPU"
    if "主板" in raw_keyword:
        return "主板"
    if "内存" in raw_keyword:
        return "内存"
    if "显卡" in raw_keyword:
        return "显卡"
    if "固态" in raw_keyword or ("硬盘" in raw_keyword and "固态" in title):
        return "固态硬盘"
    if "电源" in raw_keyword:
        return "电源"
    if "机箱" in raw_keyword and "风扇" not in raw_keyword:
        return "机箱"
    if "散热器" in raw_keyword:
        return "散热器"
    if "风扇" in raw_keyword:
        return "风扇"
    return ""


def _normalize_cpu_model(text: str) -> str:
    match = CPU_PATTERN.search(text)
    if not match:
        return ""
    token = match.group(1).upper().replace(" ", "")
    if token.startswith("I") and "-" not in token:
        token = f"{token[:2]}-{token[2:]}"
    if token.startswith("ULTRA"):
        return token.replace("ULTRA", "Ultra ")
    if token.startswith("G") and token[1:].isdigit():
        return f"奔腾 {token}"
    return token


def _normalize_gpu_model(text: str) -> str:
    match = GPU_PATTERN.search(text)
    if not match:
        return ""
    raw = re.sub(r"\s+", "", match.group(1).upper()).strip()
    for prefix in ["RTX", "GTX", "RX", "ARC", "GT"]:
        if raw.startswith(prefix):
            remainder = raw[len(prefix):]
            suffix = ""
            for marker in ["SUPER", "TI", "XT", "GRE"]:
                if remainder.endswith(marker):
                    remainder = remainder[: -len(marker)]
                    suffix = marker
                    break
            token = f"{prefix} {remainder}"
            if suffix:
                suffix = suffix.replace("TI", "Ti").replace("SUPER", "Super").replace("XT", "XT").replace("GRE", "GRE")
                token = f"{token} {suffix}"
            return token.replace("ARC", "Arc")
    return raw


def _collect_cpu_models(text: str) -> list[str]:
    values = []
    seen = set()
    for match in CPU_PATTERN.finditer(text):
        token = _normalize_cpu_model(match.group(0))
        if token and token not in seen:
            seen.add(token)
            values.append(token)
    return values


def _collect_gpu_models(text: str) -> list[str]:
    values = []
    seen = set()
    for match in GPU_PATTERN.finditer(text):
        token = _normalize_gpu_model(match.group(0))
        if token and token not in seen:
            seen.add(token)
            values.append(token)
    return values


def _collect_motherboards(text: str) -> list[str]:
    values = []
    seen = set()
    for match in MOTHERBOARD_PATTERN.finditer(text):
        token = match.group(1).upper()
        if token not in seen:
            seen.add(token)
            values.append(token)
    return values


def _extract_memory_capacity(text: str) -> str:
    kit = MEMORY_KIT_PATTERN.search(text)
    if kit:
        return f"{int(kit.group(1)) * int(kit.group(2))}GB"
    values = sorted({int(value) for value in MEMORY_CAP_PATTERN.findall(text)})
    if not values:
        return ""
    if len(values) >= 3:
        return f"{values[-1]}GB"
    return f"{values[0]}GB"


def _normalize_memory_model(text: str) -> str:
    capacity = _extract_memory_capacity(text)
    gen_match = MEMORY_GEN_PATTERN.search(text)
    gen = f"DDR{gen_match.group(1)}" if gen_match else ""
    if gen and capacity:
        return f"{gen} {capacity}"
    if gen:
        return gen
    return capacity


def _normalize_storage_model(text: str) -> str:
    capacity = ""
    match = STORAGE_PATTERN.search(text)
    if match:
        if match.group(1):
            capacity = f"{match.group(1)}GB"
        elif match.group(2):
            capacity = f"{match.group(2)}TB"
    upper = text.upper()
    if "NVME" in upper or "M.2" in upper or "PCIE" in upper:
        storage_type = "NVMe SSD"
    elif "SSD" in upper or "固态" in text:
        storage_type = "SATA SSD"
    elif "HDD" in upper or "机械" in text:
        storage_type = "HDD"
    else:
        storage_type = ""
    return " ".join(part for part in [capacity, storage_type] if part).strip()


def _normalize_psu_model(text: str) -> str:
    match = PSU_WATT_PATTERN.search(text.upper())
    return f"{match.group(1)}W" if match else ""


def _normalize_case_model(text: str) -> str:
    upper = text.upper()
    match = CASE_SIZE_PATTERN.search(upper)
    if match:
        token = match.group(1).replace("MICRO-ATX", "MATX").replace("M-ATX", "MATX")
        if token == "ITX" or token == "MINI-ITX":
            return "ITX机箱"
        return f"{token}机箱"
    if "海景房" in text:
        return "海景房机箱"
    if "小机箱" in text or "迷你" in text:
        return "小型机箱"
    return "中塔机箱"


def _normalize_cooler_model(text: str) -> str:
    if "水冷" in text:
        size_match = COOLER_WATER_PATTERN.search(text)
        return f"{size_match.group(1)}水冷" if size_match else "一体式水冷"
    if "双塔" in text:
        return "双塔风冷"
    if "下压" in text:
        return "下压风冷"
    pipe_match = HEATPIPE_PATTERN.search(text)
    if pipe_match and int(pipe_match.group(1)) >= 6:
        return "双塔风冷"
    return "单塔风冷"


def _normalize_fan_model(text: str) -> str:
    upper = text.upper()
    if "140" in upper or "14CM" in upper:
        return "140mm"
    if "90" in upper or "9CM" in upper or "92" in upper:
        return "90mm"
    return "120mm"


def infer_component_subtype(component_type: str, sku_name: str) -> tuple[str, str, str, str, str]:
    text = normalize_text(sku_name)
    if component_type == "CPU":
        model = _normalize_cpu_model(text)
        vendor = infer_cpu_vendor(model or text)
        platform = infer_cpu_platform(model)
        memory_gen = preferred_memory_gen_for_cpu(model, "")
        return model, platform, memory_gen, "", ""
    if component_type == "主板":
        match = MOTHERBOARD_PATTERN.search(text)
        subtype = match.group(1).upper() if match else ""
        platform = infer_board_platform(subtype)
        memory_gen = infer_board_memory_gen(subtype)
        case_tag = infer_case_size_from_board(subtype)
        return subtype, platform, memory_gen, "", case_tag
    if component_type == "内存":
        subtype = _normalize_memory_model(text)
        gen_match = MEMORY_GEN_PATTERN.search(text)
        memory_gen = f"DDR{gen_match.group(1)}" if gen_match else ""
        return subtype, "", memory_gen, "", ""
    if component_type == "显卡":
        subtype = _normalize_gpu_model(text)
        power_tag = str(required_power_for_gpu(subtype)) if subtype else ""
        return subtype, "", "", power_tag, ""
    if component_type == "固态硬盘":
        subtype = _normalize_storage_model(text)
        storage_tag = subtype.split(" ", 1)[0] if subtype else ""
        return subtype, "", "", storage_tag, ""
    if component_type == "电源":
        subtype = _normalize_psu_model(text)
        return subtype, "", "", subtype, ""
    if component_type == "机箱":
        subtype = _normalize_case_model(text)
        return subtype, "", "", "", subtype
    if component_type == "散热器":
        subtype = _normalize_cooler_model(text)
        return subtype, "", "", "", ""
    if component_type == "风扇":
        subtype = _normalize_fan_model(text)
        return subtype, "", "", "", ""
    return "", "", "", "", ""


def is_candidate_ambiguous(component_type: str, sku_name: str, component_subtype: str) -> bool:
    text = normalize_text(sku_name)
    if component_type == "CPU":
        combo_tokens = ["主板", "套装", "板U", "U板", "板u", "套条"]
        if any(token in sku_name for token in combo_tokens):
            return True
    elif component_type == "主板":
        if "CPU" in text and any(token in sku_name for token in ["套装", "板U", "U板"]):
            return True
    elif component_type == "显卡":
        if any(token in sku_name for token in ["整机", "主机", "板U", "套装"]):
            return True
    elif component_type == "固态硬盘":
        required = ["SSD", "固态", "NVME", "M.2", "PCIE"]
        if not any(token in text for token in required):
            return True
    elif component_type == "电源":
        if any(token in sku_name for token in ["散热器", "风冷", "水冷", "机箱风扇"]):
            return True
    elif component_type == "机箱":
        if any(token in sku_name for token in ["风扇", "散热器", "电源"]):
            return True
    return not component_subtype


def infer_cpu_vendor(cpu_model: str) -> str:
    token = (cpu_model or "").upper()
    # Intel 标识
    if token.startswith(("I", "奔腾", "赛扬", "至强")) or token.startswith("ULTRA") or "G7400" in token or "G6900" in token:
        return "Intel"
    return "AMD"


def infer_cpu_platform(cpu_model: str) -> str:
    """推断 CPU 所属插槽平台 (AM5/AM4/LGA1700/LGA1851)。"""
    token = (cpu_model or "").upper()
    # AM5: Ryzen 7000/8000/9000 系列
    if any(marker in token for marker in [
        "7500F", "7600", "7700", "7800", "7900", "7950",
        "8400F", "8500", "8600", "8700",
        "9600", "9700", "9800", "9900", "9950",
        # Ryzen 9000 系列补全
        "9600X", "9700X", "9800X3D", "9900X", "9950X", "9950X3D",
        # Ryzen 8000 APU
        "8500G", "8600G", "8700G",
        # Ryzen 7000 全系
        "7600X", "7700X", "7800X3D", "7900X", "7900X3D", "7950X", "7950X3D",
    ]):
        return "AM5"
    # AM4: Ryzen 3000/4000/5000 系列
    if any(marker in token for marker in [
        "5500", "5600", "5700", "5800", "5900", "5950",
        "3500", "3600", "3700", "3800", "3900", "3950",
        "5600X", "5600G", "5700G", "5700X", "5700X3D", "5800X", "5800X3D",
        "5900X", "5950X",
        "3100", "3300X",
        "4500", "4600G", "4650G",
    ]):
        return "AM4"
    # LGA1851: Core Ultra 200 系列 (Arrow Lake)
    if token.startswith("ULTRA"):
        # Ultra 3/5/7/9 200 series -> LGA1851
        if any(marker in token for marker in ["285", "265", "245", "235", "205"]):
            return "LGA1851"
        # 旧版 Ultra (Meteor Lake) -> LGA1700
        return "LGA1700"
    # LGA1700: Intel 12/13/14 代
    if token.startswith("I") or any(m in token for m in ["G7400", "G6900"]):
        return "LGA1700"
    return "AM4"


def infer_board_platform(board_subtype: str) -> str:
    upper = (board_subtype or "").upper()
    # AMD 芯片组
    if upper.startswith(("B650", "B850", "X670", "X870", "A620", "B550", "A520", "X570", "A320",
                         "B450", "X470", "B350", "X370", "TRX40", "WRX80")):
        return "AMD"
    # Intel 芯片组 (含 LGA1851 新芯片组)
    if upper.startswith(("B760", "B860", "Z790", "Z690", "Z890",
                         "H610", "H710", "H510", "H810",
                         "B660", "H670", "H770",
                         "Z490", "Z590", "B560", "H570")):
        return "Intel"
    return ""


def infer_board_memory_gen(board_subtype: str) -> str:
    upper = (board_subtype or "").upper()
    # DDR5 主板
    if upper.startswith(("B650", "B850", "X670", "X870", "A620", "B860", "Z890", "H810")):
        return "DDR5"
    # DDR4 主板
    if upper.startswith(("B550", "A520", "X570", "A320", "B450", "X470",
                         "H610", "H510", "B660", "H670", "B560", "H570",
                         "B350", "X370", "Z490", "Z590")):
        return "DDR4"
    # 双支持主板 (DDR4 或 DDR5 版本)
    if upper.startswith(("B760", "Z790", "Z690", "H710", "H770")):
        return "DDR4"  # 默认回退到 DDR4（实际要根据具体型号判断）
    return "DDR4"


def infer_case_size_from_board(board_subtype: str) -> str:
    upper = (board_subtype or "").upper()
    if upper.endswith("M"):
        return "MATX机箱"
    return "ATX机箱"


def preferred_memory_gen_for_cpu(cpu_model: str, board_subtype: str) -> str:
    if board_subtype:
        return infer_board_memory_gen(board_subtype)
    platform = infer_cpu_platform(cpu_model)
    if platform == "AM5":
        return "DDR5"
    return "DDR4"


def required_power_for_gpu(gpu_model: str) -> int:
    """获取显卡推荐电源的最低瓦数。先查兼容性引擎的参考数据，再回退启发式。"""
    try:
        compat = get_compatibility_engine()
        watt = compat._gpu_psu_wattage(gpu_model)
        if watt and watt != 550:
            return watt
    except Exception:
        pass
    token = (gpu_model or "").upper()
    # RTX 50 系列
    if "5090" in token:
        return 1000
    if "5080" in token:
        return 850
    if "5070 TI" in token or "4070 SUPER" in token:
        return 750
    if "5070" in token or "4070" in token or "9070" in token:
        return 700
    if "5060 TI" in token or "4060 TI" in token or "3070 TI" in token or "9060 XT" in token:
        return 650
    if "5060" in token or "4060" in token or "3060" in token:
        return 600
    if "3050" in token or "GTX 1650" in token or "GTX 1050" in token:
        return 500
    if "4090" in token or "4080" in token or "7900 XTX" in token or "7900 XT" in token:
        return 850
    if "3090" in token or "3080" in token:
        return 750
    return 550


def estimated_cpu_power(cpu_model: str) -> int:
    """估算CPU典型功耗(TDP)."""
    token = (cpu_model or "").upper()
    # 旗舰级 ~170W+
    if any(m in token for m in ["I9", "ULTRA 9", "9950X", "9900X", "9800X3D", "7950X", "7950X3D"]):
        return 170
    # 高性能 ~120-150W
    if any(m in token for m in ["I7", "ULTRA 7", "9700X", "7900X", "7900X3D", "7800X3D", "5950X"]):
        return 120
    # 中端 ~65-105W
    if any(m in token for m in ["I5", "ULTRA 5", "7600X", "9600X", "7500F", "7600", "7700", "5800X", "5700X", "5900X"]):
        return 90
    # 入门级 ~65W
    if any(m in token for m in ["I3", "ULTRA 3", "5500", "5600", "5600X", "8400F", "8500G", "8600G"]):
        return 65
    return 90


def estimated_cpu_power(cpu_model: str) -> int:
    """估算CPU典型功耗(TDP)."""
    token = (cpu_model or "").upper()
    if any(m in token for m in ["I9", "ULTRA 9", "9950X", "9900X", "9800X3D"]):
        return 170
    if any(m in token for m in ["I7", "ULTRA 7", "9700X", "7900X", "7800X3D"]):
        return 120
    if any(m in token for m in ["I5", "ULTRA 5", "7600X", "9600X", "7500F"]):
        return 90
    if any(m in token for m in ["I3", "5500", "5600", "8400F"]):
        return 65
    return 90


def required_total_power(cpu_model: str, gpu_model: str) -> int:
    """计算整机建议电源瓦数：CPU功耗 + GPU功耗 + 余量."""
    cpu_w = estimated_cpu_power(cpu_model)
    gpu_w = required_power_for_gpu(gpu_model)
    total = cpu_w + gpu_w
    if total <= 450:
        return 550
    if total <= 550:
        return 650
    if total <= 650:
        return 750
    if total <= 750:
        return 850
    return 1000


def gpu_length_tier(gpu_model: str) -> str:
    """估算显卡长度档位."""
    token = (gpu_model or "").upper()
    if any(m in token for m in ["4090", "4080", "5090", "5080", "7900XTX", "7900XT"]):
        return "超长卡(≥340mm)"
    if any(m in token for m in ["5070 TI", "4070 SUPER", "4070 TI", "5070", "4070", "9070"]):
        return "长卡(300-340mm)"
    if any(m in token for m in ["5060 TI", "4060 TI", "5060", "4060", "9060 XT"]):
        return "标准卡(250-300mm)"
    return "短卡(<250mm)"


def infer_psu_connector(psu_subtype: str) -> dict[str, Any]:
    """推断电源是否支持 12VHPWR / ATX 3.x。

    返回 {"has_12vhpwr": bool, "atx_version": str, "modular": str}
    """
    result = {"has_12vhpwr": False, "atx_version": "", "modular": ""}
    text = (psu_subtype or "").upper()
    # 检测 ATX 3.x
    if "ATX 3.1" in text or "ATX3.1" in text:
        result["atx_version"] = "3.1"
        result["has_12vhpwr"] = True
    elif "ATX 3.0" in text or "ATX3.0" in text:
        result["atx_version"] = "3.0"
        result["has_12vhpwr"] = True
    # 检测 12VHPWR 原生支持
    if any(tag in text for tag in ["12VHPWR", "12V-2X6", "PCIE5", "GEN5"]):
        result["has_12vhpwr"] = True
    # 检测模组类型
    if "全模组" in psu_subtype or "FULL" in text or "全模" in psu_subtype:
        result["modular"] = "full"
    elif "半模组" in psu_subtype or "SEMI" in text:
        result["modular"] = "semi"
    elif "非模组" in psu_subtype or "直出" in psu_subtype:
        result["modular"] = "non"
    return result


def infer_gpu_pcie_gen(gpu_model: str) -> str:
    """推断显卡 PCIe 代际。"""
    token = (gpu_model or "").upper()
    # PCIe 5.0: RTX 50 系列 (Blackwell)
    if any(m in token for m in ["5090", "5080", "5070", "5060"]):
        return "Gen5"
    # PCIe 5.0: RX 9000 系列 (RDNA 4)
    if any(m in token for m in ["9070", "9060"]):
        return "Gen5"
    # PCIe 4.0: 大部分现代显卡
    if any(m in token for m in ["4090", "4080", "4070", "4060", "3090", "3080", "3070", "3060",
                                 "7900", "7800", "7700", "7600",
                                 "ARC", "B580", "A770"]):
        return "Gen4"
    # PCIe 3.0: 老卡
    if any(m in token for m in ["GTX 16", "GTX 10", "RX 5", "RX 6"]):
        return "Gen3"
    return "Gen4"


def infer_board_pcie_gen(board_subtype: str) -> str:
    """推断主板 PCIe 代际。"""
    upper = (board_subtype or "").upper()
    # PCIe 5.0 主板
    if any(upper.startswith(cs) for cs in ["X670E", "X870E", "X870", "Z890", "B850"]):
        return "Gen5"
    # PCIe 4.0 主板
    if any(upper.startswith(cs) for cs in ["B650", "X670", "B760", "Z790", "Z690", "B860", "B550", "X570"]):
        return "Gen4"
    # PCIe 3.0
    if any(upper.startswith(cs) for cs in ["B450", "A320", "H310", "B360", "B365"]):
        return "Gen3"
    return "Gen4"


def suggest_form_factor_from_case(case_subtype: str) -> str:
    """从机箱推断推荐的主板板型。"""
    upper = (case_subtype or "").upper()
    if "ITX" in upper or "MINI" in upper:
        return "ITX"
    if "MATX" in upper or "M-ATX" in upper or "MICRO" in upper:
        return "MATX"
    return "ATX"


def check_physical_compatibility(
    results: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """检查物理兼容性并生成警告/说明."""
    warnings: list[dict[str, str]] = []
    cpu_item = None
    gpu_item = None
    case_item = None
    cooler_item = None
    psu_item = None
    board_item = None
    memory_item = None

    for item in results:
        ct = item["component_type"]
        if ct == "CPU":
            cpu_item = item
        elif ct == "显卡":
            gpu_item = item
        elif ct == "机箱":
            case_item = item
        elif ct == "散热器":
            cooler_item = item
        elif ct == "电源":
            psu_item = item
        elif ct == "主板":
            board_item = item
        elif ct == "内存":
            memory_item = item

    # 1. GPU vs 机箱
    if gpu_item and case_item:
        gpu_len = gpu_length_tier(gpu_item["component_subtype"])
        case_size = (case_item.get("case_tag") or case_item.get("component_subtype", ""))
        if "ITX" in case_size and "标准" in gpu_len:
            warnings.append({
                "level": "warning",
                "title": "显卡与机箱兼容提醒",
                "detail": f"{gpu_item['component_subtype']} ({gpu_len}) 可能与 {case_size} 机箱存在空间冲突，建议确认机箱显卡限长。",
            })
        elif "MATX" in case_size and "超长" in gpu_len:
            warnings.append({
                "level": "info",
                "title": "显卡与机箱空间提示",
                "detail": f"{gpu_item['component_subtype']} ({gpu_len}) 可能较长，请确认 {case_size} 机箱显卡限长≥340mm。",
            })

    # 2. 散热器 vs 机箱
    if cooler_item and case_item:
        cooler_type = cooler_item.get("component_subtype", "")
        case_size = (case_item.get("case_tag") or case_item.get("component_subtype", ""))
        if "双塔" in cooler_type and "ITX" in case_size:
            warnings.append({
                "level": "warning",
                "title": "散热器与机箱兼容提醒",
                "detail": f"双塔风冷 ({cooler_type}) 高度可能超出 ITX 机箱限高，建议换用下压式风冷或水冷。",
            })
        if "360水冷" in cooler_type and "ITX" in case_size:
            warnings.append({
                "level": "warning",
                "title": "散热器与机箱兼容提醒",
                "detail": "360 水冷通常不支持 ITX 机箱，请确认机箱是否支持 360 冷排。",
            })

    # 3. 电源功率校验
    if cpu_item and gpu_item and psu_item:
        recommended_w = required_total_power(
            cpu_item["component_subtype"],
            gpu_item["component_subtype"],
        )
        psu_watt_str = (psu_item.get("component_subtype") or "").rstrip("W")
        try:
            psu_watt = int(psu_watt_str) if psu_watt_str.isdigit() else 0
        except ValueError:
            psu_watt = 0
        if psu_watt and psu_watt < recommended_w:
            warnings.append({
                "level": "warning",
                "title": "电源功率不足提醒",
                "detail": f"CPU ({cpu_item['component_subtype']}) + 显卡 ({gpu_item['component_subtype']}) 建议电源 ≥{recommended_w}W，当前选择 {psu_watt}W 可能不够。",
            })
        elif psu_watt:
            warnings.append({
                "level": "info",
                "title": "电源功率确认",
                "detail": f"CPU + 显卡组合建议电源 ≥{recommended_w}W，当前 {psu_watt}W 满足需求。",
            })

    # 4. 加载扩展兼容性引擎，运行增强检查
    try:
        compat = get_compatibility_engine()
        extra_warnings = compat.check_all(
            cpu_subtype=cpu_item["component_subtype"] if cpu_item else "",
            cpu_vendor=infer_cpu_vendor(cpu_item["component_subtype"]) if cpu_item else "",
            cpu_platform=infer_cpu_platform(cpu_item["component_subtype"]) if cpu_item else "",
            gpu_subtype=gpu_item["component_subtype"] if gpu_item else "",
            gpu_length_tier=gpu_length_tier(gpu_item["component_subtype"]) if gpu_item else "",
            board_subtype=board_item["component_subtype"] if board_item else "",
            psu_subtype=psu_item["component_subtype"] if psu_item else "",
            case_subtype=(case_item.get("case_tag") or case_item.get("component_subtype", "")) if case_item else "",
            cooler_subtype=cooler_item["component_subtype"] if cooler_item else "",
            memory_subtype=memory_item["component_subtype"] if memory_item else "",
            memory_gen=memory_item.get("memory_gen", "") if memory_item else "",
        )
        # 合并兼容性检查结果，避免重复
        existing_titles = {w["title"] for w in warnings}
        for ew in extra_warnings:
            if ew["title"] not in existing_titles:
                warnings.append(ew)
    except Exception:
        pass  # 兼容性引擎加载失败时不影响主流程

    return warnings


# ============================================================
# 性能档位数据
# ============================================================

_PERF_DATA: dict[str, Any] | None = None
_PERF_FILE = BASE_DIR / "data" / "performance_tiers.json"


def _load_performance_data() -> dict[str, Any]:
    """加载性能档位 JSON 数据文件。"""
    global _PERF_DATA
    if _PERF_DATA is not None:
        return _PERF_DATA
    try:
        if _PERF_FILE.exists():
            _PERF_DATA = json.loads(_PERF_FILE.read_text(encoding="utf-8"))
            return _PERF_DATA
    except Exception:
        pass
    _PERF_DATA = {"cpu": {}, "gpu": {}, "bottleneck_thresholds": {}}
    return _PERF_DATA


def _lookup_perf_tier(model: str, category: str) -> dict[str, Any] | None:
    """在性能数据中查找型号，支持精确匹配和包含匹配。"""
    data = _load_performance_data()
    table = data.get(category, {})
    if not table:
        return None
    # 精确匹配（标准化大写）
    upper = model.upper().strip()
    if upper in table:
        return dict(table[upper])
    # 包含匹配
    for key, value in table.items():
        if key.upper() in upper or upper in key.upper():
            return dict(value)
    return None


def cpu_performance_tier(cpu_model: str) -> int:
    """获取 CPU 性能档位 1-5。先查 JSON，再回退到启发式。"""
    perf = _lookup_perf_tier(cpu_model, "cpu")
    if perf is not None and "tier" in perf:
        return int(perf["tier"])
    # ---- 回退：启发式 ----
    token = (cpu_model or "").upper()
    if any(marker in token for marker in ["9800X3D", "9700X", "7900X", "ULTRA"]):
        return 5
    if any(marker in token for marker in ["7800X3D", "9600X", "5700X", "14400F"]):
        return 4
    if any(marker in token for marker in ["7500F", "5600X", "12400F", "12490F"]):
        return 3
    if any(marker in token for marker in ["5500", "12100", "8400F"]):
        return 2
    return 1


def gpu_performance_tier(gpu_model: str) -> int:
    """获取 GPU 性能档位 1-5。先查 JSON，再回退到启发式。"""
    perf = _lookup_perf_tier(gpu_model, "gpu")
    if perf is not None and "tier" in perf:
        return int(perf["tier"])
    # ---- 回退：启发式 ----
    token = (gpu_model or "").upper()
    if any(marker in token for marker in ["5070 TI", "4070 SUPER", "9070"]):
        return 5
    if any(marker in token for marker in ["5070", "4070", "9060 XT"]):
        return 4
    if any(marker in token for marker in ["5060 TI", "4060 TI", "3070 TI"]):
        return 3
    if any(marker in token for marker in ["5060", "4060"]):
        return 2
    return 1


def get_cpu_relative_score(cpu_model: str) -> float | None:
    """获取 CPU 相对性能分（以 7500F=100 为基准），用于精确瓶颈分析。"""
    perf = _lookup_perf_tier(cpu_model, "cpu")
    if perf is not None and "relative_score" in perf:
        return float(perf["relative_score"])
    return None


def get_gpu_relative_score(gpu_model: str) -> float | None:
    """获取 GPU 相对性能分，用于精确瓶颈分析。"""
    perf = _lookup_perf_tier(gpu_model, "gpu")
    if perf is not None and "relative_score" in perf:
        return float(perf["relative_score"])
    return None


def analyze_cpu_gpu_balance(cpu_model: str, gpu_model: str) -> dict[str, Any]:
    """分析 CPU/GPU 性能平衡度，返回 {level, ratio, label, suggestion}。

    使用相对性能分计算，比简单的档位差更精确。
    """
    cpu_score = get_cpu_relative_score(cpu_model)
    gpu_score = get_gpu_relative_score(gpu_model)

    # 如果查不到分数，回退到用 tier 估算
    if cpu_score is None or gpu_score is None:
        cpu_tier = cpu_performance_tier(cpu_model)
        gpu_tier = gpu_performance_tier(gpu_model)
        # tier -> 大致分数映射
        tier_to_score = {1: 40, 2: 70, 3: 100, 4: 125, 5: 155}
        cpu_score = tier_to_score.get(cpu_tier, 100)
        gpu_score = tier_to_score.get(gpu_tier, 100)

    thresholds = _load_performance_data().get("bottleneck_thresholds", {})
    balanced = thresholds.get("balanced", 0.70)
    slight = thresholds.get("slight_gap", 0.55)
    severe = thresholds.get("severe_gap", 0.40)

    ratio = min(cpu_score, gpu_score) / max(cpu_score, gpu_score) if max(cpu_score, gpu_score) > 0 else 1.0

    if ratio >= balanced:
        level = "balanced"
        label = "CPU 与显卡性能档位匹配正常"
    elif ratio >= slight:
        level = "slight_gap"
        label = "CPU 与显卡性能存在一定差距"
    elif ratio >= severe:
        level = "gap"
        label = "CPU 与显卡性能档位存在明显差距"
    else:
        level = "severe_gap"
        label = "CPU 与显卡性能严重不匹配"

    # 判断瓶颈方向
    if cpu_score < gpu_score:
        bottleneck = "CPU"
        suggestion = f"CPU ({cpu_model}) 可能成为瓶颈，建议适当升级 CPU 或降低显卡档位"
    elif gpu_score < cpu_score:
        bottleneck = "GPU"
        suggestion = f"显卡 ({gpu_model}) 相对偏弱，如果预算允许建议升级显卡"
    else:
        bottleneck = None
        suggestion = ""

    return {
        "level": level,
        "ratio": round(ratio, 3),
        "cpu_score": cpu_score,
        "gpu_score": gpu_score,
        "label": f"{label}：{cpu_model} / {gpu_model}",
        "bottleneck": bottleneck,
        "suggestion": suggestion,
    }


def parse_price_number(value: str) -> float:
    match = PRICE_NUMBER_PATTERN.search(str(value or ""))
    return float(match.group(1)) if match else 0.0


def discover_saved_market_html_files() -> list[Path]:
    html_files: list[Path] = []
    for directory in [BASE_DIR, TAOBAO_DIR]:
        if not directory.exists():
            continue
        for path in directory.rglob("*淘宝搜索.html"):
            if path.is_file():
                html_files.append(path)
    return sorted(set(html_files), key=lambda item: str(item))


def build_market_items_from_saved_html() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for html_path in discover_saved_market_html_files():
        raw_keyword = infer_keyword_from_html_filename(html_path)
        html = html_path.read_text(encoding="utf-8", errors="ignore")
        extracted = extract_rows_from_html(html)
        for row in extracted:
            sku_name = normalize_text(row.get("sku_name", ""))
            if len(sku_name) < 4:
                continue
            component_type = normalize_component_type(raw_keyword, sku_name)
            if not component_type:
                continue
            price_text = normalize_price(row.get("coupon_price") or row.get("original_price") or "")
            if not price_text:
                continue
            subtype, platform_tag, memory_gen, power_tag, case_tag = infer_component_subtype(component_type, sku_name)
            if not subtype or subtype.startswith("未识别"):
                continue
            if is_candidate_ambiguous(component_type, sku_name, subtype):
                continue
            source_url = normalize_source_url(row.get("source_url", ""))
            rows.append(
                {
                    "source_name": html_path.name,
                    "raw_keyword": raw_keyword,
                    "component_type": component_type,
                    "component_subtype": subtype,
                    "platform_tag": platform_tag,
                    "memory_gen": memory_gen,
                    "storage_tag": subtype.split(" ", 1)[0] if component_type == "固态硬盘" and subtype else "",
                    "power_tag": power_tag,
                    "case_tag": case_tag,
                    "shop": normalize_text(row.get("shop", "")),
                    "sku_name": sku_name,
                    "effective_price": parse_price_number(price_text),
                    "source_url": source_url,
                }
            )

    best_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["component_type"], row["component_subtype"], row["sku_name"])
        best = best_by_key.get(key)
        if best is None or row["effective_price"] < best["effective_price"]:
            best_by_key[key] = row
    return sorted(best_by_key.values(), key=lambda item: (item["component_type"], item["effective_price"], item["component_subtype"]))


def discover_diy_html_files() -> list[Path]:
    if not ZOL_DIR.exists():
        return []
    matched: list[Path] = []
    for root, _, files in os.walk(ZOL_DIR):
        for file in files:
            if not file.endswith(".html"):
                continue
            if "DIY" not in file:
                continue
            path = Path(root) / file
            if "_files" in path.parts:
                continue
            matched.append(path)
    return sorted(matched, key=lambda item: str(item))


def parse_diy_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for html_path in discover_diy_html_files():
        html = html_path.read_text(encoding="gb18030", errors="ignore")
        soup = BeautifulSoup(html, "html.parser")
        for li in soup.select("a.pic"):
            container = li.find_parent("li")
            if container is None:
                continue
            img = li.find("img")
            title = normalize_text((img.get("alt") if img else "") or "")
            if not title:
                continue
            heading = container.find("h3")
            desc = normalize_text(heading.get_text(" ", strip=True) if heading else "")
            price_tag = container.select_one(".price-type")
            ref_price = parse_price_number(price_tag.get_text(strip=True) if price_tag else "")
            full_text = normalize_text(" ".join(part for part in [title, desc] if part))
            cpu_model = _normalize_cpu_model(full_text)
            gpu_model = _normalize_gpu_model(full_text)
            memory_model = _normalize_memory_model(full_text)
            storage_model = _normalize_storage_model(full_text)
            config_type_match = re.search(r"配置类型:([^\s]+)", desc)
            cpu_series_match = re.search(r"CPU系列:([^\s]+)", desc)
            tokens: list[str] = []
            if cpu_model:
                tokens.append(f"CPU:{cpu_model}")
            if gpu_model:
                tokens.append(f"GPU:{gpu_model}")
            if memory_model:
                tokens.append(f"MEMORY:{memory_model}")
            if storage_model:
                tokens.append(f"STORAGE:{storage_model}")
            if not cpu_model or len(tokens) < 2:
                continue
            cases.append(
                {
                    "source_name": html_path.name,
                    "title": title,
                    "detail_url": li.get("href", ""),
                    "ref_price": ref_price,
                    "config_type": config_type_match.group(1) if config_type_match else "",
                    "cpu_series": cpu_series_match.group(1) if cpu_series_match else "",
                    "cpu_model": cpu_model,
                    "gpu_model": gpu_model,
                    "memory_model": memory_model,
                    "storage_model": storage_model,
                    "tokens": sorted(set(tokens)),
                }
            )
    return cases


def build_association_rules(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not cases:
        return []
    item_counter: Counter[str] = Counter()
    pair_counter: Counter[tuple[str, str]] = Counter()
    transaction_count = len(cases)
    for case in cases:
        tokens = sorted(set(case["tokens"]))
        for token in tokens:
            item_counter[token] += 1
        for left, right in combinations(tokens, 2):
            pair_counter[(left, right)] += 1
            pair_counter[(right, left)] += 1

    rules: list[dict[str, Any]] = []
    for (antecedent, consequent), support_count in pair_counter.items():
        if support_count < 2:
            continue
        support_ratio = support_count / transaction_count
        confidence = support_count / item_counter[antecedent]
        consequent_support = item_counter[consequent] / transaction_count
        lift = confidence / consequent_support if consequent_support else 0.0
        if confidence < 0.15:
            continue
        rules.append(
            {
                "antecedent": antecedent,
                "consequent": consequent,
                "support_count": support_count,
                "support_ratio": round(support_ratio, 4),
                "confidence": round(confidence, 4),
                "lift": round(lift, 4),
            }
        )
    rules.sort(key=lambda item: (-item["confidence"], -item["support_count"], item["antecedent"], item["consequent"]))
    return rules


def build_market_items_from_snapshot() -> list[dict[str, Any]]:
    """从 MySQL pc_part_market_snapshot 表读取数据填充装机配件池（HTML 文件缺失时的兜底方案）."""
    rows_raw = run_mysql_query(
        "USE school_market; SELECT crawl_batch_id, crawl_time, keyword, page_no, source_platform, "
        "shop, sku_name, COALESCE(coupon_price, original_price) AS price, source_url "
        "FROM pc_part_market_snapshot WHERE is_dirty_data = 0 ORDER BY crawl_time DESC"
    )
    items: list[dict[str, Any]] = []
    seen = set()
    for row in rows_raw:
        batch_id, crawl_time, keyword, page_no, platform, shop, sku_name, price, url = row
        sku_name_clean = normalize_text(sku_name)
        raw_keyword = normalize_text(keyword)
        if len(sku_name_clean) < 4:
            continue
        component_type = normalize_component_type(raw_keyword, sku_name_clean)
        if not component_type:
            continue
        price_val = float(price) if price else 0
        if price_val <= 0:
            continue
        subtype, platform_tag, memory_gen, power_tag, case_tag = infer_component_subtype(component_type, sku_name_clean)
        if not subtype or subtype.startswith("未识别"):
            continue
        dedup_key = (component_type, subtype, sku_name_clean)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        items.append({
            "source_name": f"snapshot::{keyword}",
            "raw_keyword": raw_keyword,
            "component_type": component_type,
            "component_subtype": subtype,
            "platform_tag": platform_tag,
            "memory_gen": memory_gen,
            "storage_tag": subtype.split(" ", 1)[0] if component_type == "固态硬盘" and subtype else "",
            "power_tag": power_tag,
            "case_tag": case_tag,
            "shop": normalize_text(shop),
            "sku_name": sku_name_clean,
            "effective_price": price_val,
            "source_url": normalize_source_url(url or ""),
            "source_platform": platform or "taobao",
        })
    return items


def refresh_build_knowledge() -> dict[str, int]:
    ensure_build_tables()
    # 优先从 HTML 文件解析，缺失的从数据库快照补充
    html_items = build_market_items_from_saved_html()
    snapshot_items = build_market_items_from_snapshot()
    html_types = {item["component_type"] for item in html_items}
    market_items = html_items + [item for item in snapshot_items if item["component_type"] not in html_types]
    cases = parse_diy_cases()
    rules = build_association_rules(cases)

    statements = [
        "USE school_market;",
        "DELETE FROM pc_build_market_item;",
        "DELETE FROM pc_build_case_sample;",
        "DELETE FROM pc_build_association_rule;",
        "DELETE FROM pc_build_budget_template;",
        "DELETE FROM pc_build_compatibility_rule;",
    ]
    for row in market_items:
        statements.append(
            f"""
INSERT INTO pc_build_market_item (
  source_name, raw_keyword, component_type, component_subtype, platform_tag, memory_gen,
  storage_tag, power_tag, case_tag, shop, sku_name, effective_price, source_url, source_platform
) VALUES (
  '{sql_escape(row["source_name"])}',
  '{sql_escape(row["raw_keyword"])}',
  '{sql_escape(row["component_type"])}',
  '{sql_escape(row["component_subtype"])}',
  {('NULL' if not row["platform_tag"] else "'" + sql_escape(row["platform_tag"]) + "'")},
  {('NULL' if not row["memory_gen"] else "'" + sql_escape(row["memory_gen"]) + "'")},
  {('NULL' if not row["storage_tag"] else "'" + sql_escape(row["storage_tag"]) + "'")},
  {('NULL' if not row["power_tag"] else "'" + sql_escape(row["power_tag"]) + "'")},
  {('NULL' if not row["case_tag"] else "'" + sql_escape(row["case_tag"]) + "'")},
  {('NULL' if not row["shop"] else "'" + sql_escape(row["shop"]) + "'")},
  '{sql_escape(row["sku_name"])}',
  {round(row["effective_price"], 2)},
  {('NULL' if not row["source_url"] else "'" + sql_escape(row["source_url"]) + "'")},
  'taobao'
);""".strip()
        )
    for case in cases:
        statements.append(
            f"""
INSERT INTO pc_build_case_sample (
  source_name, title, detail_url, ref_price, config_type, cpu_series, cpu_model,
  gpu_model, memory_model, storage_model, build_tokens_json
) VALUES (
  '{sql_escape(case["source_name"])}',
  '{sql_escape(case["title"])}',
  {('NULL' if not case["detail_url"] else "'" + sql_escape(case["detail_url"]) + "'")},
  {('NULL' if not case["ref_price"] else round(case["ref_price"], 2))},
  {('NULL' if not case["config_type"] else "'" + sql_escape(case["config_type"]) + "'")},
  {('NULL' if not case["cpu_series"] else "'" + sql_escape(case["cpu_series"]) + "'")},
  {('NULL' if not case["cpu_model"] else "'" + sql_escape(case["cpu_model"]) + "'")},
  {('NULL' if not case["gpu_model"] else "'" + sql_escape(case["gpu_model"]) + "'")},
  {('NULL' if not case["memory_model"] else "'" + sql_escape(case["memory_model"]) + "'")},
  {('NULL' if not case["storage_model"] else "'" + sql_escape(case["storage_model"]) + "'")},
  '{sql_escape(json.dumps(case["tokens"], ensure_ascii=False))}'
);""".strip()
        )
    for rule in rules:
        statements.append(
            f"""
INSERT INTO pc_build_association_rule (
  antecedent, consequent, support_count, support_ratio, confidence, lift, source_name
) VALUES (
  '{sql_escape(rule["antecedent"])}',
  '{sql_escape(rule["consequent"])}',
  {rule["support_count"]},
  {rule["support_ratio"]},
  {rule["confidence"]},
  {rule["lift"]},
  'DIY组装电脑'
);""".strip()
        )
    for scenario_code, template in BUDGET_TEMPLATES.items():
        for component_type, ratio_value in template["ratios"].items():
            statements.append(
                f"""
INSERT INTO pc_build_budget_template (scenario_code, component_type, ratio_value)
VALUES ('{scenario_code}', '{sql_escape(component_type)}', {round(ratio_value, 4)});""".strip()
            )
    for rule in COMPATIBILITY_RULES:
        statements.append(
            f"""
INSERT INTO pc_build_compatibility_rule (component_type, rule_key, rule_value, compatible_values_json, note)
VALUES (
  '{sql_escape(rule["component_type"])}',
  '{sql_escape(rule["rule_key"])}',
  '{sql_escape(rule["rule_value"])}',
  '{sql_escape(json.dumps(rule["compatible_values"], ensure_ascii=False))}',
  'static-default'
);""".strip()
        )

    run_mysql_script("\n".join(statements))
    return {"market_items": len(market_items), "case_samples": len(cases), "association_rules": len(rules)}


def ensure_knowledge_ready() -> None:
    ensure_build_tables()
    rows = run_mysql_query("SELECT COUNT(*) FROM pc_build_market_item;")
    if not rows or int(rows[0][0]) == 0:
        refresh_build_knowledge()


def load_market_pool() -> dict[str, list[dict[str, Any]]]:
    ensure_knowledge_ready()
    rows = run_mysql_query(
        """
        SELECT
          component_type,
          component_subtype,
          COALESCE(platform_tag, ''),
          COALESCE(memory_gen, ''),
          COALESCE(storage_tag, ''),
          COALESCE(power_tag, ''),
          COALESCE(case_tag, ''),
          COALESCE(shop, ''),
          sku_name,
          effective_price,
          COALESCE(source_url, '')
        FROM pc_build_market_item
        ORDER BY component_type, effective_price ASC, component_subtype ASC;
        """
    )
    pool: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        pool[row[0]].append(
            {
                "component_type": row[0],
                "component_subtype": row[1],
                "platform_tag": row[2],
                "memory_gen": row[3],
                "storage_tag": row[4],
                "power_tag": row[5],
                "case_tag": row[6],
                "shop": row[7],
                "sku_name": row[8],
                "effective_price": float(row[9]),
                "source_url": row[10],
            }
        )
    return pool


def load_rules_by_antecedent() -> dict[str, list[dict[str, Any]]]:
    ensure_knowledge_ready()
    rows = run_mysql_query(
        """
        SELECT antecedent, consequent, support_count, support_ratio, confidence, lift
        FROM pc_build_association_rule
        ORDER BY antecedent, confidence DESC, support_count DESC, lift DESC;
        """
    )
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        result[row[0]].append(
            {
                "consequent": row[1],
                "support_count": int(row[2]),
                "support_ratio": float(row[3]),
                "confidence": float(row[4]),
                "lift": float(row[5]),
            }
        )
    return result


def get_build_form_options() -> dict[str, list[str]]:
    pool = load_market_pool()
    return {
        "cpu": sorted({item["component_subtype"] for item in pool.get("CPU", [])}),
        "gpu": sorted({item["component_subtype"] for item in pool.get("显卡", [])}),
        "motherboard": sorted({item["component_subtype"] for item in pool.get("主板", [])}),
        "memory": sorted({item["component_subtype"] for item in pool.get("内存", [])}),
        "storage": sorted({item["component_subtype"] for item in pool.get("固态硬盘", [])}),
        "power": sorted({item["component_subtype"] for item in pool.get("电源", [])}),
        "case": sorted({item["component_subtype"] for item in pool.get("机箱", [])}),
    }


def _choose_exact(pool: list[dict[str, Any]], subtype: str) -> dict[str, Any] | None:
    for item in pool:
        if item["component_subtype"] == subtype:
            return item
    return None


def learn_user_build_preferences(user_id: int) -> dict[str, Any]:
    """分析用户历史装机记录,提取偏好权重."""
    preferences: dict[str, Any] = {
        "cpu_vendor": "",
        "cpu_vendor_count": 0,
        "gpu_vendor": "",
        "gpu_vendor_count": 0,
        "preferred_brands": [],
        "preferred_keywords": [],
        "avg_budget": 0.0,
        "total_tasks": 0,
    }

    task_rows = run_mysql_query(
        f"""
        SELECT
          t.budget,
          t.usage_scenario,
          ri.component_type,
          ri.component_subtype,
          ri.shop,
          ri.sku_name
        FROM pc_build_task t
        JOIN pc_build_result_item ri ON ri.task_id = t.id
        WHERE t.user_id = {user_id}
        ORDER BY t.id DESC, ri.component_type
        LIMIT 200;
        """
    )
    if not task_rows:
        return preferences

    preferences["total_tasks"] = len(
        set(row[0] for row in task_rows if row[0])
    )

    cpu_vendors: Counter[str] = Counter()
    gpu_vendors: Counter[str] = Counter()
    all_brands: Counter[str] = Counter()
    budgets: list[float] = []

    for row in task_rows:
        budget_val = float(row[0]) if row[0] else 0
        if budget_val > 0:
            budgets.append(budget_val)
        comp_type = row[2]
        subtype = (row[3] or "").upper()
        shop = (row[4] or "")
        sku_name = (row[5] or "")

        if comp_type == "CPU":
            if subtype.startswith("I") or "INTEL" in subtype or "ULTRA" in subtype or "奔腾" in subtype:
                cpu_vendors["Intel"] += 1
            elif subtype:
                cpu_vendors["AMD"] += 1
        if comp_type == "显卡":
            if "RTX" in subtype or "GTX" in subtype or "GT " in subtype:
                gpu_vendors["NVIDIA"] += 1
            elif "RX" in subtype:
                gpu_vendors["AMD"] += 1
            elif "ARC" in subtype:
                gpu_vendors["Intel"] += 1

        for brand in ["华硕", "微星", "技嘉", "华擎", "七彩虹", "影驰", "索泰", "铭瑄",
                       "金士顿", "威刚", "海盗船", "芝奇", "三星", "西数", "希捷",
                       "长城", "航嘉", "爱国者", "利民", "九州风神", "酷冷至尊",
                       "先马", "恩杰", "追风者", "安钛克", "振华", "海韵"]:
            if brand in sku_name or brand in shop:
                all_brands[brand] += 1

    if cpu_vendors:
        preferences["cpu_vendor"] = cpu_vendors.most_common(1)[0][0]
        preferences["cpu_vendor_count"] = cpu_vendors.most_common(1)[0][1]
    if gpu_vendors:
        preferences["gpu_vendor"] = gpu_vendors.most_common(1)[0][0]
        preferences["gpu_vendor_count"] = gpu_vendors.most_common(1)[0][1]
    if all_brands:
        preferences["preferred_brands"] = [
            brand for brand, _count in all_brands.most_common(5)
        ]
    if budgets:
        preferences["avg_budget"] = round(sum(budgets) / len(budgets), 2)

    return preferences


def _pick_best_by_budget(
    pool: list[dict[str, Any]],
    target_budget: float,
    prefer_subtypes: list[str] | None = None,
    predicate: Any = None,
    user_prefs: dict[str, Any] | None = None,
    soft_constraints: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_score = -1e18
    prefer_subtypes = prefer_subtypes or []
    prefs = user_prefs or {}
    preferred_brands = prefs.get("preferred_brands", []) or []
    soft = soft_constraints or prefs.get("_soft_constraints") or {}

    white_requested = bool(soft.get("white_theme"))
    quiet_requested = bool(soft.get("quiet_mode"))
    soft_brands = soft.get("brands", []) or []
    soft_keywords = soft.get("keywords", []) or []

    # ---- 市场分析：预计算同 subtype 的价格中位数 ----
    subtype_prices: dict[str, list[float]] = {}
    for item in pool:
        if predicate and not predicate(item):
            continue
        st = item.get("component_subtype", "")
        if st:
            subtype_prices.setdefault(st, []).append(item["effective_price"])
    subtype_median: dict[str, float] = {}
    subtype_range: dict[str, float] = {}  # max - min, for normalization
    for st, prices in subtype_prices.items():
        sorted_p = sorted(prices)
        n = len(sorted_p)
        if n > 0:
            subtype_median[st] = sorted_p[n // 2]
            subtype_range[st] = max(sorted_p[-1] - sorted_p[0], 1.0)
        else:
            subtype_median[st] = 0.0
            subtype_range[st] = 1.0

    # 评分缩放系数：让固定加分与预算规模成比例
    scale = max(target_budget / 4000.0, 0.5) if target_budget > 0 else 1.0

    for item in pool:
        if predicate and not predicate(item):
            continue
        delta = abs(item["effective_price"] - target_budget)
        score = -delta
        # 预算内加分 (按比例缩放)
        if item["effective_price"] <= target_budget:
            score += 120 * scale

        if item["component_subtype"] in prefer_subtypes:
            score += (180 - prefer_subtypes.index(item["component_subtype"]) * 10) * scale

        # ---- 市场位置加分：低于中位数 = 好价 ----
        st = item.get("component_subtype", "")
        median = subtype_median.get(st, item["effective_price"])
        price_range = subtype_range.get(st, 1.0)
        if median > 0 and item["effective_price"] <= median:
            # 价格越低于中位数 (相对于价格区间)，加分越多
            percentile_bonus = (median - item["effective_price"]) / price_range * 50
            score += min(percentile_bonus, 50)

        # ---- 性价比加分：tier / price 归一化 ----
        ct = item.get("component_type", "")
        if ct in ("CPU", "显卡"):
            try:
                if ct == "CPU":
                    tier = cpu_performance_tier(item["component_subtype"])
                else:
                    tier = gpu_performance_tier(item["component_subtype"])
                if item["effective_price"] > 0:
                    value_ratio = tier / item["effective_price"]  # tier per yuan
                    # 归一化到 0~80
                    value_score = min(value_ratio * 1000, 80)
                    score += value_score
            except Exception:
                pass

        # 用户品牌偏好加分 (按比例缩放)
        if preferred_brands:
            item_text = f"{item.get('shop', '')} {item.get('sku_name', '')}".lower()
            for i, brand in enumerate(preferred_brands[:5]):
                if brand.lower() in item_text:
                    score += (150 - i * 25) * scale
                    break

        # ---- 软约束评分 ----
        item_text = f"{item.get('shop', '')} {item.get('sku_name', '')} {item.get('component_subtype', '')}".lower()

        if white_requested:
            if any(token in item_text for token in ["白", "雪", "冰", "海景"]):
                score += 60
            if item.get("component_type") in ("显卡", "电源") and any(
                token in item_text for token in ["白", "雪", "冰"]
            ):
                score += 25

        if quiet_requested:
            if any(token in item_text for token in ["静音", "低噪", "silent", "风量"]):
                score += 60

        if soft_brands:
            for i, brand in enumerate(soft_brands[:5]):
                if brand.lower() in item_text:
                    score += 80 - i * 20
                    break

        if soft_keywords:
            for i, kw in enumerate(soft_keywords[:5]):
                if kw.lower() in item_text:
                    score += 50 - i * 15
                    break

        if best is None or score > best_score:
            best = item
            best_score = score
    if best is not None:
        best = dict(best)
        best["selection_score"] = round(best_score, 4)
    return best


def _extract_rule_subtypes(rules_by_antecedent: dict[str, list[dict[str, Any]]], antecedent: str, component_prefix: str) -> list[str]:
    rows = rules_by_antecedent.get(antecedent, [])
    values = []
    seen = set()
    for row in rows:
        consequent = row["consequent"]
        if not consequent.startswith(component_prefix):
            continue
        subtype = consequent.split(":", 1)[1]
        if subtype in seen:
            continue
        seen.add(subtype)
        values.append(subtype)
    return values


# ============================================================
# 动态预算比例
# ============================================================

# 每个场景在不同预算锚点的分配比例。锚点之间的预算使用线性插值。
# 每个场景定义 (budget, ratios_dict) 锚点，ratios 只列出有变化的组件。
ANCHOR_BUDGET_RATIOS: dict[str, list[tuple[float, dict[str, float]]]] = {
    "gaming": [
        (3000,  {"CPU": 0.25, "主板": 0.16, "内存": 0.14, "显卡": 0.28, "固态硬盘": 0.07, "电源": 0.06, "机箱": 0.03, "散热器": 0.01}),
        (5000,  {"CPU": 0.23, "主板": 0.14, "内存": 0.12, "显卡": 0.33, "固态硬盘": 0.09, "电源": 0.05, "机箱": 0.03, "散热器": 0.01}),
        (8000,  {"CPU": 0.21, "主板": 0.13, "内存": 0.11, "显卡": 0.35, "固态硬盘": 0.09, "电源": 0.06, "机箱": 0.03, "散热器": 0.02}),
        (12000, {"CPU": 0.18, "主板": 0.11, "内存": 0.10, "显卡": 0.40, "固态硬盘": 0.09, "电源": 0.07, "机箱": 0.03, "散热器": 0.02}),
        (16000, {"CPU": 0.15, "主板": 0.10, "内存": 0.10, "显卡": 0.43, "固态硬盘": 0.08, "电源": 0.08, "机箱": 0.03, "散热器": 0.03}),
    ],
    "office": [
        (2000,  {"CPU": 0.32, "主板": 0.20, "内存": 0.18, "固态硬盘": 0.12, "电源": 0.08, "机箱": 0.06, "散热器": 0.04}),
        (4000,  {"CPU": 0.30, "主板": 0.18, "内存": 0.16, "固态硬盘": 0.16, "电源": 0.09, "机箱": 0.06, "散热器": 0.05}),
        (7000,  {"CPU": 0.27, "主板": 0.16, "内存": 0.15, "固态硬盘": 0.18, "电源": 0.10, "机箱": 0.08, "散热器": 0.06}),
    ],
    "design": [
        (4000,  {"CPU": 0.27, "主板": 0.14, "内存": 0.17, "显卡": 0.23, "固态硬盘": 0.09, "电源": 0.05, "机箱": 0.03, "散热器": 0.02}),
        (7000,  {"CPU": 0.24, "主板": 0.12, "内存": 0.15, "显卡": 0.28, "固态硬盘": 0.11, "电源": 0.05, "机箱": 0.03, "散热器": 0.02}),
        (11000, {"CPU": 0.20, "主板": 0.10, "内存": 0.13, "显卡": 0.34, "固态硬盘": 0.12, "电源": 0.06, "机箱": 0.03, "散热器": 0.02}),
    ],
    "streaming": [
        (4000,  {"CPU": 0.26, "主板": 0.14, "内存": 0.16, "显卡": 0.24, "固态硬盘": 0.10, "电源": 0.05, "机箱": 0.03, "散热器": 0.02}),
        (7000,  {"CPU": 0.23, "主板": 0.12, "内存": 0.14, "显卡": 0.27, "固态硬盘": 0.12, "电源": 0.06, "机箱": 0.04, "散热器": 0.02}),
        (11000, {"CPU": 0.20, "主板": 0.11, "内存": 0.12, "显卡": 0.33, "固态硬盘": 0.12, "电源": 0.06, "机箱": 0.04, "散热器": 0.02}),
    ],
}


def get_dynamic_ratios(budget: float, scenario_code: str) -> dict[str, float]:
    """根据预算和场景返回动态比例。

    在 ANCHOR_BUDGET_RATIOS 锚点之间线性插值。
    如果场景无锚点数据，回退到静态 BUDGET_TEMPLATES。
    """
    anchors = ANCHOR_BUDGET_RATIOS.get(scenario_code)
    if not anchors:
        return dict(BUDGET_TEMPLATES.get(scenario_code, BUDGET_TEMPLATES["gaming"])["ratios"])

    # budget 在锚点范围内
    if budget <= anchors[0][0]:
        return dict(anchors[0][1])
    if budget >= anchors[-1][0]:
        return dict(anchors[-1][1])

    # 线性插值
    for i in range(len(anchors) - 1):
        b_lo, r_lo = anchors[i]
        b_hi, r_hi = anchors[i + 1]
        if b_lo <= budget <= b_hi:
            t = (budget - b_lo) / (b_hi - b_lo) if b_hi != b_lo else 0.0
            # 合并两个锚点的所有键
            all_keys = set(r_lo.keys()) | set(r_hi.keys())
            result: dict[str, float] = {}
            for key in all_keys:
                v_lo = r_lo.get(key, r_hi.get(key, 0.0))
                v_hi = r_hi.get(key, r_lo.get(key, 0.0))
                result[key] = round(v_lo + (v_hi - v_lo) * t, 4)
            # 归一化确保总和为 1.0
            total = sum(result.values())
            if total > 0:
                result = {k: round(v / total, 4) for k, v in result.items()}
            return result

    return dict(BUDGET_TEMPLATES.get(scenario_code, BUDGET_TEMPLATES["gaming"])["ratios"])


def _scenario_template(code: str, budget: float | None = None) -> dict[str, Any]:
    """获取场景模板，可选根据预算动态调整比例。"""
    base = BUDGET_TEMPLATES.get(code, BUDGET_TEMPLATES["gaming"])
    if budget is not None and budget > 0:
        dynamic = get_dynamic_ratios(budget, code)
        # 返回带动态比例的模板副本
        result = dict(base)
        result["ratios"] = dynamic
        return result
    return base


def _default_memory_capacity(scenario_code: str) -> int:
    return int(_scenario_template(scenario_code)["memory_capacity"])


def _default_storage_capacity(scenario_code: str) -> str:
    return str(_scenario_template(scenario_code)["storage_capacity"])


def _cpu_candidates_for_budget(pool: list[dict[str, Any]], target_budget: float) -> list[dict[str, Any]]:
    filtered = [item for item in pool if item["component_subtype"]]
    return sorted(filtered, key=lambda item: (abs(item["effective_price"] - target_budget), item["effective_price"]))[:8]


def choose_cpu(pool: dict[str, list[dict[str, Any]]], rules_by_antecedent: dict[str, list[dict[str, Any]]], budget: float, scenario_code: str, selected_parts: dict[str, str], user_prefs: dict[str, Any] | None = None) -> dict[str, Any] | None:
    cpu_pool = pool.get("CPU", [])
    if selected_parts.get("CPU"):
        return _choose_exact(cpu_pool, selected_parts["CPU"])

    cpu_target = budget * _scenario_template(scenario_code, budget)["ratios"].get("CPU", 0.2)
    if selected_parts.get("显卡"):
        preferred = _extract_rule_subtypes(rules_by_antecedent, f"GPU:{selected_parts['显卡']}", "CPU:")
        chosen = _pick_best_by_budget(cpu_pool, cpu_target, prefer_subtypes=preferred, user_prefs=user_prefs)
        if chosen:
            return chosen

    # 用户CPU厂商偏好
    cpu_vendor_pref = (user_prefs or {}).get("cpu_vendor", "")
    prefs = user_prefs or {}
    if cpu_vendor_pref:
        def _cpu_vendor_predicate(item: dict[str, Any]) -> bool:
            vendor = infer_cpu_vendor(item["component_subtype"])
            return vendor == cpu_vendor_pref
        chosen = _pick_best_by_budget(cpu_pool, cpu_target, user_prefs=prefs, predicate=_cpu_vendor_predicate)
        if chosen:
            return chosen

    candidates = _cpu_candidates_for_budget(cpu_pool, cpu_target)
    return candidates[0] if candidates else None


def choose_gpu(pool: dict[str, list[dict[str, Any]]], rules_by_antecedent: dict[str, list[dict[str, Any]]], budget: float, scenario_code: str, selected_parts: dict[str, str], cpu_item: dict[str, Any] | None, user_prefs: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if not _scenario_template(scenario_code)["discrete_gpu"] and not selected_parts.get("显卡"):
        return None
    gpu_pool = pool.get("显卡", [])
    if selected_parts.get("显卡"):
        return _choose_exact(gpu_pool, selected_parts["显卡"])
    target = budget * _scenario_template(scenario_code, budget)["ratios"].get("显卡", 0.28)
    preferred: list[str] = []
    if cpu_item:
        preferred = _extract_rule_subtypes(rules_by_antecedent, f"CPU:{cpu_item['component_subtype']}", "GPU:")
    return _pick_best_by_budget(gpu_pool, target, prefer_subtypes=preferred, user_prefs=user_prefs)


def choose_motherboard(pool: dict[str, list[dict[str, Any]]], budget: float, scenario_code: str, selected_parts: dict[str, str], cpu_item: dict[str, Any] | None, user_prefs: dict[str, Any] | None = None) -> dict[str, Any] | None:
    board_pool = pool.get("主板", [])
    if selected_parts.get("主板"):
        return _choose_exact(board_pool, selected_parts["主板"])
    if not cpu_item:
        return _pick_best_by_budget(board_pool, budget * _scenario_template(scenario_code, budget)["ratios"].get("主板", 0.13), user_prefs=user_prefs)
    cpu_model = cpu_item["component_subtype"]
    vendor = infer_cpu_vendor(cpu_model)
    platform = infer_cpu_platform(cpu_model)
    target = budget * _scenario_template(scenario_code, budget)["ratios"].get("主板", 0.13)

    # 从兼容性引擎获取扩展芯片组列表
    try:
        compat = get_compatibility_engine()
        compatible_chipsets = compat.get_compatible_chipsets(platform)
    except Exception:
        compatible_chipsets = []

    def predicate(item: dict[str, Any]) -> bool:
        board = item["component_subtype"]
        # 厂商检查
        if vendor == "AMD" and infer_board_platform(board) != "AMD":
            return False
        if vendor == "Intel" and infer_board_platform(board) != "Intel":
            return False
        # 芯片组检查：优先用兼容性引擎的扩展列表，回退到硬编码
        if compatible_chipsets:
            upper = board.upper()
            return any(cs.upper() in upper for cs in compatible_chipsets)
        # 回退：原有硬编码逻辑
        if platform == "AM5" and not board.startswith(("B650", "B850", "X670", "X870", "A620")):
            return False
        if platform == "AM4" and not board.startswith(("B550", "A520", "X570", "A320")):
            return False
        return True
    return _pick_best_by_budget(board_pool, target, predicate=predicate, user_prefs=user_prefs)


def choose_memory(pool: dict[str, list[dict[str, Any]]], budget: float, scenario_code: str, selected_parts: dict[str, str], cpu_item: dict[str, Any] | None, motherboard_item: dict[str, Any] | None, user_prefs: dict[str, Any] | None = None) -> dict[str, Any] | None:
    memory_pool = pool.get("内存", [])
    if selected_parts.get("内存"):
        return _choose_exact(memory_pool, selected_parts["内存"])
    target = budget * _scenario_template(scenario_code, budget)["ratios"].get("内存", 0.1)
    preferred_gen = preferred_memory_gen_for_cpu(
        cpu_item["component_subtype"] if cpu_item else "",
        motherboard_item["component_subtype"] if motherboard_item else "",
    )
    preferred_capacity = f"{_default_memory_capacity(scenario_code)}GB"
    preferred = [f"{preferred_gen} {preferred_capacity}", preferred_gen]
    return _pick_best_by_budget(
        memory_pool,
        target,
        prefer_subtypes=preferred,
        predicate=lambda item: item["memory_gen"] in ("", preferred_gen) or item["component_subtype"].startswith(preferred_gen),
        user_prefs=user_prefs,
    )


def choose_storage(pool: dict[str, list[dict[str, Any]]], budget: float, scenario_code: str, selected_parts: dict[str, str], user_prefs: dict[str, Any] | None = None) -> dict[str, Any] | None:
    storage_pool = pool.get("固态硬盘", [])
    if selected_parts.get("固态硬盘"):
        return _choose_exact(storage_pool, selected_parts["固态硬盘"])
    target = budget * _scenario_template(scenario_code, budget)["ratios"].get("固态硬盘", 0.1)
    preferred_capacity = _default_storage_capacity(scenario_code)
    preferred = [prefix for prefix in [preferred_capacity, "1TB", "512GB"] if prefix]
    return _pick_best_by_budget(
        storage_pool,
        target,
        prefer_subtypes=[],
        predicate=lambda item: any(item["component_subtype"].startswith(prefix) for prefix in preferred),
        user_prefs=user_prefs,
    ) or _pick_best_by_budget(storage_pool, target, user_prefs=user_prefs)


def choose_power(pool: dict[str, list[dict[str, Any]]], budget: float, scenario_code: str, selected_parts: dict[str, str], gpu_item: dict[str, Any] | None, user_prefs: dict[str, Any] | None = None) -> dict[str, Any] | None:
    psu_pool = pool.get("电源", [])
    if selected_parts.get("电源"):
        return _choose_exact(psu_pool, selected_parts["电源"])
    target = budget * _scenario_template(scenario_code, budget)["ratios"].get("电源", 0.06)
    required = required_power_for_gpu(gpu_item["component_subtype"]) if gpu_item else 450
    return _pick_best_by_budget(
        psu_pool,
        target,
        predicate=lambda item: int(item["component_subtype"].rstrip("W") or "0") >= required if item["component_subtype"].endswith("W") else True,
        user_prefs=user_prefs,
    ) or _pick_best_by_budget(psu_pool, target, user_prefs=user_prefs)


def choose_case(pool: dict[str, list[dict[str, Any]]], budget: float, scenario_code: str, selected_parts: dict[str, str], motherboard_item: dict[str, Any] | None, user_prefs: dict[str, Any] | None = None) -> dict[str, Any] | None:
    case_pool = pool.get("机箱", [])
    if selected_parts.get("机箱"):
        return _choose_exact(case_pool, selected_parts["机箱"])
    target = budget * _scenario_template(scenario_code, budget)["ratios"].get("机箱", 0.03)
    preferred = [motherboard_item["case_tag"]] if motherboard_item and motherboard_item.get("case_tag") else []
    return _pick_best_by_budget(case_pool, target, prefer_subtypes=preferred, user_prefs=user_prefs) or _pick_best_by_budget(case_pool, target, user_prefs=user_prefs)


def choose_cooler(pool: dict[str, list[dict[str, Any]]], budget: float, scenario_code: str, selected_parts: dict[str, str], cpu_item: dict[str, Any] | None, user_prefs: dict[str, Any] | None = None) -> dict[str, Any] | None:
    cooler_pool = pool.get("散热器", [])
    if selected_parts.get("散热器"):
        return _choose_exact(cooler_pool, selected_parts["散热器"])
    target = budget * _scenario_template(scenario_code, budget)["ratios"].get("散热器", 0.02)
    cpu_tier = cpu_performance_tier(cpu_item["component_subtype"]) if cpu_item else 1
    if cpu_tier >= 5:
        preferred = ["360水冷", "240水冷", "一体式水冷"]
    elif cpu_tier >= 4:
        preferred = ["双塔风冷", "240水冷"]
    else:
        preferred = ["单塔风冷", "下压风冷"]
    return _pick_best_by_budget(cooler_pool, target, prefer_subtypes=preferred, user_prefs=user_prefs) or _pick_best_by_budget(cooler_pool, target, user_prefs=user_prefs)


CPU_PERFORMANCE_NOTES: dict[str, str] = {
    "9800X3D": "8核16线程，3D V-Cache加持，游戏性能顶级",
    "7800X3D": "8核16线程，3D V-Cache，游戏性价比之王",
    "9700X": "8核16线程 Zen5，能效比优秀",
    "7700": "8核16线程 Zen4，中高端甜点",
    "9600X": "6核12线程 Zen5，入门到中端好选择",
    "7500F": "6核12线程 Zen4，AM5平台最具性价比入口",
    "5600": "6核12线程 Zen3，AM4平台经典之选",
    "5500": "6核12线程 Zen3，入门办公够用",
    "I5-12400F": "6核12线程，入门游戏性价比高",
    "I5-13400F": "6P+4E核，中端全能型",
    "I5-14400F": "6P+4E核，中端升级款",
    "I7-13700F": "8P+8E核，多任务和创作强力",
    "I9-13900K": "8P+16E核，旗舰全能",
}

GPU_PERFORMANCE_NOTES: dict[str, str] = {
    "RTX 5070 Ti": "可胜任 4K 中高画质 / 2K 高刷，光追性能顶级",
    "RTX 5070": "2K 高画质高刷 / 4K 入门，新一代中高端",
    "RTX 5060 Ti": "1080P 高刷 / 2K 中高画质，新一代甜品级",
    "RTX 5060": "1080P 高画质流畅，入门到中端甜点",
    "RTX 4070 Super": "2K 高画质高刷，光追表现出色",
    "RTX 4070": "2K 中高画质，上代中高端主力",
    "RTX 4060 Ti": "1080P 高刷 / 2K 入门，上代甜品级",
    "RTX 4060": "1080P 中高画质，上代入门甜点",
    "RX 9070": "2K 高画质，AMD 新一代中高端",
    "RX 9060 XT": "1080P 高刷 / 2K 入门，AMD 新一代甜品",
}

MEMORY_PERFORMANCE_NOTES: dict[str, str] = {
    "DDR5": "新一代内存标准，带宽更高，适合新平台",
    "DDR4": "成熟稳定，性价比高",
}

COMPONENT_PERFORMANCE_NOTES: dict[str, str] = {
    "散热器": {
        "360水冷": "顶级散热，适合旗舰CPU超频",
        "240水冷": "高效散热，中高端CPU均衡选择",
        "双塔风冷": "风冷旗舰，性价比散热方案",
        "单塔风冷": "够用党首选，入门到中端够用",
    },
}


def build_reason(component_type: str, item: dict[str, Any], budget: float, scenario_code: str, extra_notes: list[str]) -> str:
    target = budget * _scenario_template(scenario_code, budget)["ratios"].get(component_type, 0.0)
    subtype = item.get("component_subtype", "")

    # Build a richer description using the performance knowledge base
    intro_parts: list[str] = []

    if component_type == "CPU" and subtype:
        perf = CPU_PERFORMANCE_NOTES.get(subtype, "")
        if perf:
            intro_parts.append(f"{subtype} — {perf}")
        else:
            intro_parts.append(f"CPU {subtype}")
    elif component_type == "显卡" and subtype:
        perf = GPU_PERFORMANCE_NOTES.get(subtype, "")
        if perf:
            intro_parts.append(f"{subtype} — {perf}")
        else:
            intro_parts.append(f"显卡 {subtype}")
    elif component_type == "散热器" and subtype:
        notes = COMPONENT_PERFORMANCE_NOTES.get("散热器", {})
        perf = notes.get(subtype, "")
        if perf:
            intro_parts.append(f"{subtype} — {perf}")
        else:
            intro_parts.append(f"{component_type} {subtype}")
    elif component_type == "内存" and subtype:
        for gen_key, gen_desc in MEMORY_PERFORMANCE_NOTES.items():
            if gen_key in subtype:
                intro_parts.append(f"{subtype} — {gen_desc}")
                break
        else:
            intro_parts.append(f"{component_type} {subtype}")
    else:
        intro_parts.append(f"{component_type} {subtype}")

    if target > 0:
        intro_parts.append(f"预算分配 ¥{target:.0f}，实际 ¥{item['effective_price']:.0f}")

    parts = ["，".join(intro_parts)]

    if extra_notes:
        parts.append("；".join(extra_notes))

    return "。".join(parts)


def generate_build_plan(
    user_id: int,
    budget: float,
    usage_scenario: str,
    selected_parts: dict[str, str],
    user_prefs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pool = load_market_pool()
    rules_by_antecedent = load_rules_by_antecedent()
    template = _scenario_template(usage_scenario, budget)  # 动态比例
    prefs = user_prefs or learn_user_build_preferences(user_id)

    cpu_item = choose_cpu(pool, rules_by_antecedent, budget, usage_scenario, selected_parts, prefs)
    gpu_item = choose_gpu(pool, rules_by_antecedent, budget, usage_scenario, selected_parts, cpu_item, prefs)
    motherboard_item = choose_motherboard(pool, budget, usage_scenario, selected_parts, cpu_item, prefs)
    memory_item = choose_memory(pool, budget, usage_scenario, selected_parts, cpu_item, motherboard_item, prefs)
    storage_item = choose_storage(pool, budget, usage_scenario, selected_parts, prefs)
    power_item = choose_power(pool, budget, usage_scenario, selected_parts, gpu_item, prefs)
    case_item = choose_case(pool, budget, usage_scenario, selected_parts, motherboard_item, prefs)
    cooler_item = choose_cooler(pool, budget, usage_scenario, selected_parts, cpu_item, prefs)

    selected_items = {
        "CPU": cpu_item,
        "主板": motherboard_item,
        "内存": memory_item,
        "显卡": gpu_item,
        "固态硬盘": storage_item,
        "电源": power_item,
        "机箱": case_item,
        "散热器": cooler_item,
    }

    notes: list[str] = []
    total_price = 0.0
    results: list[dict[str, Any]] = []
    for component_type in COMPONENT_ORDER:
        item = selected_items.get(component_type)
        if not item:
            notes.append(f"{component_type} 当前数据源未提供可用候选，首版智能搭配暂未纳入。")
            continue
        total_price += item["effective_price"]
        extra_notes: list[str] = []
        if component_type == "主板" and cpu_item:
            extra_notes.append(f"与 {cpu_item['component_subtype']} 平台兼容")
        if component_type == "内存" and motherboard_item:
            extra_notes.append(f"按 {preferred_memory_gen_for_cpu(cpu_item['component_subtype'] if cpu_item else '', motherboard_item['component_subtype'])} 代际匹配")
        if component_type == "电源" and gpu_item:
            extra_notes.append(f"满足显卡建议功率 {required_power_for_gpu(gpu_item['component_subtype'])}W")
        score = 100.0 - abs(item["effective_price"] - budget * template["ratios"].get(component_type, 0.0)) / max(1.0, budget) * 100
        results.append(
            {
                "component_type": component_type,
                "component_subtype": item["component_subtype"],
                "shop": item["shop"],
                "sku_name": item["sku_name"],
                "effective_price": item["effective_price"],
                "source_url": item["source_url"],
                "score": round(score, 4),
                "reason_text": build_reason(component_type, item, budget, usage_scenario, extra_notes),
            }
        )

    if cpu_item and gpu_item:
        balance = analyze_cpu_gpu_balance(cpu_item["component_subtype"], gpu_item["component_subtype"])
        notes.append(f"{balance['label']}（比例 {balance['ratio']}）。")
        if balance.get("suggestion"):
            notes.append(balance["suggestion"])
    if total_price > budget:
        notes.append(f"当前搭配总价 {total_price:.2f} 元，高于预算 {budget:.2f} 元。")
    else:
        notes.append(f"当前搭配总价 {total_price:.2f} 元，处于预算 {budget:.2f} 元范围内。")

    selected_json = json.dumps(selected_parts, ensure_ascii=False)
    summary = " + ".join(item["component_subtype"] for item in results[:4])
    run_mysql_script(
        f"""
USE school_market;
INSERT INTO pc_build_task (user_id, budget, usage_scenario, selected_parts_json, total_price, build_summary, note)
VALUES (
  {user_id},
  {round(budget, 2)},
  '{sql_escape(usage_scenario)}',
  '{sql_escape(selected_json)}',
  {round(total_price, 2)},
  '{sql_escape(summary)}',
  '{sql_escape(" ".join(notes))}'
);
"""
    )
    task_id = int(
        run_mysql_query(
            f"""
            SELECT id
            FROM pc_build_task
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
INSERT INTO pc_build_result_item (task_id, component_type, component_subtype, shop, sku_name, effective_price, source_url, score, reason_text)
VALUES (
  {task_id},
  '{sql_escape(item["component_type"])}',
  '{sql_escape(item["component_subtype"])}',
  {('NULL' if not item["shop"] else "'" + sql_escape(item["shop"]) + "'")},
  '{sql_escape(item["sku_name"])}',
  {round(item["effective_price"], 2)},
  {('NULL' if not item["source_url"] else "'" + sql_escape(item["source_url"]) + "'")},
  {round(item["score"], 4)},
  '{sql_escape(item["reason_text"])}'
);""".strip()
        )
    run_mysql_script("\n".join(statements))

    physical_warnings = check_physical_compatibility(results)
    for w in physical_warnings:
        notes.insert(0, f"[{w['level']}] {w['title']}: {w['detail']}")

    return {
        "task_id": task_id,
        "usage_label": template["label"],
        "budget": budget,
        "total_price": round(total_price, 2),
        "notes": notes,
        "results": results,
        "physical_warnings": physical_warnings,
    }


def preview_build_plan(
    budget: float,
    usage_scenario: str,
    selected_parts: dict[str, str],
    user_prefs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pool = load_market_pool()
    rules_by_antecedent = load_rules_by_antecedent()
    template = _scenario_template(usage_scenario, budget)  # 动态比例
    prefs = user_prefs or {}

    cpu_item = choose_cpu(pool, rules_by_antecedent, budget, usage_scenario, selected_parts, prefs)
    gpu_item = choose_gpu(pool, rules_by_antecedent, budget, usage_scenario, selected_parts, cpu_item, prefs)
    motherboard_item = choose_motherboard(pool, budget, usage_scenario, selected_parts, cpu_item, prefs)
    memory_item = choose_memory(pool, budget, usage_scenario, selected_parts, cpu_item, motherboard_item, prefs)
    storage_item = choose_storage(pool, budget, usage_scenario, selected_parts, prefs)
    power_item = choose_power(pool, budget, usage_scenario, selected_parts, gpu_item, prefs)
    case_item = choose_case(pool, budget, usage_scenario, selected_parts, motherboard_item, prefs)
    cooler_item = choose_cooler(pool, budget, usage_scenario, selected_parts, cpu_item, prefs)

    selected_items = {
        "CPU": cpu_item,
        "主板": motherboard_item,
        "内存": memory_item,
        "显卡": gpu_item,
        "固态硬盘": storage_item,
        "电源": power_item,
        "机箱": case_item,
        "散热器": cooler_item,
    }

    notes: list[str] = []
    total_price = 0.0
    results: list[dict[str, Any]] = []
    for component_type in COMPONENT_ORDER:
        item = selected_items.get(component_type)
        if not item:
            notes.append(f"{component_type} 当前数据源未提供可用候选，预览方案暂未纳入。")
            continue
        total_price += item["effective_price"]
        extra_notes: list[str] = []
        if component_type == "主板" and cpu_item:
            extra_notes.append(f"与 {cpu_item['component_subtype']} 平台兼容")
        if component_type == "内存" and motherboard_item:
            extra_notes.append(
                f"按 {preferred_memory_gen_for_cpu(cpu_item['component_subtype'] if cpu_item else '', motherboard_item['component_subtype'])} 代际匹配"
            )
        if component_type == "电源" and gpu_item:
            extra_notes.append(f"满足显卡建议功率 {required_power_for_gpu(gpu_item['component_subtype'])}W")
        score = 100.0 - abs(item["effective_price"] - budget * template["ratios"].get(component_type, 0.0)) / max(1.0, budget) * 100
        results.append(
            {
                "component_type": component_type,
                "component_subtype": item["component_subtype"],
                "shop": item["shop"],
                "sku_name": item["sku_name"],
                "effective_price": item["effective_price"],
                "source_url": item["source_url"],
                "score": round(score, 4),
                "reason_text": build_reason(component_type, item, budget, usage_scenario, extra_notes),
            }
        )

    if cpu_item and gpu_item:
        balance = analyze_cpu_gpu_balance(cpu_item["component_subtype"], gpu_item["component_subtype"])
        notes.append(f"{balance['label']}（比例 {balance['ratio']}）。")
        if balance.get("suggestion"):
            notes.append(balance["suggestion"])
    if total_price > budget:
        notes.append(f"当前搭配总价 {total_price:.2f} 元，高于预设求解预算 {budget:.2f} 元。")
    else:
        notes.append(f"当前搭配总价 {total_price:.2f} 元，处于预设求解预算 {budget:.2f} 元范围内。")

    physical_warnings = check_physical_compatibility(results)
    for w in physical_warnings:
        notes.insert(0, f"[{w['level']}] {w['title']}: {w['detail']}")

    return {
        "task_id": None,
        "usage_label": template["label"],
        "budget": budget,
        "total_price": round(total_price, 2),
        "notes": notes,
        "results": results,
        "physical_warnings": physical_warnings,
    }


def fetch_build_history(user_id: int) -> list[dict[str, Any]]:
    ensure_knowledge_ready()
    task_rows = run_mysql_query(
        f"""
        SELECT
          id,
          budget,
          usage_scenario,
          COALESCE(selected_parts_json, '{{}}'),
          COALESCE(total_price, 0),
          COALESCE(build_summary, ''),
          COALESCE(note, ''),
          DATE_FORMAT(created_at, '%Y-%m-%d %H:%i:%s')
        FROM pc_build_task
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
        SELECT task_id, component_type, component_subtype, COALESCE(shop, ''), sku_name, effective_price, COALESCE(source_url, ''), score, reason_text
        FROM pc_build_result_item
        WHERE task_id IN ({task_ids})
        ORDER BY task_id DESC, FIELD(component_type, 'CPU', '主板', '内存', '显卡', '固态硬盘', '电源', '机箱', '散热器');
        """
    )
    result_map: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in result_rows:
        result_map[int(row[0])].append(
            {
                "component_type": row[1],
                "component_subtype": row[2],
                "shop": row[3],
                "sku_name": row[4],
                "effective_price": float(row[5]),
                "source_url": row[6],
                "score": float(row[7]),
                "reason_text": row[8],
            }
        )
    history: list[dict[str, Any]] = []
    for row in task_rows:
        history.append(
            {
                "id": int(row[0]),
                "budget": float(row[1]),
                "usage_scenario": row[2],
                "usage_label": _scenario_template(row[2])["label"],
                "selected_parts_json": row[3],
                "total_price": float(row[4]),
                "build_summary": row[5],
                "note": row[6],
                "created_at": row[7],
                "results": result_map.get(int(row[0]), []),
            }
        )
    return history


def refresh_and_report() -> str:
    stats = refresh_build_knowledge()
    return json.dumps(stats, ensure_ascii=False)


def main() -> int:
    stats = refresh_build_knowledge()
    print(json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
