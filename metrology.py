#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""计量器具检定状态链管理工具（纯 Python 标准库，单文件）。

输入文件格式（每行一条指令，# 开头为注释，空行忽略）：

    器具 <编号> <类型:压力表|天平> <检定周期月> [批次]
    检定 <编号> <结果:合格|不合格> [新周期月]
    使用 <编号> <状态:启用|停用|送修>
    到期 <编号>

事件按文件顺序处理，跨流状态自然延续。
输出：器具状态表 + 带行号定位的错误清单。

用法：
    python3 metrology.py 数据文件
    python3 metrology.py - < 数据文件     # 从标准输入读
"""

import argparse
import sys
import unicodedata
from dataclasses import dataclass

STATES = ("在用", "到期", "检定", "停用", "送修", "复役")
VALID_TYPES = ("压力表", "天平")
VALID_RESULTS = ("合格", "不合格")
VALID_USE_ACTIONS = ("启用", "停用", "送修")


@dataclass
class Instrument:
    ident: str
    itype: str
    cycle: int
    batch: str
    state: str = "在用"
    note: str = ""
    last_op: str = "器具"


@dataclass
class Error:
    line: int
    kind: str
    message: str

    def __str__(self):
        return "第{}行 [{}] {}".format(self.line, self.kind, self.message)


def default_batch(ident):
    """未显式给出批次时，取编号的前导非数字部分作为批次。"""
    prefix = []
    for ch in ident:
        if ch.isdigit():
            break
        prefix.append(ch)
    return "".join(prefix) or "默认"


class Processor:
    def __init__(self):
        self.instruments = {}
        self.errors = []

    # ---------- 指令入口 ----------

    def define(self, line_no, ident, itype, cycle_s, batch=None):
        if ident in self.instruments:
            self.err(line_no, "重复定义", "器具 {} 已定义，忽略本次定义".format(ident))
            return
        if itype not in VALID_TYPES:
            self.err(line_no, "类型非法",
                     "器具 {} 类型 '{}' 非法，应为 {}".format(ident, itype, "/".join(VALID_TYPES)))
            return
        try:
            cycle = int(cycle_s)
            if cycle <= 0:
                raise ValueError
        except ValueError:
            self.err(line_no, "周期非法", "器具 {} 检定周期 '{}' 不是正整数".format(ident, cycle_s))
            return
        self.instruments[ident] = Instrument(ident, itype, cycle, batch or default_batch(ident))

    def verify(self, line_no, ident, result, new_cycle_s=None):
        inst = self.get(line_no, ident)
        if inst is None:
            return
        if result not in VALID_RESULTS:
            self.err(line_no, "结果非法", "器具 {} 检定结果 '{}' 非法".format(ident, result))
            return
        if inst.state == "停用":
            self.err(line_no, "停用期间检定",
                     "器具 {} 处于停用状态，本次检定({})被忽略".format(ident, result))
            return
        if inst.state == "检定":
            self.err(line_no, "重复检定",
                     "器具 {} 已处于检定状态（上次检定不合格待处置），本次检定被忽略".format(ident))
            return
        if inst.last_op == "检定":
            self.err(line_no, "重复检定",
                     "器具 {} 上次操作已是检定，本次重复检定被忽略".format(ident))
            return
        prev = inst.state  # 在用/到期/复役/送修（送修即复检）
        inst.state = "检定"
        if result == "合格":
            if new_cycle_s is not None:
                try:
                    new_cycle = int(new_cycle_s)
                    if new_cycle <= 0:
                        raise ValueError
                    inst.cycle = new_cycle  # 检定合格，新周期生效
                except ValueError:
                    self.err(line_no, "周期非法",
                             "器具 {} 新周期 '{}' 不是正整数，沿用原周期".format(ident, new_cycle_s))
            inst.state = "在用"
            inst.note = "复检合格复役" if prev == "送修" else ""
        else:
            inst.state = "检定"
            inst.note = "检定不合格待处置"
        inst.last_op = "检定"

    def use(self, line_no, ident, action):
        inst = self.get(line_no, ident)
        if inst is None:
            return
        if action not in VALID_USE_ACTIONS:
            self.err(line_no, "状态非法", "器具 {} 使用状态 '{}' 非法".format(ident, action))
            return
        handler = {"启用": self._enable, "停用": self._disable, "送修": self._repair}[action]
        handler(line_no, inst)
        inst.last_op = "使用"

    def expire(self, line_no, ident):
        inst = self.get(line_no, ident)
        if inst is None:
            return
        if inst.state in ("在用", "复役"):
            inst.state = "到期"
            inst.last_op = "到期"
        else:
            self.err(line_no, "非法跳转",
                     "器具 {} 当前状态 '{}' 不能接收到期事件".format(ident, inst.state))

    # ---------- 使用流子动作 ----------

    def _enable(self, line_no, inst):
        st = inst.state
        if st in ("在用", "复役"):
            return  # 幂等
        if st == "停用":
            inst.state = "复役"
            inst.note = ""
        elif st == "到期":
            self.err(line_no, "到期未检定仍启用",
                     "器具 {} 已到期未检定，启用被拦截并级联强制停用".format(inst.ident))
            inst.state = "停用"
            inst.note = "到期未检定强制停用"
        elif st == "检定":
            self.err(line_no, "检定不合格后使用",
                     "器具 {} 检定不合格仍被启用，已拦截".format(inst.ident))
            self._cascade_recheck(line_no, inst)
        elif st == "送修":
            self.err(line_no, "送修未复检启用",
                     "器具 {} 送修后未经复检合格即启用，已拦截".format(inst.ident))

    def _disable(self, line_no, inst):
        if inst.state == "停用":
            return
        inst.state = "停用"
        inst.note = ""

    def _repair(self, line_no, inst):
        if inst.state == "送修":
            return
        inst.state = "送修"
        inst.note = ""

    # ---------- 级联 ----------

    def _cascade_recheck(self, line_no, failed):
        """检定不合格器具被启用时，同类型同批次器具级联复查。"""
        peers = [p for p in self.instruments.values()
                 if p.ident != failed.ident
                 and p.itype == failed.itype
                 and p.batch == failed.batch]
        for p in peers:
            if "需复查" not in p.note:
                p.note = (p.note + " " if p.note else "") + "需复查"
        names = "、".join(p.ident for p in peers) or "（无同批次器具）"
        self.err(line_no, "级联复查",
                 "器具 {} 检定不合格，同类型同批次器具需复查：{}".format(failed.ident, names))

    # ---------- 工具 ----------

    def get(self, line_no, ident):
        inst = self.instruments.get(ident)
        if inst is None:
            self.err(line_no, "器具不存在", "引用了未定义的器具 {}".format(ident))
        return inst

    def err(self, line_no, kind, message):
        self.errors.append(Error(line_no, kind, message))


def run(lines):
    proc = Processor()
    for line_no, raw in enumerate(lines, 1):
        text = raw.split("#", 1)[0].strip()
        if not text:
            continue
        parts = text.split()
        cmd, args = parts[0], parts[1:]
        try:
            if cmd == "器具" and len(args) in (3, 4):
                proc.define(line_no, *args)
            elif cmd == "检定" and len(args) in (2, 3):
                proc.verify(line_no, *args)
            elif cmd == "使用" and len(args) == 2:
                proc.use(line_no, *args)
            elif cmd == "到期" and len(args) == 1:
                proc.expire(line_no, *args)
            elif cmd in ("器具", "检定", "使用", "到期"):
                proc.err(line_no, "格式错误", "指令 '{}' 参数个数不对：{}".format(cmd, text))
            else:
                proc.err(line_no, "未知指令", "无法识别的指令：{}".format(text))
        except Exception as exc:  # 兜底，绝不让单条坏数据中断整批处理
            proc.err(line_no, "内部错误", "{}: {}".format(text, exc))
    return proc


def disp_w(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def pad_row(row, widths):
    return "  ".join(cell + " " * (w - disp_w(cell)) for cell, w in zip(row, widths))


def render(proc):
    out = ["=== 器具状态 ==="]
    if proc.instruments:
        header = ("编号", "类型", "批次", "周期(月)", "状态", "备注")
        rows = [(i.ident, i.itype, i.batch, str(i.cycle), i.state, i.note or "-")
                for i in proc.instruments.values()]
        widths = [max(disp_w(r[c]) for r in [header] + rows) for c in range(len(header))]
        out.append(pad_row(header, widths))
        out.extend(pad_row(r, widths) for r in rows)
    else:
        out.append("（无器具定义）")
    out.append("")
    out.append("=== 错误报告（共 {} 条）===".format(len(proc.errors)))
    if proc.errors:
        out.extend(str(e) for e in proc.errors)
    else:
        out.append("（无错误）")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="计量器具检定状态链管理工具")
    ap.add_argument("input", help="输入数据文件，'-' 表示标准输入")
    args = ap.parse_args(argv)
    if args.input == "-":
        lines = sys.stdin.read().splitlines()
    else:
        with open(args.input, encoding="utf-8") as f:
            lines = f.read().splitlines()
    proc = run(lines)
    print(render(proc))
    return 1 if proc.errors else 0


if __name__ == "__main__":
    sys.exit(main())
