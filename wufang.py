#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
变电站倒闸操作"五防"校验工具（纯 Python 标准库，单文件）

用法：
    python3 wufang.py 输入文件        # 从文件读取
    python3 wufang.py < 输入文件      # 从标准输入读取

============================ 输入格式 ============================
分三个小节，行内 # 之后为注释，空行忽略：

[设备]
# 名称  类型(开关/刀闸/接地线/保护)  初始状态(合/分/挂/拆/投入/退出)  [组]
101开关    开关    分    一线
1011刀闸   刀闸    分    一线
101接地线  接地线  拆    一线
101保护    保护    投入  一线
# 组可省略，省略时按"名称去掉类型后缀"归组；同组设备参与相互闭锁。

[操作票]
票 一线停电票 -            # 票 名称 前置票(-/无 表示无前置，多个用逗号分隔)
步骤 101开关 分            # 步骤 设备名 目标状态
步骤 1011刀闸 分
步骤 101接地线 挂
票 一线送电票 一线停电票
步骤 101接地线 拆
步骤 1011刀闸 合
步骤 101开关 合

[操作流]
一线停电票 1 分            # 票名 步骤号(从1开始) 目标状态
一线停电票 2 分

============================ 校验规则 ============================
1. 操作票内步骤必须按顺序执行：越序拦截并指出应执行第几步；重复执行同一步报错。
2. 前置票未全部完成，本票禁止执行（跨票依赖）。
3. 操作流目标状态必须与票面步骤目标一致，否则报"目标与票面不符"。
4. 设备状态闭锁（五防，按同组设备判定，状态随操作级联更新、跨票延续）：
   - 合开关：同组接地线须已拆、保护须已投入、刀闸须已合；
   - 合刀闸：同组接地线须已拆、开关须已分（防带负荷合刀闸）；
   - 分刀闸：同组开关须已分（防带负荷拉刀闸）；
   - 挂接地线：同组开关、刀闸须已分（防带电挂接地线）；
   - 退出保护：同组开关须已分。
5. 引用不存在的票/设备、设备已处于目标状态，均报错。
6. 单步失败不推进该票步骤指针，后续步骤继续校验，错误全部汇总输出。
"""

import re
import sys
from dataclasses import dataclass, field

# 各设备类型允许的状态
TYPE_STATES = {
    "开关": ("合", "分"),
    "刀闸": ("合", "分"),
    "接地线": ("挂", "拆"),
    "保护": ("投入", "退出"),
}
# 用于省略组名时从设备名推导组（长的在前，先匹配）
STRIP_WORDS = ("接地线", "开关", "刀闸", "保护")


@dataclass
class Device:
    name: str
    dtype: str
    state: str
    group: str


@dataclass
class Step:
    device: str
    target: str
    lineno: int


@dataclass
class Ticket:
    name: str
    prereqs: list
    steps: list = field(default_factory=list)
    next_step: int = 1  # 下一个应执行的步骤号（1 起）

    def completed(self):
        return self.next_step > len(self.steps)


@dataclass
class FlowOp:
    ticket: str
    step_no: int
    target: str
    lineno: int


def default_group(name):
    for word in STRIP_WORDS:
        if name.endswith(word) and len(name) > len(word):
            return name[: -len(word)]
    return name


def parse(text):
    devices, tickets, flow, errors = {}, {}, [], []
    section, current = None, None
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            if section not in ("设备", "操作票", "操作流"):
                errors.append(f"第{lineno}行：未知小节[{section}]")
            current = None
            continue
        parts = line.split()
        if section == "设备":
            if len(parts) not in (3, 4):
                errors.append(f"第{lineno}行：设备定义应为 名称 类型 初始状态 [组]")
                continue
            name, dtype, state = parts[0], parts[1], parts[2]
            group = parts[3] if len(parts) == 4 else default_group(name)
            if name in devices:
                errors.append(f"第{lineno}行：设备[{name}]重复定义")
            elif dtype not in TYPE_STATES:
                errors.append(f"第{lineno}行：未知设备类型[{dtype}]")
            elif state not in TYPE_STATES[dtype]:
                errors.append(f"第{lineno}行：类型[{dtype}]不允许初始状态[{state}]")
            else:
                devices[name] = Device(name, dtype, state, group)
        elif section == "操作票":
            if parts[0] == "票":
                if len(parts) != 3:
                    errors.append(f"第{lineno}行：票定义应为 票 名称 前置票")
                    continue
                _, tname, pre = parts
                if tname in tickets:
                    errors.append(f"第{lineno}行：操作票[{tname}]重复定义")
                    continue
                prereqs = [] if pre in ("-", "无") else [p for p in re.split(r"[,，、]", pre) if p]
                tickets[tname] = Ticket(tname, prereqs)
                current = tickets[tname]
            elif parts[0] == "步骤":
                if current is None:
                    errors.append(f"第{lineno}行：步骤必须先出现在某张票内")
                elif len(parts) != 3:
                    errors.append(f"第{lineno}行：步骤定义应为 步骤 设备名 目标状态")
                else:
                    current.steps.append(Step(parts[1], parts[2], lineno))
            else:
                errors.append(f"第{lineno}行：操作票小节内无法识别的行：{line}")
        elif section == "操作流":
            if len(parts) != 3 or not parts[1].isdigit():
                errors.append(f"第{lineno}行：操作流应为 票名 步骤号 目标状态")
                continue
            flow.append(FlowOp(parts[0], int(parts[1]), parts[2], lineno))
        else:
            errors.append(f"第{lineno}行：内容必须位于 [设备]/[操作票]/[操作流] 小节内")
    for t in tickets.values():
        if not t.steps:
            errors.append(f"操作票[{t.name}]没有任何步骤")
    return devices, tickets, flow, errors


def check_interlock(dev, target, devices):
    """五防闭锁校验，返回 None 表示允许，否则返回闭锁原因。"""
    group = [d for d in devices.values() if d.group == dev.group]
    of_type = lambda t: [d for d in group if d.dtype == t]
    if dev.dtype == "开关" and target == "合":
        for g in of_type("接地线"):
            if g.state == "挂":
                return f"五防闭锁：接地线[{g.name}]未拆除，禁止合闸"
        for p in of_type("保护"):
            if p.state != "投入":
                return f"五防闭锁：保护[{p.name}]未投入，禁止合闸"
        for k in of_type("刀闸"):
            if k.state != "合":
                return f"五防闭锁：刀闸[{k.name}]未合上，禁止合开关"
    elif dev.dtype == "刀闸":
        if target == "合":
            for g in of_type("接地线"):
                if g.state == "挂":
                    return f"五防闭锁：接地线[{g.name}]未拆除，禁止合刀闸"
            for b in of_type("开关"):
                if b.state == "合":
                    return f"五防闭锁：开关[{b.name}]在合位，禁止带负荷合刀闸"
        elif target == "分":
            for b in of_type("开关"):
                if b.state == "合":
                    return f"五防闭锁：开关[{b.name}]在合位，禁止带负荷拉刀闸"
    elif dev.dtype == "接地线" and target == "挂":
        for d in group:
            if d.dtype in ("开关", "刀闸") and d.state == "合":
                return f"五防闭锁：{d.dtype}[{d.name}]在合位，禁止带电挂接地线"
    elif dev.dtype == "保护" and target == "退出":
        for b in of_type("开关"):
            if b.state == "合":
                return f"五防闭锁：开关[{b.name}]运行中，禁止退出保护"
    return None


def run(devices, tickets, flow):
    results, errors = [], []
    for idx, op in enumerate(flow, 1):
        label = f"操作{idx}（票[{op.ticket}] 第{op.step_no}步 目标[{op.target}]，输入第{op.lineno}行）"
        t = tickets.get(op.ticket)
        if t is None:
            errors.append(f"{label} -> 操作票[{op.ticket}]不存在")
            continue
        blocked = None
        for pre in t.prereqs:
            pt = tickets.get(pre)
            if pt is None:
                blocked = f"前置票[{pre}]不存在"
                break
            if not pt.completed():
                blocked = f"前置票[{pre}]未完成（已执行{pt.next_step - 1}/{len(pt.steps)}步），禁止执行本票"
                break
        if blocked:
            errors.append(f"{label} -> {blocked}")
            continue
        if not 1 <= op.step_no <= len(t.steps):
            errors.append(f"{label} -> 步骤号越界（本票共{len(t.steps)}步）")
            continue
        if op.step_no < t.next_step:
            errors.append(f"{label} -> 重复操作：第{op.step_no}步已执行过")
            continue
        if op.step_no > t.next_step:
            errors.append(f"{label} -> 越序操作：本票应先执行第{t.next_step}步")
            continue
        step = t.steps[op.step_no - 1]
        if op.target != step.target:
            errors.append(f"{label} -> 操作目标与票面不符：票面要求[{step.target}]，实际下达[{op.target}]")
            continue
        dev = devices.get(step.device)
        if dev is None:
            errors.append(f"{label} -> 设备[{step.device}]未定义（步骤定义于输入第{step.lineno}行）")
            continue
        if dev.state == op.target:
            errors.append(f"{label} -> 设备[{dev.name}]已处于[{op.target}]，操作无效")
            continue
        reason = check_interlock(dev, op.target, devices)
        if reason:
            errors.append(f"{label} -> {reason}")
            continue
        old = dev.state
        dev.state = op.target  # 状态级联更新，后续步骤/后续票均可见
        t.next_step += 1
        results.append(f"{label} -> 成功：{dev.name} {old} -> {op.target}")
    return results, errors


def main():
    text = open(sys.argv[1], encoding="utf-8").read() if len(sys.argv) > 1 else sys.stdin.read()
    devices, tickets, flow, parse_errors = parse(text)
    if parse_errors:
        print("==== 输入解析错误 ====")
        for e in parse_errors:
            print("  " + e)
        sys.exit(2)

    results, errors = run(devices, tickets, flow)

    print("==== 执行过程 ====")
    for line in results:
        print("  [成功] " + line)
    for line in errors:
        print("  [失败] " + line)

    print("\n==== 操作票完成情况 ====")
    for t in tickets.values():
        mark = "已完成" if t.completed() else f"未完成（{t.next_step - 1}/{len(t.steps)}步）"
        pre = "、".join(t.prereqs) if t.prereqs else "无"
        print(f"  {t.name}（前置票：{pre}）：{mark}")

    print("\n==== 设备最终状态 ====")
    for name in sorted(devices, key=lambda n: (devices[n].group, n)):
        d = devices[name]
        print(f"  [{d.group}] {d.name}（{d.dtype}）：{d.state}")

    print("\n==== 错误清单 ====")
    if errors:
        for i, e in enumerate(errors, 1):
            print(f"  错误{i}: {e}")
    else:
        print("  无")

    print(f"\n==== 汇总 ====\n  成功 {len(results)} 项，失败 {len(errors)} 项")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
