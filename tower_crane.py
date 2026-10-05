#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tower_crane.py — 塔吊限位联动 / 大臂干涉校验工具（纯标准库，单文件）

输入脚本语法（每行一条，# 开头为注释，字段以空白分隔）：
  CRANE <名称> <x> <y> <大臂长> <初始回转角(度)> <初始幅度(米)>
  LIMIT <时刻> <塔吊> TRIP|RECOVER      # TRIP=限位触发(锁机), RECOVER=限位恢复(放行)
  OP    <时刻> SLEW|LUFF <塔吊> <目标值> # SLEW=回转目标角(度), LUFF=变幅目标幅度(米)

判定规则（自定，见文末/输出说明）：
  1) 几何：俯视 2D，每塔大臂视为从塔身出发、方向=回转角、长度=大臂长的线段；
     任意两塔大臂线段之间的最小距离 < SAFE_DIST(默认2m) 即判定为干涉，
     拦截该次操作并报告相撞双方。最小距离天然覆盖“大臂穿塔身”“臂尖擦臂”。
  2) 同时刻（相同时刻值）的多个 OP 按文件顺序为“同批次”：
     先动者成功后即成为后动者的障碍物；后动者导致碰撞则只回滚后动者，
     即“同批次内先到先得、碰撞方被拦截”。
  3) 行程：回转角合法区间 [0, 360)；幅度合法区间 [MIN_AMP(2m), 大臂长]。
     目标越界即报超行程，状态不变。
  4) 限位：TRIP 后该塔吊一切 SLEW/LUFF 被禁止；RECOVER 后立即放行。
  5) 不存在的塔吊（OP/LIMIT 引用）一律报错；状态跨操作延续。

用法：
  python3 tower_crane.py --demo          # 内置示例
  python3 tower_crane.py scenario.txt    # 读脚本文件
  cat scenario.txt | python3 tower_crane.py
"""

import sys
import math
from dataclasses import dataclass, field
from typing import List, Tuple, Dict, Optional

SAFE_DIST = 2.0      # 大臂间安全净距（米）
MIN_AMP = 2.0        # 最小允许幅度（米）
ANGLE_MAX = 360.0    # 回转角合法区间 [0, 360)


# ---------------- 几何 ----------------

def _dot(ax: float, ay: float, bx: float, by: float) -> float:
    return ax * bx + ay * by


def _point_seg_dist(px: float, py: float,
                    ax: float, ay: float, bx: float, by: float) -> float:
    """点 P 到线段 AB 的最短距离。"""
    abx, aby = bx - ax, by - ay
    t = _dot(px - ax, py - ay, abx, aby)
    if t <= 0.0:
        return math.hypot(px - ax, py - ay)
    ab2 = abx * abx + aby * aby
    if t >= ab2:
        return math.hypot(px - bx, py - by)
    t /= ab2
    qx, qy = ax + t * abx, ay + t * aby
    return math.hypot(px - qx, py - qy)


def segment_min_dist(a: Tuple[float, float], b: Tuple[float, float],
                     c: Tuple[float, float], d: Tuple[float, float]) -> float:
    """线段 AB 与线段 CD 的最短距离；相交返回 0。"""
    r, s = (b[0] - a[0], b[1] - a[1]), (d[0] - c[0], d[1] - c[1])
    den = r[0] * s[1] - r[1] * s[0]
    if abs(den) > 1e-12:
        ca = (c[0] - a[0], c[1] - a[1])
        t = (ca[0] * s[1] - ca[1] * s[0]) / den
        u = (ca[0] * r[1] - ca[1] * r[0]) / den
        if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
            return 0.0  # 两线段相交
    return min(_point_seg_dist(*c, *a, *b),
               _point_seg_dist(*d, *a, *b),
               _point_seg_dist(*a, *c, *d),
               _point_seg_dist(*b, *c, *d))


# ---------------- 数据模型 ----------------

@dataclass
class Crane:
    name: str
    x: float
    y: float
    jib: float
    angle: float          # 度，0=+X 方向，逆时针为正
    amp: float            # 幅度（小车距塔身），仅用于状态展示/变幅校验
    locked: bool = False  # 限位触发锁机

    def jib_segment(self) -> Tuple[Tuple[float, float], Tuple[float, float]]:
        rad = math.radians(self.angle)
        tip = (self.x + self.jib * math.cos(rad),
               self.y + self.jib * math.sin(rad))
        return (self.x, self.y), tip


@dataclass
class ErrorReport:
    seq: int
    time: str
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"[#{self.seq} t={self.time}] {self.kind}: {self.detail}"


@dataclass
class Simulator:
    cranes: Dict[str, Crane] = field(default_factory=dict)
    errors: List[ErrorReport] = field(default_factory=list)
    log: List[str] = field(default_factory=list)
    _seq: int = 0

    def _err(self, time: str, kind: str, detail: str) -> None:
        self._seq += 1
        self.errors.append(ErrorReport(self._seq, time, kind, detail))
        self.log.append(f"t={time} !! {kind}: {detail}")

    # ---- 事件处理 ----

    def apply_limit(self, time: str, name: str, state: str) -> None:
        crane = self.cranes.get(name)
        if crane is None:
            self._err(time, "UNKNOWN_CRANE", f"限位引用不存在的塔吊 '{name}'")
            return
        if state == "TRIP":
            crane.locked = True
            self.log.append(f"t={time} -- 塔吊 {name} 限位触发，已锁机")
        elif state == "RECOVER":
            crane.locked = False
            self.log.append(f"t={time} -- 塔吊 {name} 限位恢复，已放行")
        else:
            self._err(time, "BAD_LIMIT_STATE", f"未知限位状态 '{state}'（应为 TRIP/RECOVER）")

    def apply_op(self, time: str, op: str, name: str, target: float,
                 others_state: Optional[List[Crane]] = None) -> None:
        """others_state: 同批次已成功动作的塔吊快照（按名字取当前态即可，此处直接查表）。"""
        crane = self.cranes.get(name)
        if crane is None:
            self._err(time, "UNKNOWN_CRANE",
                      f"操作引用不存在的塔吊 '{name}' ({op} {target})")
            return
        if crane.locked:
            self._err(time, "LIMIT_LOCKED",
                      f"塔吊 {name} 限位已触发，禁止 {op}（目标 {target}）")
            return

        # 行程校验
        if op == "SLEW":
            if not (0.0 <= target < ANGLE_MAX):
                self._err(time, "OUT_OF_RANGE",
                          f"塔吊 {name} 回转目标角 {target}° 越界，"
                          f"合法区间 [0, 360)，操作拦截")
                return
            move_desc = f"回转 {crane.angle:g}° -> {target:g}°"
        elif op == "LUFF":
            if not (MIN_AMP <= target <= crane.jib):
                self._err(time, "OUT_OF_RANGE",
                          f"塔吊 {name} 变幅目标 {target:g}m 越界，"
                          f"合法区间 [{MIN_AMP:g}, {crane.jib:g}]，操作拦截")
                return
            move_desc = f"变幅 {crane.amp:g}m -> {target:g}m"
        else:
            self._err(time, "BAD_OP", f"未知操作类型 '{op}'（应为 SLEW/LUFF）")
            return

        # 试应用
        old_angle, old_amp = crane.angle, crane.amp
        if op == "SLEW":
            crane.angle = target
        else:
            crane.amp = target

        # 干涉校验：与其余所有塔吊当前大臂比对（含同批次已成功的动作）
        hit = self._collision_with(crane)
        if hit is not None:
            other, dist = hit
            crane.angle, crane.amp = old_angle, old_amp  # 回滚
            self._err(time, "COLLISION",
                      f"塔吊 {name}({move_desc}) 将与塔吊 {other.name} 大臂干涉，"
                      f"最小净距 {dist:.2f}m < {SAFE_DIST:g}m，操作拦截；相撞双方: {name} <-> {other.name}")
            return

        self.log.append(f"t={time} ok 塔吊 {name} {move_desc}")

    def _collision_with(self, crane: Crane) -> Optional[Tuple[Crane, float]]:
        a, b = crane.jib_segment()
        worst: Optional[Tuple[Crane, float]] = None
        for other in self.cranes.values():
            if other.name == crane.name:
                continue
            c, d = other.jib_segment()
            dist = segment_min_dist(a, b, c, d)
            if dist < SAFE_DIST and (worst is None or dist < worst[1]):
                worst = (other, dist)
        return worst


# ---------------- 解析 / 运行 ----------------

def run_script(lines: List[str]) -> Simulator:
    sim = Simulator()
    pending_ops: List[Tuple[str, str, str, float]] = []  # 同批次 OP（相同时刻）
    last_time: Optional[str] = None

    def flush(ops):
        # 同时刻多塔动作：按文件顺序依次执行，先成功者成为后动者障碍物
        for t, op, name, target in ops:
            sim.apply_op(t, op, name, target)

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        f = line.split()
        head = f[0].upper()
        if head == "CRANE":
            _, name, x, y, jib, ang, amp = f
            sim.cranes[name] = Crane(name, float(x), float(y), float(jib),
                                     float(ang), float(amp))
        elif head in ("OP", "LIMIT"):
            if len(f) < 4:
                sim._err(last_time or "-", "BAD_LINE", f"字段不足: {line}")
                continue
            t = f[1]
            if t != last_time:
                flush(pending_ops)
                pending_ops = []
                last_time = t
            if head == "LIMIT":
                flush(pending_ops)
                pending_ops = []
                sim.apply_limit(t, f[2], f[3].upper())
            else:
                try:
                    pending_ops.append((t, f[2].upper(), f[3], float(f[4])))
                except (IndexError, ValueError):
                    sim._err(t, "BAD_LINE", f"操作字段非法: {line}")
        else:
            sim._err(last_time or "-", "BAD_LINE", f"无法识别的指令: {line}")
    flush(pending_ops)
    return sim


DEMO = """\
# 名称  x    y   大臂  初始角  初始幅度
CRANE T1   0    0    50     0      30
CRANE T2  40    0    50    90      25
CRANE T3 100  100    40   200      20

# t=1: T1 回转 30°，大臂 (0,0)->(43.3,25) 与 T2 大臂 (40,0)->(40,50) 相交，应拦截
OP 1 SLEW T1 30

# t=2: T1 回转 90°，与 T2 大臂平行相距 40m，放行
OP 2 SLEW T1 90

# t=3: T2 变幅 60m 超出臂长 50m，报超行程
OP 3 LUFF T2 60

# t=4: 操作不存在的塔吊 T9
OP 4 SLEW T9 45

# t=5~8: T1 限位触发后操作被禁止；恢复后放行（180° 方向安全）
LIMIT 5 T1 TRIP
OP 6 SLEW T1 180
LIMIT 7 T1 RECOVER
OP 8 SLEW T1 180

# t=9: 同时刻多塔动作，按顺序“先到先得”：
#   T2 先转到 270°（臂 (40,0)->(40,-50)）成功；
#   T1 再转 320° 将穿过 T2 新位置大臂，被拦截；
#   T3 在远处互不影响，放行。
OP 9 SLEW T2 270
OP 9 SLEW T1 320
OP 9 SLEW T3 250
"""


def render(sim: Simulator) -> str:
    out = []
    out.append("=" * 64)
    out.append("操作流水")
    out.append("=" * 64)
    out.extend(sim.log)
    out.append("")
    out.append("=" * 64)
    out.append("塔吊最终状态")
    out.append("=" * 64)
    out.append(f"{'塔吊':<6}{'位置':<14}{'大臂(m)':<9}{'回转角(°)':<11}"
               f"{'幅度(m)':<9}{'限位':<6}")
    for c in sim.cranes.values():
        pos = f"({c.x:g},{c.y:g})"
        out.append(f"{c.name:<6}{pos:<14}{c.jib:<9g}{c.angle:<11g}"
                   f"{c.amp:<9g}{'锁机' if c.locked else '正常':<6}")
    out.append("")
    out.append("=" * 64)
    out.append(f"错误清单（共 {len(sim.errors)} 条）")
    out.append("=" * 64)
    if not sim.errors:
        out.append("无错误")
    for e in sim.errors:
        out.append(str(e))
    return "\n".join(out)


def main(argv: List[str]) -> int:
    if len(argv) == 2 and argv[1] == "--demo":
        lines = DEMO.splitlines()
    elif len(argv) == 2:
        with open(argv[1], "r", encoding="utf-8") as fp:
            lines = fp.readlines()
    elif len(argv) == 1:
        lines = sys.stdin.read().splitlines()
    else:
        print(__doc__)
        return 2
    sim = run_script(lines)
    print(render(sim))
    return 1 if sim.errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
