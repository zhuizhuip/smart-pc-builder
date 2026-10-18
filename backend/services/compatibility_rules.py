"""PC 装机兼容性规则引擎.

将兼容性规则从 smart_build_engine.py 中抽取并扩展，
支持 YAML 声明式规则配置和程序化检查。
"""

from __future__ import annotations

import re
import yaml
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[2]
RULES_FILE = BASE_DIR / "data" / "compatibility_rules.yaml"


# ============================================================
# 规则加载
# ============================================================

def _load_rules() -> dict[str, Any]:
    """加载 YAML 规则文件。"""
    if not RULES_FILE.exists():
        return {"rules": [], "reference_data": {}}
    with open(RULES_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {"rules": [], "reference_data": {}}


class CompatibilityEngine:
    """PC 装机兼容性检查引擎。"""

    def __init__(self) -> None:
        raw = _load_rules()
        self._rules: list[dict[str, Any]] = raw.get("rules", [])
        self._ref: dict[str, Any] = raw.get("reference_data", {})

    # ---- 辅助方法 ----

    def _gpu_psu_wattage(self, gpu_subtype: str) -> int:
        """从参考数据中查询 GPU 推荐 PSU 瓦数。"""
        table = self._ref.get("gpu_psu_wattage", {})
        # 精确匹配
        if gpu_subtype in table:
            return int(table[gpu_subtype])
        # 模糊匹配
        for key, watt in table.items():
            if key.upper() in gpu_subtype.upper() or gpu_subtype.upper() in key.upper():
                return int(watt)
        return 550  # 默认值

    def _gpu_power_connector(self, gpu_subtype: str) -> str:
        """查询 GPU 所需电源接口类型。"""
        connectors = self._ref.get("gpu_power_connector", {})
        for connector_type, gpu_list in connectors.items():
            for gpu in gpu_list:
                if gpu.upper() in gpu_subtype.upper():
                    return connector_type
        return "8-pin"

    def _gpu_pcie_gen(self, gpu_subtype: str) -> str:
        """查询 GPU PCIe 代际。"""
        gens = self._ref.get("gpu_pcie_gen", {})
        for gen, gpu_list in gens.items():
            for gpu in gpu_list:
                if gpu.upper() in gpu_subtype.upper():
                    return gen
        return "Gen4"

    def _board_pcie_gen(self, board_subtype: str) -> str:
        """查询主板 PCIe 代际。"""
        gens = self._ref.get("board_pcie_gen", {})
        for gen, chipset_list in gens.items():
            for chipset in chipset_list:
                if str(chipset).upper() in board_subtype.upper():
                    return gen
        return "Gen4"

    def _get_chipset_compatible_memory_gen(self, board_subtype: str) -> str:
        """查询主板芯片组支持的内存代际。"""
        table = self._ref.get("board_memory_gen", {})
        upper = board_subtype.upper()
        for gen, chipset_list in table.items():
            for chipset in chipset_list:
                if str(chipset).upper() in upper:
                    return gen.replace("DDR4_or_", "")
        return "DDR4"

    # ================================================================
    # 单规则检查方法 (每个返回 dict 或 None)
    # ================================================================

    def check_gpu_psu_wattage(
        self,
        gpu_subtype: str = "",
        psu_subtype: str = "",
    ) -> dict[str, str] | None:
        """检查显卡功耗与电源瓦数匹配。"""
        if not gpu_subtype or not psu_subtype:
            return None
        required_w = self._gpu_psu_wattage(gpu_subtype)
        psu_str = psu_subtype.rstrip("W")
        try:
            selected_w = int(psu_str) if psu_str.isdigit() else 0
        except (ValueError, TypeError):
            return None
        if selected_w == 0:
            return None
        if selected_w < required_w:
            return {
                "level": "warning",
                "title": f"电源功率不足提醒",
                "detail": (
                    f"{gpu_subtype} 显卡建议电源 ≥ {required_w}W，"
                    f"当前选择 {psu_subtype}（{selected_w}W）可能不够。"
                ),
            }
        return None

    def check_gpu_12vhpwr(
        self,
        gpu_subtype: str = "",
        psu_subtype: str = "",
    ) -> dict[str, str] | None:
        """检查 RTX 40/50 系列是否需要 12VHPWR 接口。"""
        if not gpu_subtype:
            return None
        connector = self._gpu_power_connector(gpu_subtype)
        if connector == "12VHPWR":
            detail = (
                f"{gpu_subtype} 使用 12VHPWR/12V-2x6 供电接口，"
                f"请确认电源是否原生支持该接口或附带转接线。建议选择 ATX 3.0/3.1 规格电源。"
            )
            return {
                "level": "warning",
                "title": "显卡电源接口兼容提醒",
                "detail": detail,
            }
        return None

    def check_pcie_gen(
        self,
        gpu_subtype: str = "",
        board_subtype: str = "",
    ) -> dict[str, str] | None:
        """检查 PCIe 代际匹配。"""
        if not gpu_subtype or not board_subtype:
            return None
        gpu_gen = self._gpu_pcie_gen(gpu_subtype)
        board_gen = self._board_pcie_gen(board_subtype)
        if gpu_gen != board_gen:
            return {
                "level": "info",
                "title": "PCIe 代际带宽提示",
                "detail": (
                    f"{gpu_subtype} 支持 PCIe {gpu_gen}，"
                    f"当前主板 PCIe {board_gen}，可能损失部分带宽（实际游戏性能影响通常较小）。"
                ),
            }
        return None

    def check_bios_update(
        self,
        cpu_subtype: str = "",
        board_subtype: str = "",
    ) -> dict[str, str] | None:
        """检查是否需要 BIOS 更新。"""
        if not cpu_subtype or not board_subtype:
            return None
        combos = self._ref.get("bios_update_combos", [])
        for combo in combos:
            boards = combo.get("boards", [])
            cpus = combo.get("cpus", [])
            note = combo.get("note", "")
            if any(str(b).upper() in board_subtype.upper() for b in boards) and any(
                c.upper() in cpu_subtype.upper() for c in cpus
            ):
                return {
                    "level": "info",
                    "title": "BIOS 更新提醒",
                    "detail": note
                    or (
                        f"{board_subtype} 搭配 {cpu_subtype} 可能需要更新 BIOS 才能正常点亮，"
                        f"建议选择支持 USB BIOS Flashback 的主板或确认出厂 BIOS 版本。"
                    ),
                }
        return None

    def check_itx_psu(
        self,
        case_subtype: str = "",
        psu_subtype: str = "",
    ) -> dict[str, str] | None:
        """检查 ITX 机箱是否配了 ATX 电源。"""
        if not case_subtype or not psu_subtype:
            return None
        case_upper = case_subtype.upper()
        psu_upper = psu_subtype.upper()
        if "ITX" in case_upper and (
            "ATX" in psu_upper or "ATX" in case_subtype
        ):
            # 检查电源是否为 SFX
            if not any(t in psu_upper for t in ["SFX", "FLEX", "TFX", "1U"]):
                return {
                    "level": "warning",
                    "title": "ITX 机箱电源规格不兼容",
                    "detail": (
                        "ITX 机箱通常只支持 SFX/SFX-L/TFX 规格电源，"
                        f"标准 ATX 电源 ({psu_subtype}) 可能无法安装。"
                    ),
                }
        return None

    def check_cooler_ram_clearance(
        self,
        cooler_subtype: str = "",
        memory_subtype: str = "",
    ) -> dict[str, str] | None:
        """检查双塔风冷是否可能挡住高马甲内存。"""
        if not cooler_subtype or not memory_subtype:
            return None
        if "双塔" in cooler_subtype or "大霜塔" in cooler_subtype:
            # 检测高马甲内存
            if any(tag in memory_subtype for tag in ["RGB", "幻光", "皇家", "铂金", "复仇者"]):
                return {
                    "level": "info",
                    "title": "散热器与内存兼容提醒",
                    "detail": (
                        f"双塔风冷 ({cooler_subtype}) 可能遮挡内存插槽，"
                        f"若使用高马甲内存条 ({memory_subtype})，建议确认散热器内存避让设计。"
                    ),
                }
        return None

    def check_cooler_vs_case(
        self,
        cooler_subtype: str = "",
        case_subtype: str = "",
    ) -> dict[str, str] | None:
        """检查散热器是否与机箱兼容（扩展原有逻辑）。"""
        if not cooler_subtype or not case_subtype:
            return None
        case_upper = case_subtype.upper()
        # 双塔 + ITX
        if "双塔" in cooler_subtype and "ITX" in case_upper:
            return {
                "level": "warning",
                "title": "散热器与机箱兼容提醒",
                "detail": (
                    f"双塔风冷 ({cooler_subtype}) 高度可能超出 ITX 机箱限高，"
                    f"建议换用下压式风冷或 120/240 水冷。"
                ),
            }
        # 360 水冷 + ITX
        if "360" in cooler_subtype and "水冷" in cooler_subtype and "ITX" in case_upper:
            return {
                "level": "warning",
                "title": "散热器与机箱兼容提醒",
                "detail": "360 水冷通常不支持 ITX 机箱，请确认机箱是否明确支持 360 冷排安装位。",
            }
        return None

    def check_gpu_vs_case(
        self,
        gpu_subtype: str = "",
        gpu_length_tier: str = "",
        case_subtype: str = "",
    ) -> dict[str, str] | None:
        """检查显卡长度与机箱空间兼容。"""
        if not gpu_subtype or not case_subtype:
            return None
        case_upper = case_subtype.upper()
        if "ITX" in case_upper and gpu_length_tier in ("标准", "超长"):
            return {
                "level": "warning",
                "title": "显卡与机箱兼容提醒",
                "detail": (
                    f"{gpu_subtype} ({gpu_length_tier}) 可能与 {case_subtype} 机箱存在空间冲突，"
                    f"建议确认机箱显卡限长。"
                ),
            }
        if "MATX" in case_upper and "超长" in gpu_length_tier:
            return {
                "level": "info",
                "title": "显卡与机箱空间提示",
                "detail": (
                    f"{gpu_subtype} ({gpu_length_tier}) 可能较长，"
                    f"请确认 {case_subtype} 机箱显卡限长 ≥ 340mm。"
                ),
            }
        return None

    # ================================================================
    # 主板兼容性谓词 (供 choose_motherboard 等直接使用)
    # ================================================================

    def get_compatible_chipsets(self, cpu_platform: str) -> list[str]:
        """返回给定 CPU 平台兼容的主板芯片组列表。"""
        table = self._ref.get("cpu_platform_chipsets", {})
        return [str(c) for c in table.get(cpu_platform, [])]

    def is_board_compatible_with_cpu(
        self, board_subtype: str, cpu_platform: str
    ) -> bool:
        """检查主板是否与 CPU 平台兼容。"""
        chipsets = self.get_compatible_chipsets(cpu_platform)
        upper = board_subtype.upper()
        return any(cs.upper() in upper for cs in chipsets)

    def get_memory_gen_for_board(self, board_subtype: str) -> str:
        """获取主板支持的内存代际。"""
        return self._get_chipset_compatible_memory_gen(board_subtype)

    # ================================================================
    # 批量检查 (兼容现有 check_physical_compatibility 接口)
    # ================================================================

    def check_all(
        self,
        *,
        cpu_subtype: str = "",
        cpu_vendor: str = "",
        cpu_platform: str = "",
        gpu_subtype: str = "",
        gpu_length_tier: str = "",
        board_subtype: str = "",
        psu_subtype: str = "",
        case_subtype: str = "",
        cooler_subtype: str = "",
        memory_subtype: str = "",
        memory_gen: str = "",
    ) -> list[dict[str, str]]:
        """运行所有兼容性检查，返回警告列表。

        每个警告格式: {"level": "warning"|"info", "title": str, "detail": str}
        """
        warnings: list[dict[str, str]] = []

        # --- hard 检查 ---
        if result := self.check_gpu_psu_wattage(
            gpu_subtype=gpu_subtype, psu_subtype=psu_subtype
        ):
            warnings.append(result)

        if result := self.check_gpu_12vhpwr(
            gpu_subtype=gpu_subtype, psu_subtype=psu_subtype
        ):
            warnings.append(result)

        if result := self.check_itx_psu(
            case_subtype=case_subtype, psu_subtype=psu_subtype
        ):
            warnings.append(result)

        # --- soft 检查 ---
        if result := self.check_cooler_vs_case(
            cooler_subtype=cooler_subtype, case_subtype=case_subtype
        ):
            warnings.append(result)

        if result := self.check_cooler_ram_clearance(
            cooler_subtype=cooler_subtype, memory_subtype=memory_subtype
        ):
            warnings.append(result)

        if result := self.check_gpu_vs_case(
            gpu_subtype=gpu_subtype,
            gpu_length_tier=gpu_length_tier,
            case_subtype=case_subtype,
        ):
            warnings.append(result)

        # --- info 检查 ---
        if result := self.check_pcie_gen(
            gpu_subtype=gpu_subtype, board_subtype=board_subtype
        ):
            warnings.append(result)

        if result := self.check_bios_update(
            cpu_subtype=cpu_subtype, board_subtype=board_subtype
        ):
            warnings.append(result)

        return warnings


# ============================================================
# 单例
# ============================================================

_engine: CompatibilityEngine | None = None


def get_compatibility_engine() -> CompatibilityEngine:
    """获取兼容性引擎单例。"""
    global _engine
    if _engine is None:
        _engine = CompatibilityEngine()
    return _engine
