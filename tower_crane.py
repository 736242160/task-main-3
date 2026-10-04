#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tower_crane.py — 塔吊回转/变幅操作防碰撞模拟器（纯标准库，单文件）

输入（文本，行式指令，# 为注释，指令词中英文均可）：
    CRANE <名称> <x> <y> <大臂长度> <初始回转角> <初始幅度>
    OP    <时刻> <回转|变幅> <塔吊名> <目标值>
    LIMIT <时刻> <塔吊名> <触发|恢复>

输出：事件处理日志、最终塔吊状态表、错误清单。

关键机制（自定规则，理由见文末说明）：
  1. 几何判定：每塔大臂建模为“塔心 -> 塔心+幅度*方向(回转角)”的线段
     （吊载最外点即幅度处，是干涉最危险点），两大臂线段最小距离
     < 安全距离 SAFETY_DIST 即判定干涉。
  2. 行程范围：回转角 [0, 360) 度；幅度 [MIN_AMP, 大臂长度] 米。
  3. 限位：触发后该塔吊一切操作被拦截，直到同塔“恢复”后放行。
  4. 同时刻：同一时刻先处理 LIMIT（限位即时生效），再按输入顺序
     逐个处理 OP；已被接受的同刻操作立即参与后续干涉判定，被拦截
     的操作不改变姿态（保持原姿态参与后续判定）。
  5. 状态跨操作延续：所有事件按 (时刻, 类型序, 输入序) 排序后顺序
     作用于同一份塔吊状态。
"""

import math
import sys
from dataclasses import dataclass, field

# ---------------- 自定参数 ----------------
SAFETY_DIST = 2.0      # 大臂间最小安全距离（米）
MIN_AMP = 2.0          # 最小幅度（米）
ANGLE_MIN = 0.0        # 回转角下限（度，含）
ANGLE_MAX = 360.0      # 回转角上限（度，不含）

OP_TYPES = {"回转": "回转", "slew": "回转", "变幅": "变幅", "luff": "变幅"}
LIMIT_STATES = {"触发": True, "trigger": True, "on": True,
                "恢复": False, "release": False, "off": False}


# ---------------- 几何 ----------------
def _pt_seg_dist(p, a, b):
    """点到线段的最短距离。"""
    px, py = p
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _cross(o, a, b):
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _segs_intersect(p1, p2, p3, p4):
    d1 = _cross(p3, p4, p1)
    d2 = _cross(p3, p4, p2)
    d3 = _cross(p1, p2, p3)
    d4 = _cross(p1, p2, p4)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return True
    # 共线/端点接触情形
    def on_seg(a, b, p):
        return (min(a[0], b[0]) - 1e-9 <= p[0] <= max(a[0], b[0]) + 1e-9 and
                min(a[1], b[1]) - 1e-9 <= p[1] <= max(a[1], b[1]) + 1e-9)
    for d, p, a, b in ((d1, p1, p3, p4), (d2, p2, p3, p4),
                       (d3, p3, p1, p2), (d4, p4, p1, p2)):
        if abs(d) < 1e-9 and on_seg(a, b, p):
            return True
    return False


def seg_seg_dist(p1, p2, p3, p4):
    """两线段最小距离（相交为 0）。"""
    if _segs_intersect(p1, p2, p3, p4):
        return 0.0
    return min(_pt_seg_dist(p1, p3, p4), _pt_seg_dist(p2, p3, p4),
               _pt_seg_dist(p3, p1, p2), _pt_seg_dist(p4, p1, p2))


# ---------------- 模型 ----------------
@dataclass
class Crane:
    name: str
    x: float
    y: float
    jib: float       # 大臂长度（幅度上限）
    angle: float     # 当前回转角（度）
    amp: float       # 当前幅度（米）
    limited: bool = False  # 限位是否触发

    def jib_segment(self, angle=None, amp=None):
        """大臂有效线段：塔心 -> 吊载最外点（幅度处）。"""
        a = self.angle if angle is None else angle
        r = self.amp if amp is None else amp
        rad = math.radians(a)
        return (self.x, self.y), (self.x + r * math.cos(rad),
                                  self.y + r * math.sin(rad))


@dataclass
class Simulator:
    cranes: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)   # (时刻, 类别, 描述)
    log: list = field(default_factory=list)      # 处理日志

    # ---- 定义 ----
    def add_crane(self, name, x, y, jib, angle, amp):
        if name in self.cranes:
            self.errors.append(("-", "定义错误", f"塔吊 {name} 重复定义，忽略后者"))
            return
        if not (ANGLE_MIN <= angle < ANGLE_MAX):
            self.errors.append(("-", "定义错误",
                                f"塔吊 {name} 初始回转角 {angle} 超出 [{ANGLE_MIN}, {ANGLE_MAX})"))
            return
        if not (MIN_AMP <= amp <= jib):
            self.errors.append(("-", "定义错误",
                                f"塔吊 {name} 初始幅度 {amp} 超出 [{MIN_AMP}, {jib}]"))
            return
        self.cranes[name] = Crane(name, x, y, jib, angle, amp)
        self.log.append(f"定义塔吊 {name}: 位置({x},{y}) 大臂{jib}m "
                        f"回转角{angle}° 幅度{amp}m")

    # ---- 干涉判定 ----
    def find_interference(self, name, angle, amp):
        """返回与 (name 按给定姿态) 干涉的其它塔吊名列表。"""
        me = self.cranes[name]
        a1, a2 = me.jib_segment(angle, amp)
        hit = []
        for other_name, other in self.cranes.items():
            if other_name == name:
                continue
            b1, b2 = other.jib_segment()
            if seg_seg_dist(a1, a2, b1, b2) < SAFETY_DIST:
                hit.append(other_name)
        return hit

    # ---- 操作 ----
    def apply_op(self, t, op_type, name, value):
        if name not in self.cranes:
            self.errors.append((t, "引用错误", f"操作引用了不存在的塔吊 {name}"))
            return
        crane = self.cranes[name]
        if crane.limited:
            self.errors.append((t, "限位拦截",
                                f"塔吊 {name} 限位已触发，禁止{op_type}操作"))
            return
        if op_type == "回转":
            if not (ANGLE_MIN <= value < ANGLE_MAX):
                self.errors.append((t, "超行程",
                                    f"塔吊 {name} 回转目标角 {value}° 超出 "
                                    f"[{ANGLE_MIN}, {ANGLE_MAX})"))
                return
            new_angle, new_amp = value, crane.amp
        else:  # 变幅
            if not (MIN_AMP <= value <= crane.jib):
                self.errors.append((t, "超行程",
                                    f"塔吊 {name} 目标幅度 {value}m 超出 "
                                    f"[{MIN_AMP}, {crane.jib}]"))
                return
            new_angle, new_amp = crane.angle, value
        hits = self.find_interference(name, new_angle, new_amp)
        if hits:
            self.errors.append((t, "干涉拦截",
                                f"塔吊 {name} {op_type}到 "
                                f"{value} 将与 {', '.join(hits)} 大臂干涉，已拦截"))
            return
        crane.angle, crane.amp = new_angle, new_amp
        self.log.append(f"t={t}: {name} {op_type} -> {value} 接受 "
                        f"(回转角={crane.angle}°, 幅度={crane.amp}m)")

    # ---- 限位 ----
    def apply_limit(self, t, name, triggered):
        if name not in self.cranes:
            self.errors.append((t, "引用错误", f"限位引用了不存在的塔吊 {name}"))
            return
        self.cranes[name].limited = triggered
        self.log.append(f"t={t}: {name} 限位{'触发，锁定操作' if triggered else '恢复，操作放行'}")


# ---------------- 输入解析 ----------------
def parse_and_run(text):
    sim = Simulator()
    events = []  # (时刻, 类型序[LIMIT=0,OP=1], 输入序, 种类, 参数)
    seq = 0
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        tok = line.split()
        head = tok[0].upper()
        try:
            if head in ("CRANE", "塔吊"):
                name = tok[1]
                x, y, jib, angle, amp = map(float, tok[2:7])
                sim.add_crane(name, x, y, jib, angle, amp)
            elif head in ("OP", "操作"):
                t = float(tok[1])
                op_type = OP_TYPES[tok[2].lower() if tok[2].isascii() else tok[2]]
                events.append((t, 1, seq, "OP",
                               (op_type, tok[3], float(tok[4]))))
                seq += 1
            elif head in ("LIMIT", "限位"):
                t = float(tok[1])
                key = tok[3].lower() if tok[3].isascii() else tok[3]
                events.append((t, 0, seq, "LIMIT", (tok[2], LIMIT_STATES[key])))
                seq += 1
            else:
                sim.errors.append(("-", "解析错误", f"第{lineno}行: 未知指令 {tok[0]}"))
        except (IndexError, ValueError, KeyError) as e:
            sim.errors.append(("-", "解析错误", f"第{lineno}行: 格式错误 ({e})"))

    for t, _, _, kind, args in sorted(events, key=lambda e: (e[0], e[1], e[2])):
        if kind == "LIMIT":
            sim.apply_limit(t, args[0], args[1])
        else:
            sim.apply_op(t, args[0], args[1], args[2])
    return sim


# ---------------- 输出 ----------------
def report(sim):
    print("== 处理日志 ==")
    for line in sim.log:
        print("  " + line)
    print("\n== 最终塔吊状态 ==")
    print(f"  {'名称':<8}{'位置':<16}{'大臂(m)':<9}{'回转角(°)':<11}"
          f"{'幅度(m)':<9}{'限位'}")
    for c in sim.cranes.values():
        print(f"  {c.name:<8}({c.x:g},{c.y:g}){'':<10}{c.jib:<9g}"
              f"{c.angle:<11g}{c.amp:<9g}{'触发(锁定)' if c.limited else '正常'}")
    print("\n== 错误清单 ==")
    if not sim.errors:
        print("  无")
    for t, kind, msg in sim.errors:
        print(f"  [t={t}] [{kind}] {msg}")


def main(argv):
    if len(argv) > 1:
        with open(argv[1], encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()
    report(parse_and_run(text))


if __name__ == "__main__":
    main(sys.argv)
