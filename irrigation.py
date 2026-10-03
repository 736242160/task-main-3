#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""灌区渠系供水层级联动监测工具（纯 Python 标准库，单文件）。

输入为行式事件流（文件或标准输入），`#` 开头为注释，空行忽略：

    渠   <名称> <干渠|支渠|斗渠> <上级渠名|-> <设计流量>
    组   <名称> <渠1,渠2,...> <需水量>
    阈值 <比例>                      # 可选，默认 0.10（需水量的 10%）
    闸门 <渠名> <开|关>
    供水 <组名> <开始|结束>
    水量 <渠名> <实际供水量>

所有事件按行顺序处理，状态跨行（跨流）延续。处理完毕输出：
灌区状态（渠系 + 轮灌组）与错误清单。

联动规则：
  1. 组开始供水时其渠闸门未开 -> 报告并拦截（该次开始无效）；
  2. 同一渠被不同组同时供水 -> 报告并定位（渠名 + 双方组名），拦截后者；
  3. 支渠累计供水量超其上级干渠实际供水量 -> 报告，并级联标记该支渠
     及其全部下级渠、相关供水组为缺水；
  4. 干渠（上级渠）闸门关闭时下级渠供水 -> 报告（关闸瞬间与后续水量均查）；
  5. 组结束供水时累计供水量与需水量之差超阈值 -> 报告，并将差额级联
     结转到该组后续轮次的需水量；
  6. 闸门引用不存在渠 -> 报告；
  7. 供水引用不存在组 -> 报告。

用法：
    python3 irrigation.py 事件文件
    python3 irrigation.py < 事件文件
    python3 irrigation.py --demo     # 运行内置层级联动与缺水样例
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

CANAL_TYPES = ("干渠", "支渠", "斗渠")
DEFAULT_THRESHOLD = 0.10


@dataclass
class Canal:
    name: str
    ctype: str
    parent: str | None
    design_flow: float
    gate_open: bool = False
    water: float = 0.0                 # 累计实际供水量
    shortage: bool = False             # 缺水标记（可级联）
    active_groups: set = field(default_factory=set)  # 正在供水中的组


@dataclass
class Group:
    name: str
    canals: list
    demand: float                      # 单轮需水量
    supplying: bool = False
    round_no: int = 1
    carry: float = 0.0                 # 历史轮次结转的欠水量（级联调整）
    received: float = 0.0              # 本轮累计供水
    shortage: bool = False

    @property
    def adjusted_demand(self) -> float:
        return self.demand + self.carry


class IrrigationSystem:
    def __init__(self) -> None:
        self.canals: dict[str, Canal] = {}
        self.groups: dict[str, Group] = {}
        self.threshold = DEFAULT_THRESHOLD
        self.errors: list[str] = []

    # ---------- 工具 ----------
    def _err(self, lineno: int, msg: str) -> None:
        self.errors.append(f"[行{lineno}] {msg}")

    def _children_of(self, name: str) -> list[str]:
        return [c.name for c in self.canals.values() if c.parent == name]

    def _descendants(self, name: str) -> list[str]:
        result, stack = [], [name]
        while stack:
            cur = stack.pop()
            for child in self._children_of(cur):
                result.append(child)
                stack.append(child)
        return result

    def _ancestors(self, name: str) -> list[str]:
        result, cur = [], self.canals[name].parent
        while cur:
            result.append(cur)
            cur = self.canals[cur].parent if cur in self.canals else None
        return result

    # ---------- 事件处理 ----------
    def process_line(self, lineno: int, raw: str) -> None:
        line = raw.strip()
        if not line or line.startswith("#"):
            return
        parts = line.split()
        cmd = parts[0]
        try:
            if cmd == "渠":
                self._def_canal(lineno, parts)
            elif cmd == "组":
                self._def_group(lineno, parts)
            elif cmd == "阈值":
                self.threshold = float(parts[1])
            elif cmd == "闸门":
                self._gate(lineno, parts)
            elif cmd == "供水":
                self._supply(lineno, parts)
            elif cmd == "水量":
                self._water(lineno, parts)
            else:
                self._err(lineno, f"未知指令: {cmd}")
        except (IndexError, ValueError):
            self._err(lineno, f"指令格式错误: {line}")

    def _def_canal(self, lineno: int, p: list) -> None:
        name, ctype, parent, flow = p[1], p[2], p[3], float(p[4])
        if name in self.canals:
            self._err(lineno, f"渠重复定义: {name}")
            return
        if ctype not in CANAL_TYPES:
            self._err(lineno, f"渠 {name} 类型非法: {ctype}（应为 干渠/支渠/斗渠）")
            return
        parent = None if parent == "-" else parent
        if parent and parent not in self.canals:
            self._err(lineno, f"渠 {name} 的上级渠不存在: {parent}")
            return
        self.canals[name] = Canal(name, ctype, parent, flow)

    def _def_group(self, lineno: int, p: list) -> None:
        name, canal_str, demand = p[1], p[2].replace("，", ","), float(p[3])
        if name in self.groups:
            self._err(lineno, f"组重复定义: {name}")
            return
        canals, missing = [], []
        for c in canal_str.split(","):
            (canals if c in self.canals else missing).append(c)
        for c in missing:
            self._err(lineno, f"组 {name} 引用了不存在的渠: {c}")
        self.groups[name] = Group(name, canals, demand)

    def _gate(self, lineno: int, p: list) -> None:
        name, action = p[1], p[2]
        if name not in self.canals:                       # 规则 6
            self._err(lineno, f"闸门操作引用了不存在的渠: {name}")
            return
        if action not in ("开", "关"):
            self._err(lineno, f"闸门状态非法: {action}（应为 开/关）")
            return
        canal = self.canals[name]
        if action == "关" and canal.gate_open:
            # 规则 4（关闸瞬间）：下级渠仍有组在供水
            for desc in self._descendants(name):
                if self.canals[desc].active_groups:
                    groups = "、".join(sorted(self.canals[desc].active_groups))
                    self._err(lineno, f"干渠闸门关闭时下级渠供水: 关闭 {name} 时，"
                                      f"下级渠 {desc} 正被组 {groups} 供水")
        canal.gate_open = (action == "开")

    def _supply(self, lineno: int, p: list) -> None:
        name, action = p[1], p[2]
        if name not in self.groups:                       # 规则 7
            self._err(lineno, f"供水操作引用了不存在的组: {name}")
            return
        group = self.groups[name]
        if action == "开始":
            self._supply_start(lineno, group)
        elif action == "结束":
            self._supply_end(lineno, group)
        else:
            self._err(lineno, f"供水状态非法: {action}（应为 开始/结束）")

    def _supply_start(self, lineno: int, group: Group) -> None:
        if group.supplying:
            self._err(lineno, f"组 {group.name} 重复开始供水（第{group.round_no}轮未结束）")
            return
        # 规则 1：闸门未开 -> 报告并拦截
        closed = [c for c in group.canals if not self.canals[c].gate_open]
        if closed:
            self._err(lineno, f"组 {group.name} 开始供水被拦截: 闸门未开 -> "
                              f"{'、'.join(closed)}")
            return
        # 规则 2：同一渠不同组同时供水 -> 报告并定位，拦截后者
        blocked = False
        for c in group.canals:
            others = self.canals[c].active_groups
            if others:
                self._err(lineno, f"渠 {c} 供水冲突: 组 {'、'.join(sorted(others))} "
                                  f"正在供水，组 {group.name} 的开始请求被拦截")
                blocked = True
        if blocked:
            return
        group.supplying = True
        group.received = 0.0
        for c in group.canals:
            self.canals[c].active_groups.add(group.name)

    def _supply_end(self, lineno: int, group: Group) -> None:
        if not group.supplying:
            self._err(lineno, f"组 {group.name} 未在供水，无法结束")
            return
        for c in group.canals:
            self.canals[c].active_groups.discard(group.name)
        group.supplying = False
        # 规则 5：累计供水 vs 调整后续水量，超阈值 -> 报告并级联结转
        need = group.adjusted_demand
        diff = group.received - need
        if abs(diff) > self.threshold * need:
            kind = "欠水" if diff < 0 else "超供"
            self._err(lineno, f"组 {group.name} 第{group.round_no}轮结束: 累计供水 "
                              f"{group.received:g} 与需水 {need:g} 相差 {diff:+g}，"
                              f"超过阈值 {self.threshold:.0%}（{kind}）")
            if diff < 0:
                group.carry += -diff
                self._err(lineno, f"级联调整: 组 {group.name} 第{group.round_no + 1}轮"
                                  f"需水量调整为 {group.adjusted_demand:g}"
                                  f"（结转欠水 {-diff:g}）")
        group.round_no += 1

    def _water(self, lineno: int, p: list) -> None:
        name, amount = p[1], float(p[2])
        if name not in self.canals:
            self._err(lineno, f"水量记录引用了不存在的渠: {name}")
            return
        canal = self.canals[name]
        canal.water += amount
        for gname in canal.active_groups:
            self.groups[gname].received += amount
        # 规则 4（供水过程中）：上级渠闸门处于关闭状态
        for anc in self._ancestors(name):
            if not self.canals[anc].gate_open:
                self._err(lineno, f"干渠闸门关闭时下级渠供水: {anc} 闸门已关，"
                                  f"下级渠 {name} 仍供水 {amount:g}")
                break
        # 规则 3：支渠累计供水超上级干渠实际供水 -> 级联标记缺水
        if canal.ctype == "支渠" and canal.parent:
            parent = self.canals[canal.parent]
            if canal.water > parent.water:
                self._err(lineno, f"支渠 {name} 累计供水 {canal.water:g} 超过上级 "
                                  f"{parent.name} 实际供水 {parent.water:g}")
                marked = [name] + self._descendants(name)
                hit_groups = set()
                for c in marked:
                    self.canals[c].shortage = True
                    hit_groups |= self.canals[c].active_groups
                for gname in hit_groups:
                    self.groups[gname].shortage = True
                tail = f"，涉及供水组: {'、'.join(sorted(hit_groups))}" if hit_groups else ""
                self._err(lineno, f"级联标记缺水: {'、'.join(marked)}{tail}")

    # ---------- 输出 ----------
    def render(self) -> str:
        out = ["=" * 46, "灌区状态", "=" * 46, "【渠系】"]
        for c in self.canals.values():
            active = "、".join(sorted(c.active_groups)) or "-"
            out.append(
                f"  {c.name}({c.ctype}) 上级:{c.parent or '-'} "
                f"设计流量:{c.design_flow:g} 闸门:{'开' if c.gate_open else '关'} "
                f"累计供水:{c.water:g} 缺水:{'是' if c.shortage else '否'} "
                f"供水中组:{active}")
        out.append("【轮灌组】")
        for g in self.groups.values():
            if g.supplying:
                state = f"第{g.round_no}轮供水中"
            elif g.round_no == 1:
                state = "待开始（第1轮）"
            else:
                state = f"已完成至第{g.round_no - 1}轮"
            out.append(
                f"  {g.name} 渠:{'、'.join(g.canals) or '-'} 状态:{state} "
                f"需水:{g.demand:g}(调整后:{g.adjusted_demand:g}) "
                f"本轮累计:{g.received:g} 缺水:{'是' if g.shortage else '否'}")
        out += ["=" * 46, f"错误清单（共 {len(self.errors)} 条）", "=" * 46]
        out += [f"  {i}. {e}" for i, e in enumerate(self.errors, 1)] or ["  （无）"]
        return "\n".join(out)


DEMO = """\
# ===== 渠系定义：总干渠 -> 东干渠 -> 东一/东二支渠 -> 东一斗渠 =====
渠 总干渠 干渠 - 100
渠 东干渠 干渠 总干渠 60
渠 东一支渠 支渠 东干渠 25
渠 东一斗渠 斗渠 东一支渠 10
渠 东二支渠 支渠 东干渠 20
# ===== 轮灌组定义 =====
组 甲组 东一支渠,东一斗渠 80
组 乙组 东二支渠 50
组 丙组 东一斗渠 30
阈值 0.1
# ===== 事件流 =====
闸门 总干渠 开
闸门 东干渠 开
水量 总干渠 50
供水 乙组 开始
# ^ 乙组被拦截：东二支渠闸门未开（规则1）
水量 东干渠 30
闸门 东一支渠 开
闸门 东一斗渠 开
供水 甲组 开始
供水 丙组 开始
# ^ 丙组被拦截：东一斗渠正被甲组供水（规则2，定位渠与双方组）
水量 东一支渠 40
# ^ 东一支渠累计40 > 东干渠实际30：级联标记东一支渠、东一斗渠及甲组缺水（规则3）
水量 东一斗渠 20
闸门 东干渠 关
# ^ 关闸瞬间下级东一支渠/东一斗渠仍在供水（规则4）
水量 东二支渠 10
# ^ 上级东干渠闸门已关，下级仍供水（规则4）
供水 甲组 结束
# ^ 甲组本轮累计60 vs 需水80，欠20 > 阈值10%：结转至第2轮需水100（规则5）
闸门 西干渠 开
# ^ 闸门引用不存在的渠（规则6）
供水 丁组 开始
# ^ 供水引用不存在的组（规则7）
"""


def main(argv: list[str]) -> None:
    if "--demo" in argv:
        print("【内置样例：层级联动与缺水】")
        print(DEMO)
        lines = DEMO.splitlines()
    elif len(argv) > 1:
        with open(argv[1], encoding="utf-8") as f:
            lines = f.read().splitlines()
    else:
        lines = sys.stdin.read().splitlines()
    system = IrrigationSystem()
    for lineno, line in enumerate(lines, 1):
        system.process_line(lineno, line)
    print(system.render())


if __name__ == "__main__":
    main(sys.argv)
