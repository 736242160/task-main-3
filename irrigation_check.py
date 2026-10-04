#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""灌区渠系供水层级联动校验工具（纯 Python 标准库，单文件）。

输入为分区段文本（# 之后为注释）：

    [渠系]    名称 类型(干渠|支渠|斗渠) 上级渠(- 表示无) 设计流量
    [轮灌组]  名称 渠列表(逗号/顿号分隔) 需水量
    [供水]    [时刻] 组 开始|结束
    [闸门]    [时刻] 渠 开|关
    [水量]    [时刻] 渠 实际供水量

事件可带数字时刻前缀；带时刻的事件按时刻升序处理，无时刻的保持文件先后
顺序。三条事件流汇入同一引擎，闸门/供水/水量状态跨流延续。

校验规则：
  1. 组开始供水时其渠闸门未开：GATE_NOT_OPEN，报告并拦截；
  2. 同一渠被不同组同时供水：GROUP_CONFLICT，报告并定位渠与双方组；
  3. 支渠累计供水超上级干渠实际供水：OVER_SUPPLY，报告并级联标记下级缺水；
  4. 上级渠（干渠）闸门关闭时下级渠供水：PARENT_GATE_CLOSED；
  5. 组结束供水时累计供水与需水（含往轮调整）偏差超阈值：DEMAND_MISMATCH，
     差额级联计入后续轮次；
  6. 闸门/水量引用不存在的渠：UNKNOWN_CANAL；
  7. 供水引用不存在的组：UNKNOWN_GROUP。
"""

import argparse
import sys

CANAL_TYPES = ("干渠", "支渠", "斗渠")
SECTIONS = ("渠系", "轮灌组", "供水", "闸门", "水量")
DEFAULT_THRESHOLD = 0.10


class Canal:
    def __init__(self, name, ctype, parent, design_flow):
        self.name = name
        self.ctype = ctype
        self.parent = parent
        self.design_flow = design_flow


class Group:
    def __init__(self, name, canals, demand):
        self.name = name
        self.canals = canals
        self.demand = demand


class CanalState:
    def __init__(self):
        self.gate_open = False
        self.cum_water = 0.0
        self.shortage = False
        self.over_reported = False
        self.supplied_by = set()


class GroupState:
    def __init__(self):
        self.active = False
        self.received = 0.0
        self.adjustment = 0.0
        self.rounds = []


def is_number(token):
    try:
        float(token)
        return True
    except ValueError:
        return False


def parse(text):
    canals, groups, events = {}, {}, []
    section = None
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            if section not in SECTIONS:
                raise ValueError("第 %d 行：未知段名 [%s]" % (lineno, section))
            continue
        if section is None:
            raise ValueError("第 %d 行：内容必须位于某个段内" % lineno)
        parts = line.split()
        if section == "渠系":
            if len(parts) != 4:
                raise ValueError("第 %d 行：渠系定义应为 名称 类型 上级渠 设计流量" % lineno)
            name, ctype, parent, flow = parts
            if ctype not in CANAL_TYPES:
                raise ValueError("第 %d 行：未知渠类型 %s" % (lineno, ctype))
            if name in canals:
                raise ValueError("第 %d 行：渠 %s 重复定义" % (lineno, name))
            if not is_number(flow):
                raise ValueError("第 %d 行：设计流量不是数字" % lineno)
            canals[name] = Canal(name, ctype, None if parent in ("-", "无") else parent, float(flow))
        elif section == "轮灌组":
            if len(parts) != 3:
                raise ValueError("第 %d 行：轮灌组定义应为 名称 渠列表 需水量" % lineno)
            if not is_number(parts[2]):
                raise ValueError("第 %d 行：需水量不是数字" % lineno)
            clist = [c for c in parts[1].replace("，", ",").replace("、", ",").split(",") if c]
            groups[parts[0]] = Group(parts[0], clist, float(parts[2]))
        else:
            moment = None
            if parts and is_number(parts[0]):
                moment = float(parts[0])
                parts = parts[1:]
            if section in ("供水", "闸门"):
                actions = ("开始", "结束") if section == "供水" else ("开", "关")
                if len(parts) != 2 or parts[1] not in actions:
                    raise ValueError("第 %d 行：%s事件应为 [时刻] 名称 %s" % (lineno, section, "|".join(actions)))
            else:
                if len(parts) != 2 or not is_number(parts[1]):
                    raise ValueError("第 %d 行：水量事件应为 [时刻] 渠 实际供水量" % lineno)
                parts = [parts[0], float(parts[1])]
            events.append((moment, lineno, section, parts))
    timed = sorted((e for e in events if e[0] is not None), key=lambda e: e[0])
    untimed = [e for e in events if e[0] is None]
    return canals, groups, timed + untimed


class Engine:
    def __init__(self, canals, groups, threshold=DEFAULT_THRESHOLD):
        self.canals = canals
        self.groups = groups
        self.threshold = threshold
        self.errors = []
        self.cst = {n: CanalState() for n in canals}
        self.gst = {n: GroupState() for n in groups}
        self.children = {}
        for c in canals.values():
            if c.parent:
                self.children.setdefault(c.parent, []).append(c.name)
        self._validate_definitions()

    def _validate_definitions(self):
        for c in self.canals.values():
            if c.parent and c.parent not in self.canals:
                self.error("UNKNOWN_CANAL", "渠 %s 的上级渠 %s 不存在" % (c.name, c.parent), None)
        for g in self.groups.values():
            for c in g.canals:
                if c not in self.canals:
                    self.error("UNKNOWN_CANAL", "轮灌组 %s 引用不存在的渠 %s" % (g.name, c), None)

    def error(self, code, message, moment):
        self.errors.append({"code": code, "message": message, "time": moment})

    def descendants(self, name):
        out, stack = [], list(self.children.get(name, ()))
        while stack:
            cur = stack.pop()
            out.append(cur)
            stack.extend(self.children.get(cur, ()))
        return out

    def ancestors(self, name):
        out = []
        cur = self.canals[name].parent
        while cur and cur in self.canals:
            out.append(cur)
            cur = self.canals[cur].parent
        return out

    def run(self, events):
        for moment, lineno, kind, parts in events:
            if kind == "闸门":
                self.on_gate(parts[0], parts[1], moment)
            elif kind == "供水":
                self.on_supply(parts[0], parts[1], moment)
            else:
                self.on_water(parts[0], parts[1], moment)

    def on_gate(self, name, action, moment):
        if name not in self.canals:
            self.error("UNKNOWN_CANAL", "闸门事件引用不存在的渠 %s（%s）" % (name, action), moment)
            return
        st = self.cst[name]
        if action == "开":
            st.gate_open = True
            return
        st.gate_open = False
        for d in self.descendants(name):
            dst = self.cst[d]
            if dst.supplied_by:
                self.error("PARENT_GATE_CLOSED",
                           "上级渠 %s 闸门关闭，下级渠 %s 仍在供水（供水组：%s）"
                           % (name, d, "、".join(sorted(dst.supplied_by))), moment)

    def on_supply(self, name, action, moment):
        g = self.groups.get(name)
        if g is None:
            self.error("UNKNOWN_GROUP", "供水事件引用不存在的组 %s（%s）" % (name, action), moment)
            return
        if action == "开始":
            self._start_group(g, moment)
        else:
            self._end_group(g, moment)

    def _start_group(self, g, moment):
        gst = self.gst[g.name]
        if gst.active:
            self.error("DOUBLE_START", "组 %s 已在供水中，重复开始被拦截" % g.name, moment)
            return
        known = [c for c in g.canals if c in self.canals]
        closed = [c for c in known if not self.cst[c].gate_open]
        if closed:
            self.error("GATE_NOT_OPEN",
                       "组 %s 开始供水被拦截：闸门未开 -> %s" % (g.name, "、".join(closed)), moment)
            return
        conflicts = [(c, sorted(self.cst[c].supplied_by)) for c in known if self.cst[c].supplied_by]
        if conflicts:
            for c, others in conflicts:
                self.error("GROUP_CONFLICT",
                           "渠 %s 正由组 %s 供水，组 %s 开始供水被拦截"
                           % (c, "、".join(others), g.name), moment)
            return
        for c in known:
            for anc in self.ancestors(c):
                if not self.cst[anc].gate_open:
                    self.error("PARENT_GATE_CLOSED",
                               "组 %s 开始供水：渠 %s 的上级渠 %s 闸门处于关闭状态" % (g.name, c, anc), moment)
                    break
        gst.active = True
        gst.received = 0.0
        for c in known:
            self.cst[c].supplied_by.add(g.name)

    def _end_group(self, g, moment):
        gst = self.gst[g.name]
        if not gst.active:
            self.error("NOT_ACTIVE", "组 %s 未在供水，结束事件被忽略" % g.name, moment)
            return
        expected = g.demand + gst.adjustment
        received = gst.received
        exceeded = expected > 0 and abs(received - expected) / expected > self.threshold
        if exceeded:
            diff = expected - received
            gst.adjustment += diff
            self.error("DEMAND_MISMATCH",
                       "组 %s 结束供水：累计供水 %g 与需水 %g 偏差 %+g 超阈值 %.0f%%，"
                       "差额 %+g 已级联计入后续轮次"
                       % (g.name, received, expected, -diff, self.threshold * 100, diff), moment)
        gst.rounds.append({"received": received, "expected": expected, "exceeded": exceeded})
        gst.active = False
        gst.received = 0.0
        for c in g.canals:
            if c in self.canals:
                self.cst[c].supplied_by.discard(g.name)

    def on_water(self, name, amount, moment):
        if name not in self.canals:
            self.error("UNKNOWN_CANAL", "水量事件引用不存在的渠 %s（水量 %g）" % (name, amount), moment)
            return
        c = self.canals[name]
        st = self.cst[name]
        st.cum_water += amount
        for anc in self.ancestors(name):
            if not self.cst[anc].gate_open:
                self.error("PARENT_GATE_CLOSED",
                           "渠 %s 供水 %g 时，上级渠 %s 闸门处于关闭状态" % (name, amount, anc), moment)
                break
        for gname in sorted(st.supplied_by):
            self.gst[gname].received += amount
        if c.ctype == "支渠" and c.parent and c.parent in self.canals:
            pst = self.cst[c.parent]
            if st.cum_water > pst.cum_water + 1e-9 and not st.over_reported:
                st.over_reported = True
                marked = [name] + self.descendants(name)
                for m in marked:
                    self.cst[m].shortage = True
                self.error("OVER_SUPPLY",
                           "支渠 %s 累计供水 %g 超过上级渠 %s 实际供水 %g，已级联标记缺水：%s"
                           % (name, st.cum_water, c.parent, pst.cum_water, "、".join(marked)), moment)


def format_state(engine):
    lines = ["======== 灌区状态 ========", "[渠系]"]
    for name, c in engine.canals.items():
        st = engine.cst[name]
        supplying = "、".join(sorted(st.supplied_by)) if st.supplied_by else "-"
        lines.append("  %-8s 类型=%s 上级=%s 设计流量=%g 闸门=%s 累计供水=%g 缺水=%s 当前供水组=%s"
                     % (name, c.ctype, c.parent or "-", c.design_flow,
                        "开" if st.gate_open else "关", st.cum_water,
                        "是" if st.shortage else "否", supplying))
    lines.append("[轮灌组]")
    for name, g in engine.groups.items():
        gst = engine.gst[name]
        lines.append("  %-8s 状态=%s 需水量=%g 后续轮次调整=%+g 已完成轮次=%d"
                     % (name, "供水中" if gst.active else "空闲", g.demand,
                        gst.adjustment, len(gst.rounds)))
        for i, r in enumerate(gst.rounds, 1):
            lines.append("    轮次%d: 实收=%g 期望=%g 偏差=%+g（%s）"
                         % (i, r["received"], r["expected"],
                            r["received"] - r["expected"], "超限" if r["exceeded"] else "正常"))
    return "\n".join(lines)


def format_errors(engine):
    lines = ["======== 错误清单（%d 条） ========" % len(engine.errors)]
    for i, e in enumerate(engine.errors, 1):
        moment = "t=%g" % e["time"] if e["time"] is not None else "定义阶段"
        lines.append("  %2d. [%s] %s: %s" % (i, moment, e["code"], e["message"]))
    return "\n".join(lines)


DEMO = """\
# 层级联动与缺水样例：干渠G -> 支渠Z1/Z2 -> 斗渠D1/D2
[渠系]
干渠G 干渠 - 100
支渠Z1 支渠 干渠G 40
支渠Z2 支渠 干渠G 40
斗渠D1 斗渠 支渠Z1 15
斗渠D2 斗渠 支渠Z2 15

[轮灌组]
组1 支渠Z1,斗渠D1 500
组2 支渠Z1 300
组3 支渠Z2,斗渠D2 200

[闸门]
1 干渠G 开
2 支渠Z1 开
3 斗渠D1 开
10 干渠G 关
14 幻影渠 开
15 干渠G 开

[供水]
4 组1 开始
7 组2 开始
9 组3 开始
12 组1 结束
13 幽灵组 开始
16 组1 开始
19 组1 结束

[水量]
5 干渠G 300
6 支渠Z1 350
8 斗渠D1 60
11 支渠Z1 20
17 干渠G 650
18 支渠Z1 560
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description="灌区渠系供水层级联动校验工具")
    ap.add_argument("input", nargs="?", help="输入文件路径，缺省或 - 表示标准输入")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                    help="需水偏差阈值（默认 %.2f）" % DEFAULT_THRESHOLD)
    ap.add_argument("--demo", action="store_true", help="运行内置层级联动与缺水样例")
    args = ap.parse_args(argv)
    if args.demo:
        text = DEMO
        print("-------- 样例输入 --------")
        print(text)
    elif args.input and args.input != "-":
        with open(args.input, encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()
    try:
        canals, groups, events = parse(text)
    except ValueError as exc:
        print("输入解析失败：%s" % exc, file=sys.stderr)
        return 2
    engine = Engine(canals, groups, threshold=args.threshold)
    engine.run(events)
    print(format_state(engine))
    print(format_errors(engine))
    return 0


if __name__ == "__main__":
    sys.exit(main())
