from __future__ import annotations

import csv
import io
import re
from functools import lru_cache
from pathlib import Path
from typing import Any


CATALOG_HEADER = "一级分类,二级分类,三级分类,四级分类/型号,备注"
CATALOG_PATHS = [
    Path(__file__).resolve().parents[2] / "data" / "电脑配件商品分类表.txt",
]

GPU_PATTERNS = [
    re.compile(r"\bRTX\s*(\d{4})\s*(TI|SUPER)?\b", re.I),
    re.compile(r"\bGTX\s*(\d{3,4})\s*(TI|SUPER)?\b", re.I),
    re.compile(r"\bRX\s*(\d{4,5})\s*(XT|GRE)?\b", re.I),
    re.compile(r"\bARC\s*([AB]\d{3})\b", re.I),
    re.compile(r"\bGT\s*(\d{3,4})\b", re.I),
]
CPU_PATTERNS = [
    re.compile(r"\bI([3579])[\s\-]?(\d{4,5}[A-Z]{0,3})\b", re.I),
    re.compile(r"\b(ULTRA\s*\d{3}[A-Z]?)\b", re.I),
    re.compile(r"\b(\d{4,5}X3D|\d{4,5}X|\d{4,5}G|\d{4,5}GT|\d{4,5}F)\b", re.I),
    re.compile(r"\bG(\d{4})\b", re.I),
]
MOTHERBOARD_PATTERN = re.compile(
    r"\b(X99|X79|X570|X670E|X670|X870E|X870|B860|B850M|B850|B760M|B760|B650M|B650|B550M|B550|H710|H610M|H610|H510M|H510|Z790|Z690|Z890|A620|A520M|A520|A320)\b",
    re.I,
)
MEMORY_GEN_PATTERN = re.compile(r"DDR\s*([345])L?", re.I)
MEMORY_KIT_PATTERN = re.compile(r"(4|8|12|16|24|32|48|64)\s*(?:G|GB)\s*[*X×]\s*(2|4)", re.I)
MEMORY_CAP_PATTERN = re.compile(r"(2|4|8|12|16|24|32|48|64|96|128)\s*(?:G|GB)", re.I)
STORAGE_PATTERN = re.compile(r"\b(120|128|240|250|256|480|500|512|960)\s*(G|GB)\b|\b(1|2|4|6|8|10|12|16)\s*T(B)?\b", re.I)
PSU_WATT_PATTERN = re.compile(r"\b(300|350|400|450|500|550|600|650|700|750|800|850|1000|1200|1300|1600)\s*W\b", re.I)
FAN_SIZE_PATTERN = re.compile(r"\b(80|90|92|120|140|200)\s*MM\b|\b(8|9|12|14|20)\s*CM\b", re.I)
WATER_COOLER_PATTERN = re.compile(r"\b(120|240|280|360|420)\b")
HEATPIPE_PATTERN = re.compile(r"([45678])\s*热管")


def find_catalog_file() -> Path:
    for path in CATALOG_PATHS:
        if path.exists():
            return path
    raise FileNotFoundError("未找到电脑配件商品分类表.txt")


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").upper()).strip()


def read_catalog_text() -> str:
    path = find_catalog_file()
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("catalog", raw, 0, 1, "无法解码分类表")


@lru_cache(maxsize=1)
def load_catalog_rows() -> list[dict[str, str]]:
    text = read_catalog_text()
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines) if line.strip() == CATALOG_HEADER), -1)
    if start < 0:
        raise ValueError("分类表中未找到 CSV 头")

    reader = csv.reader(io.StringIO("\n".join(lines[start:])))
    rows = []
    next(reader, None)
    for row in reader:
        if len(row) < 5:
            continue
        rows.append(
            {
                "level1": row[0].strip(),
                "level2": row[1].strip(),
                "level3": row[2].strip(),
                "level4": row[3].strip(),
                "remark": row[4].strip(),
            }
        )
    return rows


def extract_memory_capacity(text: str) -> str:
    kit_match = MEMORY_KIT_PATTERN.search(text)
    if kit_match:
        return f"{int(kit_match.group(1)) * int(kit_match.group(2))}GB"

    values = [int(value) for value in MEMORY_CAP_PATTERN.findall(text)]
    if not values:
        return ""
    return f"{max(values)}GB" if len(set(values)) >= 3 else f"{sorted(set(values))[0]}GB"


def extract_storage_capacity(text: str) -> str:
    for g_value, _, t_value, _ in STORAGE_PATTERN.findall(text):
        if t_value:
            return f"{t_value}TB"
        if g_value:
            return f"{g_value}GB"
    return ""


def detect_gpu_model(text: str) -> str:
    for pattern in GPU_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        parts = [part for part in match.groups() if part]
        prefix = "RTX"
        if "GTX" in pattern.pattern.upper():
            prefix = "GTX"
        elif "RX" in pattern.pattern.upper():
            prefix = "RX"
        elif "ARC" in pattern.pattern.upper():
            prefix = "Arc"
        elif "GT " in pattern.pattern.upper():
            prefix = "GT"
        label = f"{prefix} {parts[0].upper()}"
        if len(parts) > 1:
            label = f"{label} {parts[1].upper()}"
        return label.replace("TI", "Ti").replace("SUPER", "Super")
    return "未识别型号"


def detect_cpu_model(text: str) -> str:
    intel_match = CPU_PATTERNS[0].search(text)
    if intel_match:
        return f"i{intel_match.group(1)}-{intel_match.group(2).upper()}"
    ultra_match = CPU_PATTERNS[1].search(text)
    if ultra_match:
        return re.sub(r"\s+", " ", ultra_match.group(1).upper()).strip()
    for pattern in CPU_PATTERNS[2:]:
        match = pattern.search(text)
        if not match:
            continue
        token = match.group(0).upper()
        if token.startswith("G") and token[1:].isdigit():
            return f"奔腾 {token}"
        return token
    return "未识别型号"


def detect_motherboard_model(text: str) -> str:
    match = MOTHERBOARD_PATTERN.search(text)
    return match.group(1).upper() if match else "未识别芯片组"


def detect_memory_model(text: str) -> str:
    generation_match = MEMORY_GEN_PATTERN.search(text)
    generation = f"DDR{generation_match.group(1)}" if generation_match else "DDR?"
    capacity = extract_memory_capacity(text)
    return f"{generation} {capacity}".strip() if capacity else generation


def detect_storage_model(text: str) -> str:
    capacity = extract_storage_capacity(text)
    if "机械硬盘" in text or "HDD" in text:
        storage_type = "HDD"
    elif "PCIE 5" in text or "PCIE5" in text:
        storage_type = "PCIe 5.0 SSD"
    elif "NVME" in text or "M.2" in text:
        storage_type = "M.2 NVMe SSD"
    elif "U盘" in text:
        storage_type = "U盘"
    elif "存储卡" in text or "TF卡" in text:
        storage_type = "存储卡"
    else:
        storage_type = "SATA SSD"
    return f"{capacity} {storage_type}".strip() if capacity else storage_type


def detect_psu_model(text: str) -> str:
    watt_match = PSU_WATT_PATTERN.search(text)
    return f"{watt_match.group(1)}W" if watt_match else "未识别功率"


def detect_cooler_model(text: str) -> str:
    if "水冷" in text:
        size_match = WATER_COOLER_PATTERN.search(text)
        return f"{size_match.group(1)}水冷" if size_match else "一体式水冷"
    if "下压" in text:
        return "下压式风冷"
    if "双塔" in text:
        return "双塔风冷"
    if "单塔" in text:
        return "单塔风冷"
    heatpipe_match = HEATPIPE_PATTERN.search(text)
    if heatpipe_match:
        return "双塔风冷" if int(heatpipe_match.group(1)) >= 6 else "单塔风冷"
    return "单塔风冷"


def detect_fan_model(text: str) -> str:
    match = FAN_SIZE_PATTERN.search(text)
    if not match:
        return "120mm"
    size = match.group(1) or match.group(2)
    if len(size) <= 2:
        size = f"{size}0"
    return f"{size}mm"


def build_cpu_catalog(text: str, product_subtype: str) -> dict[str, str]:
    level2 = "服务器/工作站CPU" if any(token in text for token in ["XEON", "EPYC", "THREADRIPPER PRO"]) else "台式机CPU"
    if "NOTEBOOK" in text or "笔记本" in text:
        level2 = "笔记本CPU"
    if "XEON" in text:
        level3, level4 = "Intel", "Xeon"
    elif "EPYC" in text:
        level3, level4 = "AMD", "EPYC"
    elif "THREADRIPPER PRO" in text:
        level3, level4 = "AMD", "Threadripper Pro"
    elif "THREADRIPPER" in text:
        level3, level4 = "AMD", "Threadripper"
    elif product_subtype.startswith("i3-"):
        level3, level4 = "Intel", "i3"
    elif product_subtype.startswith("i5-"):
        level3, level4 = "Intel", "i5"
    elif product_subtype.startswith("i7-"):
        level3, level4 = "Intel", "i7"
    elif product_subtype.startswith("i9-") or product_subtype.startswith("ULTRA"):
        level3, level4 = "Intel", "i9"
    elif "奔腾" in product_subtype:
        level3, level4 = "Intel", "奔腾"
    elif "赛扬" in text:
        level3, level4 = "Intel", "赛扬"
    elif has_any_text(text, ["R3", "RYZEN 3"]):
        level3, level4 = "AMD", "Ryzen 3"
    elif has_any_text(text, ["R5", "RYZEN 5"]):
        level3, level4 = "AMD", "Ryzen 5"
    elif has_any_text(text, ["R7", "RYZEN 7"]):
        level3, level4 = "AMD", "Ryzen 7"
    elif has_any_text(text, ["R9", "RYZEN 9"]):
        level3, level4 = "AMD", "Ryzen 9"
    elif "ATHLON" in text:
        level3, level4 = "AMD", "Athlon"
    else:
        level3, level4 = ("Intel", "i5") if "INTEL" in text else ("AMD", "Ryzen 5")
    return {
        "category_level1": "CPU/处理器",
        "category_level2": level2,
        "category_level3": level3,
        "category_level4": level4,
    }


def build_motherboard_catalog(text: str, product_subtype: str) -> dict[str, str]:
    upper = product_subtype.upper()
    if upper.startswith(("H610", "H710", "H510", "B660", "B760", "B860", "Z690", "Z790", "Z890", "X99", "X79")):
        platform = "Intel主板"
    else:
        platform = "AMD主板"
    if upper.startswith(("H610", "H710", "H510")):
        series = "H系列"
    elif upper.startswith(("B760", "B660", "B860")):
        series = "B系列"
    elif upper.startswith(("Z690", "Z790", "Z890", "X99", "X79")):
        series = "Z系列"
    elif upper.startswith(("A320", "A520", "A620")):
        series = "A系列"
    elif upper.startswith(("B550", "B650", "B850")):
        series = "B系列"
    else:
        series = "X系列"
    return {
        "category_level1": "主板",
        "category_level2": "按平台",
        "category_level3": platform,
        "category_level4": series,
    }


def build_gpu_catalog(product_subtype: str) -> dict[str, str]:
    token = product_subtype.upper()
    if token.startswith("RTX"):
        vendor = "NVIDIA显卡"
        number = int(re.search(r"(\d{4})", token).group(1))
        series = f"RTX {str(number)[0:2]}系列"
        level4 = product_subtype
    elif token.startswith("GTX"):
        vendor = "NVIDIA显卡"
        series = "GTX系列"
        level4 = product_subtype
    elif token.startswith("RX"):
        vendor = "AMD显卡"
        number = int(re.search(r"(\d{4,5})", token).group(1))
        series = f"RX {str(number)[0]}000系列" if number >= 10000 else f"RX {str(number)[0]}000系列"
        if number >= 9000:
            series = "RX 9000系列"
        elif number >= 7000:
            series = "RX 7000系列"
        else:
            series = "RX 6000系列"
        level4 = product_subtype
    elif token.startswith("ARC"):
        vendor = "Intel显卡"
        series = "Arc系列"
        level4 = product_subtype
    else:
        vendor = "NVIDIA显卡"
        series = "RTX 50系列"
        level4 = "RTX 5060"
    return {
        "category_level1": "显卡",
        "category_level2": vendor,
        "category_level3": series,
        "category_level4": level4,
    }


def build_memory_catalog(text: str, product_subtype: str) -> dict[str, str]:
    if any(token in text for token in ["ECC", "RDIMM", "LRDIMM", "UDIMM"]):
        level2 = "服务器/工作站内存"
        level3 = "ECC内存"
        if "LRDIMM" in text:
            level4 = "LRDIMM"
        elif "RDIMM" in text:
            level4 = "RDIMM"
        else:
            level4 = "UDIMM"
    elif any(token in text for token in ["SO-DIMM", "SODIMM", "笔记本"]):
        generation = "DDR5" if "DDR5" in product_subtype else "DDR4"
        level2 = "笔记本内存"
        level3 = generation
        level4 = "SO-DIMM"
    else:
        parts = product_subtype.split(" ", 1)
        level2 = "台式机内存"
        level3 = parts[0]
        capacity = parts[1] if len(parts) > 1 else "16GB"
        level4 = capacity.replace("套装", "")
    return {
        "category_level1": "内存条",
        "category_level2": level2,
        "category_level3": level3,
        "category_level4": level4,
    }


def build_storage_catalog(text: str, product_subtype: str) -> dict[str, str]:
    capacity = extract_storage_capacity(text) or product_subtype.split(" ", 1)[0]
    if "U盘" in text:
        return {"category_level1": "硬盘", "category_level2": "外置存储", "category_level3": "U盘", "category_level4": capacity or "64GB"}
    if "存储卡" in text or "TF卡" in text:
        return {"category_level1": "硬盘", "category_level2": "外置存储", "category_level3": "存储卡", "category_level4": "TF卡"}
    if "移动固态" in text:
        return {"category_level1": "硬盘", "category_level2": "外置存储", "category_level3": "移动固态硬盘", "category_level4": capacity or "1TB"}
    if "移动机械" in text:
        return {"category_level1": "硬盘", "category_level2": "外置存储", "category_level3": "移动机械硬盘", "category_level4": capacity or "1TB"}
    if "机械硬盘" in text or "HDD" in text:
        form_factor = "2.5英寸" if "2.5" in text or "笔记本" in text else "3.5英寸"
        return {"category_level1": "硬盘", "category_level2": "机械硬盘HDD", "category_level3": form_factor, "category_level4": capacity or "1TB"}
    if "PCIE 5" in text or "PCIE5" in text:
        level3 = "PCIe 5.0 SSD"
    elif "NVME" in text or "M.2" in text:
        level3 = "M.2 NVMe SSD"
    else:
        level3 = "SATA SSD"
    return {"category_level1": "硬盘", "category_level2": "固态硬盘SSD", "category_level3": level3, "category_level4": capacity or "512GB"}


def build_psu_catalog(text: str, product_subtype: str) -> dict[str, str]:
    spec = "ATX"
    if "SFX-L" in text:
        spec = "SFX-L"
    elif "SFX" in text:
        spec = "SFX"
    elif "TFX" in text:
        spec = "TFX"
    elif "FLEX" in text:
        spec = "Flex"

    if spec != "ATX":
        return {"category_level1": "电源", "category_level2": "按规格", "category_level3": "电源", "category_level4": spec}
    return {"category_level1": "电源", "category_level2": "按功率", "category_level3": "ATX电源", "category_level4": product_subtype}


def build_cooler_catalog(text: str, product_subtype: str) -> dict[str, str]:
    if "水冷" in product_subtype:
        return {"category_level1": "散热器", "category_level2": "CPU水冷散热器", "category_level3": "一体式水冷", "category_level4": product_subtype}
    if product_subtype == "下压式风冷":
        level4 = "下压式风冷"
    elif "双塔" in product_subtype:
        level4 = "双塔风冷"
    else:
        level4 = "单塔风冷"
    return {"category_level1": "散热器", "category_level2": "CPU风冷散热器", "category_level3": "风冷", "category_level4": level4}


def build_fan_catalog(product_subtype: str) -> dict[str, str]:
    size = re.search(r"(\d{2,3})", product_subtype)
    return {
        "category_level1": "风扇",
        "category_level2": "机箱风扇",
        "category_level3": "尺寸",
        "category_level4": f"{size.group(1)}mm" if size else "120mm",
    }


def has_any_text(text: str, tokens: list[str]) -> bool:
    return any(token in text for token in tokens)


def classify_product(keyword: str, sku_name: str) -> dict[str, str]:
    text = f" {normalize_text(keyword)} {normalize_text(sku_name)} "

    if ("风扇" in text or "FAN" in text or "ARGB" in text or "RGB" in text) and FAN_SIZE_PATTERN.search(text):
        canonical_keyword = "机箱散热风扇"
        product_subtype = detect_fan_model(text)
        category = build_fan_catalog(product_subtype)
    elif any(token in text for token in ["散热器", "水冷", "风冷", "热管", "PA120", "AG620", "AK400", "玄冰"]) and "主板" not in text:
        canonical_keyword = "CPU散热器"
        product_subtype = detect_cooler_model(text)
        category = build_cooler_catalog(text, product_subtype)
    elif "显卡" in text or any(pattern.search(text) for pattern in GPU_PATTERNS):
        canonical_keyword = "显卡"
        product_subtype = detect_gpu_model(text)
        category = build_gpu_catalog(product_subtype)
    elif "主板" in text or MOTHERBOARD_PATTERN.search(text):
        canonical_keyword = "电脑主板"
        product_subtype = detect_motherboard_model(text)
        category = build_motherboard_catalog(text, product_subtype)
    elif "内存" in text or any(token in text for token in ["DDR3", "DDR4", "DDR5", "ECC", "SO-DIMM", "UDIMM"]):
        canonical_keyword = "内存条"
        product_subtype = detect_memory_model(text)
        category = build_memory_catalog(text, product_subtype)
    elif any(token in text for token in ["SSD", "固态", "机械硬盘", "HDD", "NVME", "M.2", "硬盘", "U盘", "TF卡", "存储卡"]):
        canonical_keyword = "硬盘"
        product_subtype = detect_storage_model(text)
        category = build_storage_catalog(text, product_subtype)
    elif ("电源" in text or any(token in text for token in ["ATX", "SFX", "TFX", "FLEX", "额定"])) and PSU_WATT_PATTERN.search(text):
        canonical_keyword = "台式机电源"
        product_subtype = detect_psu_model(text)
        category = build_psu_catalog(text, product_subtype)
    else:
        canonical_keyword = "CPU"
        product_subtype = detect_cpu_model(text)
        category = build_cpu_catalog(text, product_subtype)

    subtype_group = canonical_keyword
    if canonical_keyword == "显卡":
        subtype_group = product_subtype.split(" ")[0]
    elif canonical_keyword == "内存条":
        subtype_group = product_subtype.split(" ")[0]
    elif canonical_keyword == "CPU散热器":
        subtype_group = category["category_level3"]
    elif canonical_keyword == "机箱散热风扇":
        subtype_group = category["category_level4"]
    elif canonical_keyword == "硬盘":
        subtype_group = category["category_level3"]

    category_note = find_catalog_remark(
        category["category_level1"],
        category["category_level2"],
        category["category_level3"],
        category["category_level4"],
    )
    return {
        "canonical_keyword": canonical_keyword,
        "subtype_group": subtype_group,
        "product_subtype": product_subtype,
        "category_level1": category["category_level1"],
        "category_level2": category["category_level2"],
        "category_level3": category["category_level3"],
        "category_level4": category["category_level4"],
        "category_remark": category_note,
    }


def find_catalog_remark(level1: str, level2: str, level3: str, level4: str) -> str:
    for row in load_catalog_rows():
        if row["level1"] == level1 and row["level2"] == level2 and row["level3"] == level3 and row["level4"] == level4:
            return row["remark"]
    return ""
