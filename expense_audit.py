#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""差旅报销校验工具（纯 Python 标准库，单文件）。

输入格式（文本，按行解析；``#`` 开头为注释，空行忽略）：
    段落标题：员工 / 标准 / 报销 / 审批，可重复出现，全部按文件顺序处理。
      员工：姓名 职级
      标准：职级 类型(住宿|交通|餐补) 限额
      报销：编号 员工 类型(住宿|交通|餐补) 金额 发票号
      审批：报销编号 结果(通过|驳回)

规则：
    1. 报销金额超过员工职级对应类型限额 -> 拦截，并报告超量。
    2. 同一发票号被两笔"有效"报销占用 -> 后一笔拦截（驳回/拦截的报销不占发票号）。
    3. 审批状态机：提交 -> 通过 | 驳回；对非"提交"状态的审批为非法跳转。
    4. 驳回或拦截后允许用同一编号修改重报（重报后重新校验、可再次审批）；
       待审批状态禁止重复提交；已通过为终态，禁止修改。
    5. 审批引用不存在的报销、报销引用不存在的员工或缺失的标准 -> 报告错误。
    6. 报销流与审批流共享同一状态机，按文件顺序推进（跨流状态延续）。

用法：
    python3 expense_audit.py 输入文件     # 校验并输出报告
    python3 expense_audit.py < 输入文件   # 从标准输入读取
    python3 expense_audit.py --demo       # 运行内置示例

退出码：0 无错误；1 存在错误；2 输入文件无法读取。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

TYPES = ("住宿", "交通", "餐补")
RESULTS = ("通过", "驳回")
SECTIONS = ("员工", "标准", "报销", "审批")

ST_PENDING = "提交"    # 已提交，待审批
ST_APPROVED = "通过"   # 终态
ST_REJECTED = "驳回"   # 可修改重报
ST_BLOCKED = "拦截"    # 校验失败，可修改重报
RESUBMITTABLE = (ST_REJECTED, ST_BLOCKED)


def money(value: Decimal) -> str:
    return format(value, "f")


def parse_money(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"金额 {text!r} 不是合法数字")
    if value < 0:
        raise ValueError(f"金额 {text!r} 不能为负")
    return value


@dataclass
class AuditError:
    line: int
    category: str
    message: str


@dataclass
class Employee:
    name: str
    level: str


@dataclass
class Reimbursement:
    rid: str
    employee: str
    rtype: str
    amount: Decimal
    invoice: str
    line: int
    status: str = ST_PENDING
    reason: str = ""
    attempts: int = 1


class Engine:
    """报销校验状态机：按事件顺序推进，跨报销流/审批流共享状态。"""

    def __init__(self) -> None:
        self.employees: dict[str, Employee] = {}
        self.standards: dict[tuple[str, str], Decimal] = {}
        self.reims: dict[str, Reimbursement] = {}
        self.invoice_owner: dict[str, str] = {}  # 发票号 -> 报销编号（仅有效报销占用）
        self.errors: list[AuditError] = []

    def error(self, line: int, category: str, message: str) -> None:
        self.errors.append(AuditError(line, category, message))

    # ---- 定义流 ----

    def add_employee(self, line: int, name: str, level: str) -> None:
        if name in self.employees:
            self.error(line, "重复定义", f"员工 {name} 重复定义，保留首次定义")
            return
        self.employees[name] = Employee(name, level)

    def add_standard(self, line: int, level: str, rtype: str, limit: Decimal) -> None:
        if rtype not in TYPES:
            self.error(line, "格式错误", f"标准类型 {rtype!r} 非法，应为 {'/'.join(TYPES)}")
            return
        key = (level, rtype)
        if key in self.standards:
            self.error(line, "重复定义", f"标准 {level}/{rtype} 重复定义，保留首次定义")
            return
        self.standards[key] = limit

    # ---- 报销流 ----

    def submit(self, line: int, rid: str, emp_name: str, rtype: str,
               amount: Decimal, invoice: str) -> None:
        if rtype not in TYPES:
            self.error(line, "格式错误", f"报销 {rid} 类型 {rtype!r} 非法，应为 {'/'.join(TYPES)}")
            return
        if amount <= 0:
            self.error(line, "格式错误", f"报销 {rid} 金额必须大于 0")
            return

        attempts = 1
        old = self.reims.get(rid)
        if old is not None:
            if old.status in RESUBMITTABLE:
                # 驳回/拦截后修改重报：释放旧占用，作为新一次提交重新校验
                self._free_invoice(old)
                attempts = old.attempts + 1
            elif old.status == ST_PENDING:
                self.error(line, "非法重报", f"报销 {rid} 仍在待审批状态，禁止重复提交")
                return
            else:
                self.error(line, "非法重报", f"报销 {rid} 已通过（终态），禁止修改重报")
                return

        rec = Reimbursement(rid, emp_name, rtype, amount, invoice, line=line,
                            attempts=attempts)
        self.reims[rid] = rec

        emp = self.employees.get(emp_name)
        if emp is None:
            self._block(line, rec, "未知员工", f"员工 {emp_name} 未在员工定义中")
            return
        limit = self.standards.get((emp.level, rtype))
        if limit is None:
            self._block(line, rec, "缺少标准", f"职级 {emp.level} 未定义 {rtype} 限额标准")
            return
        if amount > limit:
            self._block(line, rec, "超出限额",
                        f"金额 {money(amount)} 超 {emp.level}/{rtype} 限额 "
                        f"{money(limit)}，超量 {money(amount - limit)}")
            return
        owner = self.invoice_owner.get(invoice)
        if owner is not None:
            self._block(line, rec, "发票重复", f"发票号 {invoice} 已被报销 {owner} 占用")
            return

        rec.status = ST_PENDING
        self.invoice_owner[invoice] = rid

    def _block(self, line: int, rec: Reimbursement, category: str, detail: str) -> None:
        rec.status = ST_BLOCKED
        rec.reason = f"{category}：{detail}"
        self.error(line, category, f"报销 {rec.rid} 被拦截：{detail}")

    def _free_invoice(self, rec: Reimbursement) -> None:
        if self.invoice_owner.get(rec.invoice) == rec.rid:
            del self.invoice_owner[rec.invoice]

    # ---- 审批流 ----

    def approve(self, line: int, rid: str, result: str) -> None:
        if result not in RESULTS:
            self.error(line, "格式错误", f"审批结果 {result!r} 非法，应为 通过/驳回")
            return
        rec = self.reims.get(rid)
        if rec is None:
            self.error(line, "未知报销", f"审批引用了不存在的报销 {rid}")
            return
        if rec.status != ST_PENDING:
            self.error(line, "非法审批",
                       f"报销 {rid} 当前状态为「{rec.status}」，仅「提交」状态可审批")
            return
        if result == "通过":
            rec.status = ST_APPROVED
        else:
            rec.status = ST_REJECTED
            self._free_invoice(rec)  # 驳回即作废，发票号可被重报复用


def parse_and_run(text: str) -> Engine:
    engine = Engine()
    section = None
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line in SECTIONS:
            section = line
            continue
        parts = line.split()
        if section is None:
            engine.error(lineno, "格式错误", "数据行出现在任何段落标题之前")
            continue
        try:
            if section == "员工":
                if len(parts) != 2:
                    raise ValueError("员工行应为：姓名 职级")
                engine.add_employee(lineno, parts[0], parts[1])
            elif section == "标准":
                if len(parts) != 3:
                    raise ValueError("标准行应为：职级 类型 限额")
                engine.add_standard(lineno, parts[0], parts[1], parse_money(parts[2]))
            elif section == "报销":
                if len(parts) != 5:
                    raise ValueError("报销行应为：编号 员工 类型 金额 发票号")
                engine.submit(lineno, parts[0], parts[1], parts[2],
                              parse_money(parts[3]), parts[4])
            else:  # 审批
                if len(parts) != 2:
                    raise ValueError("审批行应为：报销编号 结果")
                engine.approve(lineno, parts[0], parts[1])
        except ValueError as exc:
            engine.error(lineno, "格式错误", str(exc))
    return engine


def report(engine: Engine) -> None:
    print("========== 报销状态 ==========")
    if not engine.reims:
        print("（无报销记录）")
    for rec in engine.reims.values():
        line = (f"{rec.rid}  员工:{rec.employee}  类型:{rec.rtype}  "
                f"金额:{money(rec.amount)}  发票:{rec.invoice}  状态:{rec.status}")
        if rec.attempts > 1:
            line += f"（第{rec.attempts}次提交）"
        if rec.reason:
            line += f"  [{rec.reason}]"
        print(line)
    print()
    print("========== 错误清单 ==========")
    if not engine.errors:
        print("（无错误）")
    for err in engine.errors:
        print(f"第{err.line}行 [{err.category}] {err.message}")


DEMO = """\
# ---- 员工定义 ----
员工
张三 经理
李四 员工
王五 员工
钱七 实习生

# ---- 标准定义（实习生无标准） ----
标准
经理 住宿 800
经理 交通 300
经理 餐补 150
员工 住宿 500
员工 交通 200
员工 餐补 100

# ---- 报销流 ----
报销
R001 张三 住宿 750 FP1001
R002 李四 住宿 600 FP1002
R003 张三 交通 250 FP1003
R004 李四 餐补 80 FP1003
R005 赵六 住宿 300 FP1004
R006 王五 住宿 450 FP1005
R007 李四 餐补 90 FP1006
R008 钱七 住宿 200 FP1007

# ---- 审批流 ----
审批
R001 通过
R001 驳回
R002 通过
R003 驳回
R004 通过
R006 通过
R009 通过

# ---- 驳回/拦截后修改重报（R003 复用原发票号） ----
报销
R002 李四 住宿 480 FP1002
R003 张三 交通 260 FP1003

审批
R002 通过
R003 通过
"""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="差旅报销校验工具（纯标准库）")
    parser.add_argument("input", nargs="?", help="输入文件（缺省读标准输入）")
    parser.add_argument("--demo", action="store_true", help="运行内置示例")
    args = parser.parse_args(argv)

    if args.demo:
        text = DEMO
    elif args.input:
        try:
            with open(args.input, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            print(f"无法读取输入文件: {exc}", file=sys.stderr)
            return 2
    else:
        text = sys.stdin.read()

    engine = parse_and_run(text)
    report(engine)
    return 1 if engine.errors else 0


if __name__ == "__main__":
    sys.exit(main())
