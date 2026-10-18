from __future__ import annotations

import re
from typing import Any


MEMORY_CAPACITY_ORDER = [2, 4, 8, 12, 16, 24, 32, 48, 64, 96, 128]
GPU_PATTERNS = [
    re.compile(r"\bRTX\s*(\d{4})\s*(TI|SUPER)?\b", re.I),
    re.compile(r"\bGTX\s*(\d{3,4})\s*(TI|SUPER)?\b", re.I),
    re.compile(r"\bRX\s*(\d{4,5})\s*(XT|GRE)?\b", re.I),
    re.compile(r"\bGT\s*(\d{3,4})\b", re.I),
    re.compile(r"\bARC\s*(A\d{3})\b", re.I),
]
CPU_PATTERNS = [
    re.compile(r"\bI([3579])[\s\-]?(\d{4,5}[A-Z]{0,3})\b", re.I),
    re.compile(r"\b(ULTRA\s*\d{3}[A-Z]?)\b", re.I),
    re.compile(r"\b(\d{4,5}X3D|\d{4,5}X|\d{4,5}G|\d{4,5}GT|\d{4,5}F)\b", re.I),
    re.compile(r"\bG(\d{4})\b", re.I),
]
MOTHERBOARD_PATTERNS = [
    re.compile(r"\b(X99|X79|X670E|X670|X870E|X870|B860|B850M|B850|B760M|B760|B650M|B650|B550M|B550|H610M|H610|H510M|H510|Z790|Z890|A520M|A520|A320)\b", re.I),
]
COOLER_WATER_PATTERN = re.compile(r"\b(120|240|280|360|420)\b")
COOLER_HEATPIPE_PATTERN = re.compile(r"([45678])\s*热管")
PSU_WATT_PATTERN = re.compile(r"\b(300|400|450|500|550|600|650|700|750|800|850|1000|1200|1300)\s*W\b", re.I)
FAN_SIZE_PATTERN = re.compile(r"\b(8|9|12|14)\s*CM\b|(\d{2,3})\s*MM\b", re.I)
MEMORY_GEN_PATTERN = re.compile(r"DDR\s*([345])L?", re.I)
MEMORY_KIT_PATTERN = re.compile(r"(4|8|12|16|24|32|48|64)\s*(?:G|GB)\s*[*X×]\s*(2|4)", re.I)
MEMORY_CAP_PATTERN = re.compile(r"(2|4|8|12|16|24|32|48|64|96|128)\s*(?:G|GB)", re.I)
STORAGE_PATTERN = re.compile(r"\b(128|240|250|256|480|500|512|960)\s*(G|GB)\b|\b(1|2|4|8)\s*T(B)?\b", re.I)


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").upper()).strip()


def has_any(text: str, keywords: list[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def matches_any(text: str, patterns: list[re.Pattern[str]]) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def has_case_fan_signature(text: str) -> bool:
    return (
        ("风扇" in text or "FAN" in text or "ARGB" in text or "RGB" in text)
        and FAN_SIZE_PATTERN.search(text) is not None
    ) or has_any(text, ["机箱风扇", "散热风扇", "外置风扇", "机箱散热"])


def has_cooler_signature(text: str) -> bool:
    return has_any(
        text,
        ["散热器", "水冷", "风冷", "热管", "塔式", "下压", "PA120", "AG620", "AK400", "CR1400", "玄冰"],
    )


def has_cpu_signature(text: str) -> bool:
    return matches_any(text, CPU_PATTERNS) or has_any(
        text,
        ["CPU", "处理器", "锐龙", "酷睿", "INTEL", "奔腾", "赛扬", "ULTRA"],
    )


def has_memory_signature(text: str) -> bool:
    return has_any(text, ["内存", "DDR3", "DDR4", "DDR5", "ECC", "SO-DIMM", "UDIMM"])


def has_storage_signature(text: str) -> bool:
    return has_any(text, ["SSD", "固态", "机械硬盘", "HDD", "NVME", "M.2", "硬盘"])


def has_motherboard_signature(text: str) -> bool:
    return "主板" in text or matches_any(text, MOTHERBOARD_PATTERNS)


def has_gpu_signature(text: str) -> bool:
    return "显卡" in text or matches_any(text, GPU_PATTERNS)


def has_psu_signature(text: str) -> bool:
    return has_any(text, ["电源", "ATX", "SFX", "额定", "全模组", "非模组"]) and PSU_WATT_PATTERN.search(text) is not None


def extract_memory_capacity(text: str) -> str:
    kit_match = MEMORY_KIT_PATTERN.search(text)
    if kit_match:
        single = int(kit_match.group(1))
        count = int(kit_match.group(2))
        return f"{single * count}GB套装"

    values = [int(value) for value in MEMORY_CAP_PATTERN.findall(text)]
    values = [value for value in values if value in MEMORY_CAPACITY_ORDER]
    if not values:
        return ""

    unique_values = sorted(set(values))
    if len(unique_values) >= 3:
        return f"{max(unique_values)}GB"
    return f"{unique_values[0]}GB"


def extract_storage_capacity(text: str) -> str:
    matches = STORAGE_PATTERN.findall(text)
    if not matches:
        return ""

    for g_value, _, t_value, _ in matches:
        if t_value:
            return f"{t_value}TB"
        if g_value:
            return f"{g_value}GB"
    return ""


def classify_gpu(text: str) -> tuple[str, str]:
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
            prefix = "ARC"
        elif "GT " in pattern.pattern.upper():
            prefix = "GT"

        label = f"{prefix} {parts[0]}"
        if len(parts) > 1:
            label = f"{label} {parts[1].upper()}"
        return "显卡", label
    return "显卡", "未识别型号"


def classify_cpu(text: str) -> tuple[str, str]:
    intel_match = CPU_PATTERNS[0].search(text)
    if intel_match:
        return "CPU", f"i{intel_match.group(1)}-{intel_match.group(2).upper()}"

    ultra_match = CPU_PATTERNS[1].search(text)
    if ultra_match:
        return "CPU", re.sub(r"\s+", " ", ultra_match.group(1).upper()).strip()

    for pattern in CPU_PATTERNS[2:]:
        match = pattern.search(text)
        if not match:
            continue
        token = match.group(0).upper()
        if token.startswith("G") and token[1:].isdigit():
            return "CPU", f"奔腾 {token}"
        return "CPU", token

    return "CPU", "未识别型号"


def classify_motherboard(text: str) -> tuple[str, str]:
    for pattern in MOTHERBOARD_PATTERNS:
        match = pattern.search(text)
        if match:
            return "电脑主板", match.group(1).upper()
    return "电脑主板", "未识别芯片组"


def classify_memory(text: str) -> tuple[str, str]:
    generation = "DDR?"
    gen_match = MEMORY_GEN_PATTERN.search(text)
    if gen_match:
        generation = f"DDR{gen_match.group(1)}"
    capacity = extract_memory_capacity(text)
    if capacity:
        return "内存条", f"{generation} {capacity}"
    return "内存条", generation


def classify_storage(text: str) -> tuple[str, str]:
    storage_type = "SSD"
    if has_any(text, ["机械硬盘", "HDD", "7200转", "5400转"]):
        storage_type = "HDD"
    elif has_any(text, ["NVME", "M.2", "PCIE4", "PCIE 4", "PCIE5", "PCIE 5"]):
        storage_type = "NVMe SSD"
    elif "SATA" in text:
        storage_type = "SATA SSD"

    capacity = extract_storage_capacity(text)
    if capacity:
        return "硬盘", f"{capacity} {storage_type}"
    return "硬盘", storage_type


def classify_psu(text: str) -> tuple[str, str]:
    watt_match = PSU_WATT_PATTERN.search(text)
    if watt_match:
        return "台式机电源", f"{watt_match.group(1)}W"
    return "台式机电源", "未识别功率"


def classify_cooler(text: str) -> tuple[str, str]:
    if "水冷" in text:
        size_match = COOLER_WATER_PATTERN.search(text)
        if size_match:
            return "CPU散热器", f"{size_match.group(1)}水冷"
        return "CPU散热器", "一体式水冷"

    tower_prefix = "双塔" if "双塔" in text else "单塔"
    heatpipe_match = COOLER_HEATPIPE_PATTERN.search(text)
    if heatpipe_match:
        return "CPU散热器", f"{tower_prefix}{heatpipe_match.group(1)}热管"
    if "下压" in text:
        return "CPU散热器", "下压式风冷"
    return "CPU散热器", "塔式风冷"


def classify_case_fan(text: str) -> tuple[str, str]:
    size_match = FAN_SIZE_PATTERN.search(text)
    size = ""
    if size_match:
        size = size_match.group(1) or size_match.group(2) or ""
    style = "ARGB" if "ARGB" in text or "RGB" in text else "无光"
    if size:
        return "机箱散热风扇", f"{size}CM {style}"
    return "机箱散热风扇", style


def infer_category(text: str, keyword: str) -> tuple[str, str]:
    normalized_keyword = normalize_text(keyword)

    if has_case_fan_signature(text) and not has_psu_signature(text):
        return classify_case_fan(text)
    if has_cooler_signature(text) and "风扇" not in normalized_keyword:
        return classify_cooler(text)

    if "内存" in normalized_keyword:
        return classify_memory(text)
    if "主板" in normalized_keyword:
        return classify_motherboard(text)
    if "显卡" in normalized_keyword:
        return classify_gpu(text)
    if "散热器" in normalized_keyword:
        return classify_cooler(text)
    if "电源" in normalized_keyword and has_psu_signature(text):
        return classify_psu(text)
    if "硬盘" in normalized_keyword:
        return classify_storage(text)
    if normalized_keyword in {"CPU", "CPU处理器", "CPU套装"} or normalized_keyword.lower() == "cpu":
        return classify_cpu(text)

    if has_gpu_signature(text):
        return classify_gpu(text)
    if has_memory_signature(text):
        return classify_memory(text)
    if has_motherboard_signature(text):
        return classify_motherboard(text)
    if has_storage_signature(text):
        return classify_storage(text)
    if has_psu_signature(text):
        return classify_psu(text)
    if has_cpu_signature(text):
        return classify_cpu(text)

    return "未分类", "未识别小类"


def derive_product_taxonomy(keyword: str, sku_name: str) -> dict[str, str]:
    raw_text = f"{keyword} {sku_name}"
    text = f" {normalize_text(raw_text)} "
    canonical_keyword, product_subtype = infer_category(text, keyword)

    subtype_group = canonical_keyword
    if canonical_keyword == "内存条":
        subtype_group = product_subtype.split(" ")[0]
    elif canonical_keyword == "硬盘":
        subtype_group = product_subtype.split(" ")[-1]
    elif canonical_keyword == "CPU散热器":
        subtype_group = "水冷" if "水冷" in product_subtype else "风冷"
    elif canonical_keyword == "机箱散热风扇":
        subtype_group = product_subtype.split(" ")[0]
    elif canonical_keyword == "显卡":
        subtype_group = product_subtype.split(" ")[0]

    return {
        "canonical_keyword": canonical_keyword,
        "subtype_group": subtype_group,
        "product_subtype": product_subtype,
    }
