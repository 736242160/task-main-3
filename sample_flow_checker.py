#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sample_flow_checker.py — 检验样本流转状态追踪与错误报告工具（纯 Python 标准库，单文件）

输入（三段文本；# 后为注释，空行忽略）：
  样本定义：每行 `样本编号 样本类型(血清|尿液) 采集科室`
  流转流  ：每行 `样本编号 环节(采集|转运|接收|检测|审核|报告) 科室`
            可穿插 `异常 样本编号 异常类型(溶血|脂血)` 保证时序；
            单独一行 `---` 分隔多条流（状态按样本跨流延续）
  异常流  ：每行 `样本编号 异常类型`（其中的异常按“全部流转结束后登记”处理）

规则：
  R1 环节必须按 采集→转运→接收→检测→审核→报告 逐级推进，跳级/回退即报错并定位环节；
  R2 溶血样本的后续检测要报告，并把样本及其报告级联标记为“可疑”；
  R3 复查规则（自定）：样本发生异常后，必须再做一次检测（复查）才允许审核，否则驳回；
  R4 审核通过即自动生成报告并结案，报告环节之后任何流转一律拦截；
  R5 流转/异常引用不存在的样本即报错；
  R6 重复到达同一环节即报错；
  R7 状态按样本全局延续，跨多条流转流自动接续。

用法：
  python3 sample_flow_checker.py                      # 运行内置示例
  python3 sample_flow_checker.py 样本.txt 流转.txt 异常.txt
"""

import sys
from dataclasses import dataclass, field

STAGES = ["采集", "转运", "接收", "检测", "审核", "报告"]
STAGE_INDEX = {name: i for i, name in enumerate(STAGES)}
SAMPLE_TYPES = {"血清", "尿液"}
EXCEPTION_TYPES = {"溶血", "脂血"}
CASCADE_EXCEPTIONS = {"溶血"}  # 这些异常发生后的检测：报告 + 级联标记可疑


@dataclass
class FlowError:
    location: str
    sample_id: str
    stage: str
    message: str

    def __str__(self):
        return f"[{self.location}] 样本 {self.sample_id} 环节「{self.stage}」：{self.message}"


@dataclass
class Sample:
    sample_id: str
    sample_type: str
    dept: str
    stage_idx: int = -1                      # -1 表示尚未开始流转
    exceptions: list = field(default_factory=list)  # [(异常类型, 登记位置)]
    suspicious: bool = False                 # 级联可疑标记
    recheck_done: bool = False               # 最近一次异常之后是否已复查检测
    report: dict = None                      # 审核通过后自动生成的报告
    history: list = field(default_factory=list)     # [(环节, 科室, 位置)]

    @property
    def closed(self):
        return self.report is not None

    @property
    def stage_name(self):
        return STAGES[self.stage_idx] if self.stage_idx >= 0 else "未开始"


class Engine:
    def __init__(self):
        self.samples = {}
        self.errors = []

    def error(self, location, sample_id, stage, message):
        self.errors.append(FlowError(location, sample_id, stage, message))

    # ---------- 样本定义 ----------
    def add_sample(self, sample_id, sample_type, dept, loc):
        if sample_id in self.samples:
            self.error(loc, sample_id, "样本定义", "样本编号重复定义")
            return
        if sample_type not in SAMPLE_TYPES:
            self.error(loc, sample_id, "样本定义",
                       f"未知样本类型「{sample_type}」（应为：{'、'.join(sorted(SAMPLE_TYPES))}）")
            return
        self.samples[sample_id] = Sample(sample_id, sample_type, dept)

    # ---------- 异常登记 ----------
    def add_exception(self, sample_id, ex_type, loc):
        if ex_type not in EXCEPTION_TYPES:
            self.error(loc, sample_id, "异常", f"未知异常类型「{ex_type}」")
            return
        s = self.samples.get(sample_id)
        if s is None:
            self.error(loc, sample_id, "异常", "异常引用了不存在的样本")
            return
        if s.closed:
            self.error(loc, sample_id, "异常", "样本已出报告并结案，不再接受异常登记")
            return
        s.exceptions.append((ex_type, loc))
        s.recheck_done = False  # 新异常发生后必须重新复查（R3）

    # ---------- 流转 ----------
    def transfer(self, sample_id, stage, dept, loc):
        if stage not in STAGE_INDEX:
            self.error(loc, sample_id, stage, f"未知环节「{stage}」")
            return
        s = self.samples.get(sample_id)
        if s is None:
            self.error(loc, sample_id, stage, "流转引用了不存在的样本")  # R5
            return
        if s.closed:
            self.error(loc, sample_id, stage,
                       "样本已出报告并结案，报告环节后禁止再流转（已拦截）")  # R4
            return
        idx = STAGE_INDEX[stage]
        if idx == s.stage_idx:
            # 复查规则（R3）：异常后允许重新进入「检测」环节进行复查，不算重复到达
            if stage == "检测" and s.exceptions and not s.recheck_done:
                self._on_test(s, loc)
                s.history.append((stage, dept, loc + "（复查）"))
                return
            self.error(loc, sample_id, stage, f"重复到达同一环节「{stage}」")  # R6
            return
        if idx < s.stage_idx:
            self.error(loc, sample_id, stage,
                       f"环节顺序错乱：当前已在「{s.stage_name}」，不能回退到「{stage}」")  # R1
            return
        if idx > s.stage_idx + 1:
            skipped = "、".join(STAGES[s.stage_idx + 1:idx])
            self.error(loc, sample_id, stage,
                       f"环节顺序错乱：尚未经过「{skipped}」就到达「{stage}」")  # R1
            return
        # 逐级推进，分环节处理
        if stage == "检测":
            self._on_test(s, loc)
        elif stage == "审核":
            if not self._on_review(s, loc):
                return  # 未复查，审核被驳回，状态不变
            s.stage_idx = STAGE_INDEX["报告"]  # 审核通过 → 报告自动生成并结案（R4）
            s.history.append(("审核", dept, loc))
            s.history.append(("报告", dept, loc + "（自动生成）"))
            return
        s.stage_idx = idx
        s.history.append((stage, dept, loc))

    def _on_test(self, s, loc):
        if not s.exceptions:
            return
        s.recheck_done = True  # 异常之后的检测即复查（R3）
        cascade = [e for e, _ in s.exceptions if e in CASCADE_EXCEPTIONS]
        if cascade:
            s.suspicious = True  # 级联标记可疑（R2）
            self.error(loc, s.sample_id, "检测",
                       f"「{'、'.join(cascade)}」样本的后续检测，结果级联标记为可疑")

    def _on_review(self, s, loc):
        if s.exceptions and not s.recheck_done:
            self.error(loc, s.sample_id, "审核",
                       "异常样本未复查（异常后未再次检测），审核被驳回")  # R3
            return False
        s.report = {
            "样本编号": s.sample_id,
            "样本类型": s.sample_type,
            "采集科室": s.dept,
            "异常": "、".join(e for e, _ in s.exceptions) or "无",
            "结论": "可疑（需结合临床复查）" if s.suspicious else "正常",
            "生成位置": loc,
        }
        return True


# ---------- 输入解析 ----------
def _clean_lines(text):
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            yield line


def load(engine, samples_text, flows_text, exceptions_text=""):
    for n, line in enumerate(_clean_lines(samples_text), 1):
        parts = line.split()
        loc = f"样本定义 第{n}条"
        if len(parts) != 3:
            engine.error(loc, "?", "样本定义", f"格式错误：{line!r}（应为：编号 类型 采集科室）")
            continue
        engine.add_sample(parts[0], parts[1], parts[2], loc)

    flow_no, evt_no = 1, 0
    for line in _clean_lines(flows_text):
        if set(line) == {"-"}:  # --- 分隔多条流
            flow_no, evt_no = flow_no + 1, 0
            continue
        evt_no += 1
        parts = line.split()
        loc = f"流{flow_no} 第{evt_no}条"
        if parts[0] == "异常" and len(parts) == 3:
            engine.add_exception(parts[1], parts[2], loc)
        elif len(parts) == 3:
            engine.transfer(parts[0], parts[1], parts[2], loc)
        else:
            engine.error(loc, "?", "流转", f"格式错误：{line!r}（应为：编号 环节 科室 或 异常 编号 类型）")

    for n, line in enumerate(_clean_lines(exceptions_text), 1):
        parts = line.split()
        loc = f"异常流 第{n}条"
        if len(parts) != 2:
            engine.error(loc, "?", "异常", f"格式错误：{line!r}（应为：编号 异常类型）")
            continue
        engine.add_exception(parts[0], parts[1], loc)
    return engine


# ---------- 输出 ----------
def format_report(engine):
    out = ["=" * 66, "一、样本状态清单", "=" * 66]
    for s in engine.samples.values():
        ex = "、".join(e for e, _ in s.exceptions) or "无"
        out.append(
            f"样本 {s.sample_id}（{s.sample_type}，{s.dept}）\n"
            f"    当前环节：{s.stage_name}    异常：{ex}    "
            f"可疑：{'是' if s.suspicious else '否'}    "
            f"已复查：{'是' if s.recheck_done else '否'}    "
            f"状态：{'已结案' if s.closed else '在途'}"
        )
        if s.report:
            r = s.report
            out.append(
                f"    报告（审核后自动生成）：结论={r['结论']}，异常={r['异常']}，"
                f"生成于 {r['生成位置']}"
            )
    out += ["", "=" * 66, "二、错误/问题清单", "=" * 66]
    if not engine.errors:
        out.append("（无）")
    for i, e in enumerate(engine.errors, 1):
        out.append(f"{i:2d}. {e}")
    return "\n".join(out)


# ---------- 示例 ----------
DEMO_SAMPLES = """\
# 编号  类型  采集科室
S001 血清 检验科
S002 尿液 肾内科
S003 血清 心内科
S004 尿液 内分泌科
S005 血清 普外科
S006 血清 呼吸科
S007 尿液 急诊科
"""

DEMO_FLOWS = """\
# ---- 流 1 ----
S001 采集 检验科
S001 转运 检验科
S001 接收 检验科
S001 检测 检验科
S001 审核 检验科        # 审核通过 → 报告自动生成并结案
S001 报告 检验科        # 应被拦截：报告环节后禁止再流转
S002 采集 肾内科
S002 转运 肾内科
S002 接收 肾内科
异常 S002 溶血          # 接收后发现溶血
S003 采集 心内科
S003 转运 心内科
S003 检测 心内科        # 顺序错乱：未接收先检测
S003 接收 心内科        # 回到正轨
S003 检测 心内科
S003 审核 心内科
S005 采集 普外科
S005 转运 普外科
S005 接收 普外科
S005 检测 普外科
S005 审核 普外科        # 自动报告结案
S006 采集 呼吸科
S006 转运 呼吸科
S006 接收 呼吸科
S006 接收 呼吸科        # 重复到达同一环节
S999 采集 检验科        # 不存在的样本
S007 采集 急诊科
S007 转运 急诊科
---
# ---- 流 2（跨流状态延续）----
S002 检测 肾内科        # 溶血后检测：报告 + 级联可疑（同时即复查）
S002 审核 肾内科        # 已复查 → 通过，自动生成“可疑”报告
S004 采集 内分泌科
S004 转运 内分泌科
S004 接收 内分泌科
S004 检测 内分泌科
异常 S004 脂血          # 检测后登记脂血，需复查
S004 审核 内分泌科      # 应被驳回：异常后未复查
S004 检测 内分泌科      # 复查检测
S004 审核 内分泌科      # 复查完成 → 通过
S005 转运 普外科        # 应被拦截：S005 已结案
S007 接收 急诊科        # 跨流延续：正常推进
S007 检测 急诊科
S007 审核 急诊科
"""

DEMO_EXCEPTIONS = """\
S998 溶血   # 不存在的样本
"""


def main():
    if len(sys.argv) == 4:
        with open(sys.argv[1], encoding="utf-8") as f:
            samples_text = f.read()
        with open(sys.argv[2], encoding="utf-8") as f:
            flows_text = f.read()
        with open(sys.argv[3], encoding="utf-8") as f:
            exceptions_text = f.read()
    else:
        samples_text, flows_text, exceptions_text = DEMO_SAMPLES, DEMO_FLOWS, DEMO_EXCEPTIONS

    engine = load(Engine(), samples_text, flows_text, exceptions_text)
    print(format_report(engine))

    # 关键断言，便于直接验证规则正确性
    if len(sys.argv) != 4:
        s = engine.samples
        assert s["S001"].closed and not s["S001"].suspicious          # 正常结案
        assert s["S002"].closed and s["S002"].suspicious              # 溶血级联可疑
        assert s["S002"].report["结论"].startswith("可疑")            # 报告级联标记
        assert s["S003"].closed                                       # 顺序错乱纠正后结案
        assert s["S004"].closed and s["S004"].recheck_done            # 复查后结案
        assert s["S005"].closed                                       # 结案后流转被拦截
        assert s["S006"].stage_name == "接收" and not s["S006"].closed
        assert s["S007"].closed                                       # 跨流延续结案
        assert len(engine.errors) == 8, f"预期 8 条错误，实际 {len(engine.errors)}"
        print("\n内置示例断言全部通过。")


if __name__ == "__main__":
    main()
