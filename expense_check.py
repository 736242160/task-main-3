#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
差旅报销校验工具（纯 Python 标准库，单文件）

输入格式（文本行，空白分隔字段，# 之后为注释）：
    员工 <姓名> <职级>
    标准 <职级> <类型:住宿|交通|餐补> <限额>
    报销 <编号> <员工> <类型> <金额> <发票号>
    审批 <报销编号> <结果:通过|驳回>

用法：
    python3 expense_check.py [输入文件]     # 缺省读标准输入

规则要点：
    1. 报销金额超过其职级对应类型限额 -> 拦截，报告并定位超量。
    2. 同一发票号被不同报销单占用（占用=待审批/已通过）-> 拦截；
       驳回或拦截会释放发票号，允许修改后重报。
    3. 状态机：提交(待审批) -> 审批 -> 已通过/已驳回；
       仅「已驳回/已拦截」的报销允许同编号修改重报，其余跳转均非法。
    4. 审批引用不存在的报销、报销引用不存在的员工/标准 -> 报告并拦截。
    5. 全流程单遍顺序处理，状态跨流延续。
"""

import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional

VALID_TYPES = {"住宿", "交通", "餐补"}
VALID_RESULTS = {"通过", "驳回"}

PENDING = "待审批"
APPROVED = "已通过"
REJECTED = "已驳回"
BLOCKED = "已拦截"


def parse_money(text):
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def money(value):
    return f"{value.quantize(Decimal('0.01'))}"


@dataclass
class Expense:
    rid: str
    employee: str
    etype: str
    amount: Decimal
    invoice: str
    line_no: int
    status: str = PENDING
    excess: Optional[Decimal] = None  # 超限量
    version: int = 1                  # 提交次数（重报自增）


class Engine:
    def __init__(self):
        self.employees = {}       # 姓名 -> 职级
        self.standards = {}       # (职级, 类型) -> 限额
        self.expenses = {}        # 报销编号 -> Expense
        self.order = []           # 报销编号出现顺序
        self.invoice_holds = {}   # 发票号 -> 占用的报销编号（仅 待审批/已通过）
        self.errors = []          # [(行号, 消息)]

    def error(self, line_no, msg):
        self.errors.append((line_no, msg))

    # ---------- 定义流 ----------
    def add_employee(self, line_no, name, level):
        if name in self.employees:
            self.error(line_no, f"员工 {name} 重复定义，以新定义覆盖")
        self.employees[name] = level

    def add_standard(self, line_no, level, etype, limit_s):
        if etype not in VALID_TYPES:
            self.error(line_no, f"标准引用未知类型 {etype}（应为 住宿/交通/餐补）")
            return
        limit = parse_money(limit_s)
        if limit is None or limit <= 0:
            self.error(line_no, f"标准限额 {limit_s} 非法")
            return
        key = (level, etype)
        if key in self.standards:
            self.error(line_no, f"标准 {level}/{etype} 重复定义，以新定义覆盖")
        self.standards[key] = limit

    # ---------- 报销流 ----------
    def submit(self, line_no, rid, employee, etype, amount_s, invoice):
        amount = parse_money(amount_s)
        if amount is None or amount <= 0:
            self.error(line_no, f"报销 {rid} 金额 {amount_s} 非法，已忽略")
            return
        old = self.expenses.get(rid)
        if old is not None:
            if old.status in (REJECTED, BLOCKED):
                # 驳回/拦截后允许修改重报：释放旧发票占用，重新校验
                self.invoice_holds.pop(old.invoice, None)
                old.employee, old.etype = employee, etype
                old.amount, old.invoice = amount, invoice
                old.line_no, old.excess = line_no, None
                old.version += 1
                self._validate(old)
            else:
                self.error(line_no,
                    f"非法状态跳转：报销 {rid} 当前为「{old.status}」，仅驳回/拦截后允许修改重报")
            return
        exp = Expense(rid, employee, etype, amount, invoice, line_no)
        self.expenses[rid] = exp
        self.order.append(rid)
        self._validate(exp)

    def _validate(self, exp):
        exp.status = PENDING
        if exp.employee not in self.employees:
            exp.status = BLOCKED
            self.error(exp.line_no, f"报销 {exp.rid} 引用不存在的员工 {exp.employee}")
            return
        level = self.employees[exp.employee]
        if exp.etype not in VALID_TYPES:
            exp.status = BLOCKED
            self.error(exp.line_no, f"报销 {exp.rid} 类型 {exp.etype} 未知（应为 住宿/交通/餐补）")
            return
        limit = self.standards.get((level, exp.etype))
        if limit is None:
            exp.status = BLOCKED
            self.error(exp.line_no, f"报销 {exp.rid} 无匹配标准：职级 {level} 未定义 {exp.etype} 限额")
            return
        if exp.amount > limit:
            exp.status = BLOCKED
            exp.excess = exp.amount - limit
            self.error(exp.line_no,
                f"报销 {exp.rid} 金额 {money(exp.amount)} 超职级 {level} {exp.etype} "
                f"限额 {money(limit)}，超量 {money(exp.excess)}")
            return
        holder = self.invoice_holds.get(exp.invoice)
        if holder is not None and holder != exp.rid:
            exp.status = BLOCKED
            self.error(exp.line_no,
                f"报销 {exp.rid} 发票号 {exp.invoice} 与报销 {holder} 重复，已拦截")
            return
        self.invoice_holds[exp.invoice] = exp.rid

    # ---------- 审批流 ----------
    def approve(self, line_no, rid, result):
        if result not in VALID_RESULTS:
            self.error(line_no, f"审批结果 {result} 非法（应为 通过/驳回）")
            return
        exp = self.expenses.get(rid)
        if exp is None:
            self.error(line_no, f"审批引用不存在的报销 {rid}")
            return
        if exp.status != PENDING:
            self.error(line_no,
                f"非法状态跳转：报销 {rid} 当前为「{exp.status}」，仅「待审批」可审批")
            return
        if result == "通过":
            exp.status = APPROVED
        else:
            exp.status = REJECTED
            self.invoice_holds.pop(exp.invoice, None)  # 驳回释放发票号


def report(engine, out):
    out.write("===== 报销状态 =====\n")
    for rid in engine.order:
        e = engine.expenses[rid]
        ver = f"（第{e.version}次提交）" if e.version > 1 else ""
        extra = f"　超量 {money(e.excess)}" if e.excess is not None else ""
        out.write(f"{e.rid}\t{e.employee}\t{e.etype}\t{money(e.amount)}\t"
                  f"发票{e.invoice}\t{e.status}{ver}{extra}\n")
    out.write("===== 错误清单 =====\n")
    if not engine.errors:
        out.write("（无）\n")
    for line_no, msg in engine.errors:
        out.write(f"行{line_no}: {msg}\n")


def run(lines, out):
    engine = Engine()
    for line_no, raw in enumerate(lines, 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        head, args = parts[0], parts[1:]
        if head == "员工" and len(args) == 2:
            engine.add_employee(line_no, *args)
        elif head == "标准" and len(args) == 3:
            engine.add_standard(line_no, *args)
        elif head == "报销" and len(args) == 5:
            engine.submit(line_no, *args)
        elif head == "审批" and len(args) == 2:
            engine.approve(line_no, *args)
        else:
            engine.error(line_no, f"无法解析的行：{line}")
    report(engine, out)


def main(argv):
    if len(argv) > 2:
        sys.exit(__doc__)
    if len(argv) == 2:
        with open(argv[1], encoding="utf-8") as f:
            run(f, sys.stdout)
    else:
        run(sys.stdin, sys.stdout)


if __name__ == "__main__":
    main(sys.argv)
