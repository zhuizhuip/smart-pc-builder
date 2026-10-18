from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


CATALOG_HEADER = "一级分类,二级分类,三级分类,四级分类/型号,备注"
CATALOG_FILE = Path(__file__).resolve().parents[2] / "data" / "电脑配件商品分类表.txt"

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
    r"\b(X99|X79|X570|X670E|X670|X870E|X870|B860|B850M|B850|B760M|B760|B650M|B650|B550M|B550|H710|H610M|H610|H510M|H510|Z890|Z790|Z690|A620|A520M|A520|A320)\b",
    re.I,
)
MEMORY_GEN_PATTERN = re.compile(r"DDR\s*([345])L?", re.I)
MEMORY_CAP_PATTERN = re.compile(r"(2|4|8|12|16|24|32|48|64|96|128)\s*(?:G|GB)", re.I)
MEMORY_KIT_PATTERN = re.compile(r"(4|8|12|16|24|32|48|64)\s*(?:G|GB)\s*[*X×]\s*(2|4)", re.I)
STORAGE_PATTERN = re.compile(
    r"\b(64|120|128|240|250|256|480|500|512|960)\s*(G|GB)\b|\b(1|2|4|6|8|10|12|16)\s*T(B)?\b",
    re.I,
)
PSU_WATT_PATTERN = re.compile(r"\b(300|350|400|450|500|550|600|650|700|750|800|850|1000|1200|1300|1600)\s*W\b", re.I)
FAN_SIZE_PATTERN = re.compile(r"\b(80|90|92|120|140|200)\s*MM\b|\b(8|9|12|14|20)\s*CM\b", re.I)
WATER_COOLER_PATTERN = re.compile(r"\b(120|240|280|360|420)\b")
HEATPIPE_PATTERN = re.compile(r"([45678])\s*热管")
SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class CatalogRow:
    level1: str
    level2: str
    level3: str
    level4: str
    remark: str


def normalize_text(value: str) -> str:
    return SPACE_RE.sub(" ", str(value or "").replace("\u3000", " ")).strip().upper()


def contains_any(text: str, tokens: list[str]) -> bool:
    return any(token in text for token in tokens)


def read_catalog_text() -> str:
    raw = CATALOG_FILE.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise RuntimeError(f"无法解码分类表: {CATALOG_FILE}")


@lru_cache(maxsize=1)
def load_catalog_rows() -> tuple[CatalogRow, ...]:
    content = read_catalog_text()
    lines = content.splitlines()
    start = next((idx for idx, line in enumerate(lines) if line.strip() == CATALOG_HEADER), -1)
    if start < 0:
        raise RuntimeError("分类表中未找到 CSV 表头")

    reader = csv.reader(io.StringIO("\n".join(lines[start:])))
    next(reader, None)

    rows: list[CatalogRow] = []
    for row in reader:
        if len(row) < 5:
            continue
        rows.append(
            CatalogRow(
                level1=row[0].strip(),
                level2=row[1].strip(),
                level3=row[2].strip(),
                level4=row[3].strip(),
                remark=row[4].strip(),
            )
        )
    return tuple(rows)


def find_catalog_remark(level1: str, level2: str, level3: str, level4: str) -> str:
    for row in load_catalog_rows():
        if (row.level1, row.level2, row.level3, row.level4) == (level1, level2, level3, level4):
            return row.remark
    return ""


def extract_memory_capacity(text: str) -> str:
    kit_match = MEMORY_KIT_PATTERN.search(text)
    if kit_match:
        single = int(kit_match.group(1))
        count = int(kit_match.group(2))
        return f"{single * count}GB"

    capacities = sorted({int(value) for value in MEMORY_CAP_PATTERN.findall(text)})
    if not capacities:
        return ""
    if len(capacities) >= 3:
        return f"{capacities[-1]}GB"
    return f"{capacities[0]}GB"


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

        label = f"{prefix} {parts[0]}"
        if len(parts) > 1:
            suffix = parts[1].upper().replace("TI", "Ti").replace("SUPER", "Super")
            label = f"{label} {suffix}"
        return label
    return "未识别型号"


def detect_cpu_model(text: str) -> str:
    intel_match = CPU_PATTERNS[0].search(text)
    if intel_match:
        return f"i{intel_match.group(1)}-{intel_match.group(2).upper()}"

    ultra_match = CPU_PATTERNS[1].search(text)
    if ultra_match:
        return SPACE_RE.sub(" ", ultra_match.group(1).upper()).strip()

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
    if not capacity:
        return generation
    return f"{generation} {capacity}"


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
    elif "TF卡" in text or "存储卡" in text:
        storage_type = "存储卡"
    elif "移动机械" in text:
        storage_type = "移动机械硬盘"
    elif "移动固态" in text:
        storage_type = "移动固态硬盘"
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
    size_match = FAN_SIZE_PATTERN.search(text)
    if not size_match:
        return "120mm"
    size = size_match.group(1) or size_match.group(2)
    if len(size) <= 2:
        size = f"{size}0"
    return f"{size}mm"


def build_cpu_category(text: str, product_subtype: str) -> dict[str, str]:
    level2 = "台式机CPU"
    if "笔记本" in text or "NOTEBOOK" in text:
        level2 = "笔记本CPU"
    elif contains_any(text, ["XEON", "EPYC", "THREADRIPPER PRO"]):
        level2 = "服务器/工作站CPU"

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
    elif product_subtype.startswith("奔腾"):
        level3, level4 = "Intel", "奔腾"
    elif "赛扬" in text:
        level3, level4 = "Intel", "赛扬"
    elif contains_any(text, ["R3", "RYZEN 3"]):
        level3, level4 = "AMD", "Ryzen 3"
    elif contains_any(text, ["R5", "RYZEN 5"]):
        level3, level4 = "AMD", "Ryzen 5"
    elif contains_any(text, ["R7", "RYZEN 7"]):
        level3, level4 = "AMD", "Ryzen 7"
    elif contains_any(text, ["R9", "RYZEN 9"]):
        level3, level4 = "AMD", "Ryzen 9"
    elif "ATHLON" in text:
        level3, level4 = "AMD", "Athlon"
    else:
        level3, level4 = ("Intel", "i5") if "INTEL" in text else ("AMD", "Ryzen 5")

    return {
        "level1": "CPU/处理器",
        "level2": level2,
        "level3": level3,
        "level4": level4,
    }


def build_motherboard_category(product_subtype: str) -> dict[str, str]:
    chipset = product_subtype.upper()
    if chipset.startswith(("H610", "H710", "H510", "B660", "B760", "B860", "Z690", "Z790", "Z890", "X99", "X79")):
        platform = "Intel主板"
    else:
        platform = "AMD主板"

    if chipset.startswith(("H610", "H710", "H510")):
        level4 = "H系列"
    elif chipset.startswith(("B660", "B760", "B860", "B550", "B650", "B850")):
        level4 = "B系列"
    elif chipset.startswith(("Z690", "Z790", "Z890")):
        level4 = "Z系列"
    elif chipset.startswith(("A320", "A520", "A620")):
        level4 = "A系列"
    else:
        level4 = "X系列"

    return {
        "level1": "主板",
        "level2": "按平台",
        "level3": platform,
        "level4": level4,
    }


def build_gpu_category(product_subtype: str) -> dict[str, str]:
    token = product_subtype.upper()
    if token.startswith("RTX") or token.startswith("GTX") or token.startswith("GT "):
        vendor = "NVIDIA显卡"
        if "RTX 30" in token or "RTX 3070" in token or "RTX 3050" in token or "RTX 3060" in token:
            level3 = "RTX 30系列"
        elif "RTX 40" in token or "RTX 4060" in token or "RTX 4070" in token or "RTX 4080" in token or "RTX 4090" in token:
            level3 = "RTX 40系列"
        elif "RTX 50" in token or "RTX 5050" in token or "RTX 5060" in token or "RTX 5070" in token or "RTX 5080" in token or "RTX 5090" in token:
            level3 = "RTX 50系列"
        else:
            level3 = "RTX 50系列"
    elif token.startswith("RX"):
        vendor = "AMD显卡"
        match = re.search(r"(\d{4,5})", token)
        number = int(match.group(1)) if match else 9000
        if number >= 9000:
            level3 = "RX 9000系列"
        elif number >= 7000:
            level3 = "RX 7000系列"
        else:
            level3 = "RX 6000系列"
    else:
        vendor = "Intel显卡"
        level3 = "Arc系列"

    return {
        "level1": "显卡",
        "level2": vendor,
        "level3": level3,
        "level4": product_subtype,
    }


def build_memory_category(text: str, product_subtype: str) -> dict[str, str]:
    if contains_any(text, ["ECC", "RDIMM", "LRDIMM", "UDIMM"]):
        level2 = "服务器/工作站内存"
        level3 = "ECC内存"
        if "LRDIMM" in text:
            level4 = "LRDIMM"
        elif "RDIMM" in text:
            level4 = "RDIMM"
        else:
            level4 = "UDIMM"
    elif contains_any(text, ["SO-DIMM", "SODIMM", "笔记本"]):
        level2 = "笔记本内存"
        level3 = "DDR5" if "DDR5" in product_subtype else "DDR4"
        level4 = "SO-DIMM"
    else:
        level2 = "台式机内存"
        parts = product_subtype.split(" ", 1)
        level3 = parts[0]
        level4 = parts[1].replace("套装", "") if len(parts) > 1 else "16GB"

    return {
        "level1": "内存条",
        "level2": level2,
        "level3": level3,
        "level4": level4,
    }


def build_storage_category(text: str, product_subtype: str) -> dict[str, str]:
    capacity = extract_storage_capacity(text) or product_subtype.split(" ", 1)[0]
    if "U盘" in text:
        return {"level1": "硬盘", "level2": "外置存储", "level3": "U盘", "level4": capacity or "64GB"}
    if "TF卡" in text or "存储卡" in text:
        return {"level1": "硬盘", "level2": "外置存储", "level3": "存储卡", "level4": "TF卡"}
    if "移动固态" in text:
        return {"level1": "硬盘", "level2": "外置存储", "level3": "移动固态硬盘", "level4": capacity or "1TB"}
    if "移动机械" in text:
        return {"level1": "硬盘", "level2": "外置存储", "level3": "移动机械硬盘", "level4": capacity or "1TB"}
    if "机械硬盘" in text or "HDD" in text:
        form_factor = "2.5英寸" if "2.5" in text or "笔记本" in text else "3.5英寸"
        return {"level1": "硬盘", "level2": "机械硬盘HDD", "level3": form_factor, "level4": capacity or "1TB"}
    if "PCIE 5" in text or "PCIE5" in text:
        level3 = "PCIe 5.0 SSD"
    elif "NVME" in text or "M.2" in text:
        level3 = "M.2 NVMe SSD"
    else:
        level3 = "SATA SSD"
    return {"level1": "硬盘", "level2": "固态硬盘SSD", "level3": level3, "level4": capacity or "512GB"}


def build_psu_category(text: str, product_subtype: str) -> dict[str, str]:
    if "SFX-L" in text:
        return {"level1": "电源", "level2": "按规格", "level3": "电源", "level4": "SFX-L"}
    if "SFX" in text:
        return {"level1": "电源", "level2": "按规格", "level3": "电源", "level4": "SFX"}
    if "TFX" in text:
        return {"level1": "电源", "level2": "按规格", "level3": "电源", "level4": "TFX"}
    if "FLEX" in text:
        return {"level1": "电源", "level2": "按规格", "level3": "电源", "level4": "Flex"}
    return {"level1": "电源", "level2": "按功率", "level3": "ATX电源", "level4": product_subtype}


def build_cooler_category(product_subtype: str) -> dict[str, str]:
    if "水冷" in product_subtype:
        return {"level1": "散热器", "level2": "CPU水冷散热器", "level3": "一体式水冷", "level4": product_subtype}
    level4 = "下压式风冷" if product_subtype == "下压式风冷" else product_subtype
    return {"level1": "散热器", "level2": "CPU风冷散热器", "level3": "风冷", "level4": level4}


def build_fan_category(product_subtype: str) -> dict[str, str]:
    return {"level1": "风扇", "level2": "机箱风扇", "level3": "尺寸", "level4": product_subtype}


def classify_product(keyword: str, sku_name: str) -> dict[str, str]:
    text = f" {normalize_text(keyword)} {normalize_text(sku_name)} "

    canonical_keyword: str
    subtype_group: str
    product_subtype: str
    category: dict[str, str]
    dirty_reason = ""

    has_fan_signature = (contains_any(text, ["风扇", "FAN", "ARGB", "RGB"]) and FAN_SIZE_PATTERN.search(text) is not None) or contains_any(
        text,
        ["机箱风扇", "散热风扇", "外置风扇", "机箱散热"],
    )
    has_cooler_signature = contains_any(text, ["散热器", "水冷", "风冷", "热管", "双塔", "单塔", "下压", "PA120", "AG620", "AK400", "玄冰"])

    if has_fan_signature and "主板" not in text and "散热器" not in text:
        canonical_keyword = "机箱散热风扇"
        product_subtype = detect_fan_model(text)
        subtype_group = product_subtype
        category = build_fan_category(product_subtype)
    elif has_cooler_signature and "主板" not in text:
        canonical_keyword = "CPU散热器"
        product_subtype = detect_cooler_model(text)
        subtype_group = "水冷" if "水冷" in product_subtype else "风冷"
        category = build_cooler_category(product_subtype)
    elif "主板" in text or MOTHERBOARD_PATTERN.search(text):
        canonical_keyword = "电脑主板"
        product_subtype = detect_motherboard_model(text)
        subtype_group = product_subtype
        category = build_motherboard_category(product_subtype)
    elif "显卡" in text or any(pattern.search(text) for pattern in GPU_PATTERNS):
        canonical_keyword = "显卡"
        product_subtype = detect_gpu_model(text)
        subtype_group = product_subtype.split(" ")[0] if product_subtype != "未识别型号" else "显卡"
        category = build_gpu_category(product_subtype if product_subtype != "未识别型号" else "RTX 5060")
    elif contains_any(text, ["SSD", "固态", "机械硬盘", "HDD", "NVME", "M.2", "硬盘", "U盘", "TF卡", "存储卡"]):
        canonical_keyword = "硬盘"
        product_subtype = detect_storage_model(text)
        subtype_group = category_level3 = build_storage_category(text, product_subtype)["level3"]
        category = build_storage_category(text, product_subtype)
        subtype_group = category_level3
    elif (contains_any(text, ["电源", "ATX", "SFX", "TFX", "FLEX", "额定"]) and PSU_WATT_PATTERN.search(text)) or normalize_text(keyword) == "台式电脑电源":
        canonical_keyword = "台式机电源"
        product_subtype = detect_psu_model(text)
        subtype_group = product_subtype
        category = build_psu_category(text, product_subtype)
    elif contains_any(text, ["内存", "DDR3", "DDR4", "DDR5", "ECC", "SO-DIMM", "UDIMM"]):
        canonical_keyword = "内存条"
        product_subtype = detect_memory_model(text)
        subtype_group = product_subtype.split(" ")[0]
        category = build_memory_category(text, product_subtype)
    else:
        canonical_keyword = "CPU"
        product_subtype = detect_cpu_model(text)
        subtype_group = "CPU"
        category = build_cpu_category(text, product_subtype)

    is_dirty = False
    if canonical_keyword == "显卡" and product_subtype == "未识别型号":
        is_dirty = True
        dirty_reason = "显卡型号未识别"
    elif canonical_keyword == "电脑主板" and product_subtype == "未识别芯片组":
        is_dirty = True
        dirty_reason = "主板芯片组未识别"
    elif canonical_keyword == "台式机电源" and product_subtype == "未识别功率":
        is_dirty = True
        dirty_reason = "电源功率未识别"
    elif canonical_keyword == "CPU" and product_subtype == "未识别型号":
        is_dirty = True
        dirty_reason = "CPU 型号未识别"
    elif canonical_keyword == "内存条" and product_subtype in {"DDR?", "DDR? 8GB"}:
        is_dirty = True
        dirty_reason = "内存规格未识别"

    remark = find_catalog_remark(category["level1"], category["level2"], category["level3"], category["level4"])
    return {
        "canonical_keyword": canonical_keyword,
        "subtype_group": subtype_group,
        "product_subtype": product_subtype,
        "category_level1": category["level1"],
        "category_level2": category["level2"],
        "category_level3": category["level3"],
        "category_level4": category["level4"],
        "category_remark": remark,
        "is_dirty_data": 1 if is_dirty else 0,
        "dirty_reason": dirty_reason,
    }
