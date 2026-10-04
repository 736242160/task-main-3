#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
样本流转状态校验工具（纯 Python 标准库，单文件）

输入：
  - 样本定义：(样本编号, 类型[血清|尿液], 采集科室)
  - 流转流：  (样本编号, 环节[采集|转运|接收|检测|审核|报告], 科室)
  - 异常流：  (样本编号, 异常类型[溶血|脂血])

输出：
  - 每个样本的最终状态（当前环节、状态、异常、报告结论）
  - 错误清单（定位到样本与环节）

校验规则：
  1. 环节必须按 采集→转运→接收→检测→审核→报告 顺序推进，
     跳环节（如未接收先检测）或回退均报错并定位环节。
  2. 异常（溶血/脂血）样本的后续检测结果级联标记为「可疑」并报错。
  3. 复查规则（自定）：可疑样本允许再做一次「检测」作为复查，
     复查后解除可疑标记、状态变为「已复查」；除此之外重复到达同一环节报错。
  4. 样本到达「审核」后自动生成「报告」；报告之后任何流转均被拦截。
  5. 流转/异常引用不存在的样本要报错。
  6. 同一样本的多条流转记录状态连续（跨流状态延续）。
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

STAGES = ["采集", "转运", "接收", "检测", "审核", "报告"]
STAGE_INDEX = {name: idx for idx, name in enumerate(STAGES)}
SAMPLE_TYPES = {"血清", "尿液"}
ANOMALY_TYPES = {"溶血", "脂血"}


@dataclass
class Sample:
    sample_id: str
    sample_type: str
    dept: str
    stage_idx: int = -1                 # -1 表示尚未开始流转
    status: str = "正常"                # 正常 / 可疑 / 已复查
    anomalies: List[str] = field(default_factory=list)
    suspicious: bool = False
    rechecked: bool = False
    report: Optional[dict] = None
    history: List[str] = field(default_factory=list)

    @property
    def stage(self) -> str:
        return STAGES[self.stage_idx] if self.stage_idx >= 0 else "未开始"


class FlowChecker:
    """样本流转校验器：先登记样本与异常，再按顺序处理流转流。"""

    def __init__(self) -> None:
        self.samples: Dict[str, Sample] = {}
        self.errors: List[dict] = []

    # ---------- 错误记录 ----------
    def _error(self, sample_id: str, stage: str, message: str) -> None:
        self.errors.append({"样本编号": sample_id, "环节": stage, "错误": message})

    # ---------- 样本登记 ----------
    def add_sample(self, sample_id: str, sample_type: str, dept: str) -> None:
        if sample_id in self.samples:
            self._error(sample_id, "登记", "样本编号重复登记")
            return
        if sample_type not in SAMPLE_TYPES:
            self._error(sample_id, "登记", f"未知样本类型「{sample_type}」（应为 血清/尿液）")
            return
        self.samples[sample_id] = Sample(sample_id, sample_type, dept)

    # ---------- 异常登记 ----------
    def add_anomaly(self, sample_id: str, anomaly_type: str) -> None:
        sample = self.samples.get(sample_id)
        if sample is None:
            self._error(sample_id, "异常登记", "异常记录引用不存在的样本")
            return
        if anomaly_type not in ANOMALY_TYPES:
            self._error(sample_id, "异常登记", f"未知异常类型「{anomaly_type}」（应为 溶血/脂血）")
            return
        if anomaly_type not in sample.anomalies:
            sample.anomalies.append(anomaly_type)
            sample.history.append(f"发现异常：{anomaly_type}")

    # ---------- 流转处理 ----------
    def process_flow(self, sample_id: str, stage: str, dept: str) -> None:
        if stage not in STAGE_INDEX:
            self._error(sample_id, stage, f"未知环节「{stage}」")
            return
        sample = self.samples.get(sample_id)
        if sample is None:
            self._error(sample_id, stage, "流转引用不存在的样本")
            return
        if sample.stage_idx == STAGE_INDEX["报告"]:
            self._error(sample_id, stage,
                        f"样本已出报告，后续流转「{stage}」（{dept}）被拦截")
            return

        target = STAGE_INDEX[stage]

        # 重复到达同一环节：可疑样本的第二次「检测」按复查规则放行
        if target == sample.stage_idx:
            if stage == "检测" and sample.suspicious and not sample.rechecked:
                sample.rechecked = True
                sample.suspicious = False
                sample.status = "已复查"
                sample.history.append(f"复查检测（{dept}）：可疑标记解除")
            else:
                self._error(sample_id, stage, f"重复到达环节「{stage}」（{dept}）")
            return

        # 环节回退
        if target < sample.stage_idx:
            self._error(sample_id, stage,
                        f"环节回退：当前在「{sample.stage}」，不能回到「{stage}」（{dept}）")
            return

        # 跳环节（如未接收先检测）
        if target > sample.stage_idx + 1:
            missing = STAGES[sample.stage_idx + 1: target]
            self._error(sample_id, stage,
                        f"环节顺序错乱：未{'、未'.join(missing)}先{stage}（{dept}），本次流转被拒绝")
            return

        # 正常推进一个环节
        sample.stage_idx = target
        sample.history.append(f"{stage}（{dept}）")

        # 异常样本的检测：级联标记可疑
        if stage == "检测" and sample.anomalies and not sample.rechecked:
            sample.suspicious = True
            sample.status = "可疑"
            self._error(sample_id, stage,
                        f"异常样本（{'、'.join(sample.anomalies)}）执行检测，"
                        f"检测结果级联标记为可疑，需复查")

        # 审核通过后自动生成报告
        if stage == "审核":
            sample.stage_idx = STAGE_INDEX["报告"]
            conclusion = {
                "可疑": "结果可疑，建议复查后重新审核",
                "已复查": "复查后结果可信",
                "正常": "结果正常",
            }[sample.status]
            sample.report = {
                "审核科室": dept,
                "结论": conclusion,
                "审核时状态": sample.status,
            }
            sample.history.append(f"报告（审核通过后自动生成）：{conclusion}")

    # ---------- 一键运行 ----------
    def run(self,
            samples: List[Tuple[str, str, str]],
            flows: List[Tuple[str, str, str]],
            anomalies: List[Tuple[str, str]]) -> None:
        for sample_id, sample_type, dept in samples:
            self.add_sample(sample_id, sample_type, dept)
        for sample_id, anomaly_type in anomalies:
            self.add_anomaly(sample_id, anomaly_type)
        for sample_id, stage, dept in flows:
            self.process_flow(sample_id, stage, dept)

    # ---------- 输出 ----------
    def sample_states(self) -> List[dict]:
        states = []
        for s in self.samples.values():
            states.append({
                "样本编号": s.sample_id,
                "类型": s.sample_type,
                "采集科室": s.dept,
                "当前环节": s.stage,
                "状态": s.status,
                "异常": "、".join(s.anomalies) if s.anomalies else "无",
                "报告结论": s.report["结论"] if s.report else "未出报告",
            })
        return states

    def print_report(self) -> None:
        print("=" * 72)
        print("样本状态")
        print("=" * 72)
        header = f"{'编号':<8}{'类型':<6}{'采集科室':<12}{'当前环节':<8}{'状态':<8}{'异常':<8}报告结论"
        print(header)
        print("-" * 72)
        for st in self.sample_states():
            print(f"{st['样本编号']:<8}{st['类型']:<6}{st['采集科室']:<12}"
                  f"{st['当前环节']:<8}{st['状态']:<8}{st['异常']:<8}{st['报告结论']}")

        print()
        print("=" * 72)
        print(f"错误清单（共 {len(self.errors)} 条）")
        print("=" * 72)
        if not self.errors:
            print("无错误。")
        for i, err in enumerate(self.errors, 1):
            print(f"{i:>2}. [{err['样本编号']} | {err['环节']}] {err['错误']}")

        print()
        print("=" * 72)
        print("流转轨迹")
        print("=" * 72)
        for s in self.samples.values():
            print(f"{s.sample_id}：")
            for h in s.history:
                print(f"    - {h}")


def check_sample_flow(samples, flows, anomalies) -> FlowChecker:
    """便捷入口：传入三类记录，返回校验器（含样本状态与错误清单）。"""
    checker = FlowChecker()
    checker.run(samples, flows, anomalies)
    return checker


# ============================ 示例调用 ============================
if __name__ == "__main__":
    # 样本定义：(编号, 类型, 采集科室)
    samples = [
        ("S001", "血清", "门诊采血室"),
        ("S002", "尿液", "检验科"),
        ("S003", "血清", "急诊科"),
        ("S004", "血清", "门诊采血室"),
        ("S005", "尿液", "住院部"),
    ]

    # 异常流：(样本编号, 异常类型)
    anomalies = [
        ("S003", "溶血"),   # 溶血样本：后续检测应标记可疑
        ("S999", "溶血"),   # 引用不存在的样本 → 报错
    ]

    # 流转流：(样本编号, 环节, 科室)
    flows = [
        # S001：正常全流程，审核后自动生成报告；再显式走「报告」应被拦截
        ("S001", "采集", "门诊采血室"),
        ("S001", "转运", "标本运送组"),
        ("S001", "接收", "检验科"),
        ("S001", "检测", "检验科"),
        ("S001", "审核", "检验科"),       # → 自动生成报告
        ("S001", "报告", "检验科"),       # → 已出报告，拦截

        # S002：跳环节（未转运、未接收先检测）→ 报错；后续流转状态延续
        ("S002", "采集", "检验科"),
        ("S002", "检测", "检验科"),       # → 顺序错乱
        ("S002", "转运", "标本运送组"),   # → 状态延续，继续正常推进
        ("S002", "接收", "检验科"),
        ("S002", "检测", "检验科"),
        ("S002", "审核", "检验科"),

        # S003：溶血样本，首次检测标记可疑，第二次检测按复查规则放行
        ("S003", "采集", "急诊科"),
        ("S003", "转运", "标本运送组"),
        ("S003", "接收", "检验科"),
        ("S003", "检测", "检验科"),       # → 溶血样本检测，级联标记可疑
        ("S003", "检测", "检验科"),       # → 复查，解除可疑
        ("S003", "审核", "检验科"),       # → 自动生成报告（复查后结果可信）

        # S004：重复到达同一环节
        ("S004", "采集", "门诊采血室"),
        ("S004", "采集", "门诊采血室"),   # → 重复到达

        # S005：出报告后仍有流转 → 拦截
        ("S005", "采集", "住院部"),
        ("S005", "转运", "标本运送组"),
        ("S005", "接收", "检验科"),
        ("S005", "检测", "检验科"),
        ("S005", "审核", "检验科"),       # → 自动生成报告
        ("S005", "转运", "标本运送组"),   # → 报告后流转，拦截

        # 引用不存在的样本
        ("S777", "采集", "检验科"),       # → 样本不存在
    ]

    checker = check_sample_flow(samples, flows, anomalies)
    checker.print_report()
