#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
变电站倒闸操作"五防"校验工具（纯 Python 标准库，单文件）

用法:
    python3 wufang.py input.json
    cat input.json | python3 wufang.py          # 从标准输入读取

输入 JSON 格式:
{
  "devices": [ {"name": "DL101", "type": "开关", "state": "合"}, ... ],
  "tickets": [ {"name": "停电票", "prerequisites": ["其他票"], 
                "steps": [ {"device": "DL101", "target": "分"}, ... ]}, ... ],
  "operations": [ {"ticket": "停电票", "step": 1, "target": "分"}, ... ]
}

设备类型与合法状态:
    开关: 合/分   刀闸: 合/分   接地线: 挂/分(已拆除)   保护: 投入/退出

校验规则:
    1. 操作须按票内步骤顺序执行，越序拦截并报告应执行的第几步
    2. 重复操作同一步骤要拦截
    3. 前置票未全部完成，该票禁止执行（跨票依赖）
    4. 设备状态闭锁: 任一接地线未拆除(挂)禁止合开关/刀闸; 保护未投入禁止合开关
    5. 操作目标须与票面步骤一致，且与设备当前状态相符（不得重复到同一状态）
    6. 引用不存在的票/步骤/设备要报告
    7. 设备状态全局唯一，操作成功后级联更新，跨票状态延续
输出: 每条操作的执行结果、设备最终状态、错误清单（含操作序号定位）。
"""
import json
import sys

VALID_STATES = {
    "开关": {"合", "分"},
    "刀闸": {"合", "分"},
    "接地线": {"挂", "分"},   # 分 = 已拆除
    "保护": {"投入", "退出"},
}


class Simulator:
    def __init__(self, data):
        self.errors = []          # 装载期错误 + 运行期错误（统一进错误清单）
        self.results = []         # 每条操作的执行结果
        self.devices = {}         # name -> {"type":..., "state":...}
        self.tickets = {}         # name -> {"steps":[...], "prerequisites":[...]}
        self.progress = {}        # ticket -> 已完成步骤数（下一步 = progress+1）
        self._load(data)

    # ---------- 输入装载与静态校验 ----------
    def _load(self, data):
        for i, d in enumerate(data.get("devices", []), 1):
            where = f"设备定义#{i}"
            name, dtype, state = d.get("name"), d.get("type"), d.get("state")
            if not name:
                self.errors.append(f"{where}: 缺少设备名称")
                continue
            if name in self.devices:
                self.errors.append(f"{where}: 设备 '{name}' 重复定义")
                continue
            if dtype not in VALID_STATES:
                self.errors.append(f"{where}: 设备 '{name}' 类型 '{dtype}' 非法，"
                                   f"应为 { '/'.join(VALID_STATES) }")
                continue
            if state not in VALID_STATES[dtype]:
                self.errors.append(f"{where}: 设备 '{name}'({dtype}) 初始状态 '{state}' 非法，"
                                   f"应为 {'/'.join(sorted(VALID_STATES[dtype]))}")
                continue
            self.devices[name] = {"type": dtype, "state": state}

        for i, t in enumerate(data.get("tickets", []), 1):
            where = f"操作票定义#{i}"
            name = t.get("name")
            if not name:
                self.errors.append(f"{where}: 缺少票名")
                continue
            if name in self.tickets:
                self.errors.append(f"{where}: 操作票 '{name}' 重复定义")
                continue
            steps = t.get("steps", [])
            if not steps:
                self.errors.append(f"{where}: 操作票 '{name}' 步骤序列为空")
                continue
            self.tickets[name] = {
                "steps": steps,
                "prerequisites": list(t.get("prerequisites", [])),
            }
            self.progress[name] = 0

        # 票内步骤与前置票的引用校验（所有票登记后再查）
        for name, t in self.tickets.items():
            for pre in t["prerequisites"]:
                if pre not in self.tickets:
                    self.errors.append(f"操作票 '{name}': 前置票 '{pre}' 不存在")
            for j, s in enumerate(t["steps"], 1):
                dev, target = s.get("device"), s.get("target")
                where = f"操作票 '{name}' 第{j}步"
                if dev not in self.devices:
                    self.errors.append(f"{where}: 引用了不存在的设备 '{dev}'")
                    continue
                dtype = self.devices[dev]["type"]
                if target not in VALID_STATES[dtype]:
                    self.errors.append(f"{where}: 目标状态 '{target}' 对设备 "
                                       f"'{dev}'({dtype}) 非法")

    # ---------- 闭锁逻辑 ----------
    def _interlock_errors(self, dev_name, target):
        """合闸闭锁: 接地线未拆除不能合闸; 保护未投入不能合开关。"""
        errs = []
        if target != "合":
            return errs
        dtype = self.devices[dev_name]["type"]
        if dtype not in ("开关", "刀闸"):
            return errs
        grounded = [n for n, d in self.devices.items()
                    if d["type"] == "接地线" and d["state"] == "挂"]
        if grounded:
            errs.append("防误闭锁: 接地线 " + "、".join(grounded) +
                        " 未拆除(状态=挂)，禁止合闸")
        if dtype == "开关":
            off = [n for n, d in self.devices.items()
                   if d["type"] == "保护" and d["state"] != "投入"]
            if off:
                errs.append("防误闭锁: 保护 " + "、".join(off) +
                            " 未投入，禁止合开关")
        return errs

    # ---------- 单条操作执行 ----------
    def execute(self, idx, op):
        tname, step_no, target = op.get("ticket"), op.get("step"), op.get("target")
        where = f"操作#{idx} [票='{tname}' 步骤={step_no} 目标='{target}']"

        def fail(msgs):
            for m in msgs:
                self.errors.append(f"{where}: {m}")
            self.results.append((idx, False, where, msgs))

        # 1. 票存在性
        if tname not in self.tickets:
            fail([f"引用了不存在的操作票 '{tname}'"])
            return
        ticket = self.tickets[tname]
        steps = ticket["steps"]

        # 2. 步骤号合法性
        if not isinstance(step_no, int) or not (1 <= step_no <= len(steps)):
            fail([f"步骤号 {step_no} 超出范围(票 '{tname}' 共 {len(steps)} 步)"])
            return

        # 3. 重复操作
        if step_no <= self.progress[tname]:
            fail([f"重复操作: 票 '{tname}' 第{step_no}步已执行过"])
            return

        # 4. 跨票前置依赖
        undone = [p for p in ticket["prerequisites"]
                  if self.progress.get(p, 0) < len(self.tickets.get(p, {"steps": []})["steps"])]
        if undone:
            fail(["前置票未完成，禁止执行本票: " +
                  "、".join(f"'{p}'(已完成{self.progress.get(p, 0)}/"
                            f"{len(self.tickets[p]['steps'])}步)" for p in undone)])
            return

        # 5. 步骤顺序（越序拦截并报告应执行的第几步）
        expected = self.progress[tname] + 1
        if step_no != expected:
            fail([f"越序操作: 票 '{tname}' 应执行第{expected}步，"
                  f"实际请求第{step_no}步"])
            return

        step = steps[step_no - 1]
        dev_name = step["device"]
        dev = self.devices[dev_name]

        # 6. 操作目标与票面不符
        if target != step["target"]:
            fail([f"操作目标 '{target}' 与票面第{step_no}步要求 "
                  f"'{step['target']}' 不符"])
            return

        # 7. 操作目标与设备当前状态不符
        if dev["state"] == target:
            fail([f"设备 '{dev_name}'({dev['type']}) 当前状态已为 "
                  f"'{target}'，操作目标与设备状态不符"])
            return

        # 8. 设备状态闭锁（五防）
        interlocks = self._interlock_errors(dev_name, target)
        if interlocks:
            fail(interlocks)
            return

        # 9. 执行成功: 级联更新设备状态（全局状态，跨票延续）
        old = dev["state"]
        dev["state"] = target
        self.progress[tname] += 1
        self.results.append((idx, True, where,
                             [f"设备 '{dev_name}'({dev['type']}): {old} -> {target}"]))

    # ---------- 报告 ----------
    def report(self, out):
        print("=" * 60, file=out)
        print("一、执行结果", file=out)
        print("=" * 60, file=out)
        for idx, ok, where, msgs in self.results:
            tag = "成功" if ok else "失败"
            print(f"[{tag}] {where}", file=out)
            for m in msgs:
                print(f"      - {m}", file=out)

        print("\n" + "=" * 60, file=out)
        print("二、设备最终状态", file=out)
        print("=" * 60, file=out)
        for name, d in self.devices.items():
            print(f"  {name:<10} 类型={d['type']:<4} 状态={d['state']}", file=out)

        print("\n" + "=" * 60, file=out)
        print(f"三、错误清单（共 {len(self.errors)} 条）", file=out)
        print("=" * 60, file=out)
        if not self.errors:
            print("  无错误", file=out)
        for i, e in enumerate(self.errors, 1):
            print(f"  错误{i}: {e}", file=out)


def main(argv):
    try:
        if len(argv) > 1:
            with open(argv[1], encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = json.load(sys.stdin)
    except (OSError, json.JSONDecodeError) as e:
        print(f"输入读取/解析失败: {e}", file=sys.stderr)
        return 2

    sim = Simulator(data)
    for idx, op in enumerate(data.get("operations", []), 1):
        sim.execute(idx, op)
    sim.report(sys.stdout)
    return 1 if sim.errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
