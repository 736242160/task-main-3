#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
同城票据交换轧差清算工具（纯 Python 标准库，单文件）

输入（文本文件，三段式，# 为注释，字段以空白分隔）：

    [票据]   编号 金额 出票行 收款行
    [交换]   批次 票据编号列表(| 分隔) 状态(提出|提回|清算|退票)
    [退票]   批次 票据编号 原因

状态机（跨流延续，交换流与退票流共享同一状态）：
    提出 -> 提回 -> 清算
    提出/提回 -> 退票        （清算后不得再退票）
    非法跳转一律拦截并报告，不改变状态、不参与轧差。

轧差规则（自定，见 README/帮助）：
    对某批次全部“有效票据”（状态为 提出/提回/清算，即剔除退票）：
        收款行 应收 += 金额；出票行 应付 += 金额
        净额 = 应收 - 应付（正=净应收，负=净应付）
    退票成功后立即按上述规则级联重算该批次净额并留痕。

用法：
    python3 clearing_tool.py            # 运行内置样例（含轧差与退票重算演示）
    python3 clearing_tool.py 输入文件    # 处理输入文件
    python3 clearing_tool.py --json 输入文件
退出码：无错误 0，存在错误 1。
"""

import json
import sys
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

# ---------------------------------------------------------------- 常量

BILLS, EXCHANGES, RETURNS = "票据", "交换", "退票"
PRESENT, TAKEBACK, SETTLED, RETURNED = "提出", "提回", "清算", "退票"
ACTIVE_STATES = (PRESENT, TAKEBACK, SETTLED)  # 参与轧差的状态
LEGAL_TRANSITIONS = {
    None: {PRESENT},
    PRESENT: {TAKEBACK, RETURNED},
    TAKEBACK: {SETTLED, RETURNED},
    SETTLED: set(),
    RETURNED: set(),
}
ZERO = Decimal("0")
CENT = Decimal("0.01")

# ---------------------------------------------------------------- 数据模型

@dataclass
class Bill:
    bill_id: str
    amount: Decimal
    payer: str      # 出票行
    payee: str      # 收款行
    state: str = None  # None=未提出

@dataclass
class Error:
    code: str
    message: str
    stream: str = ""
    line: int = 0
    batch: str = ""
    bill: str = ""

@dataclass
class BatchState:
    batch_id: str
    presented: OrderedDict = field(default_factory=OrderedDict)   # 票据号 -> 状态
    nets: dict = field(default_factory=dict)                      # 行 -> 净额
    trail: list = field(default_factory=list)                     # (触发说明, 净额快照)

# ---------------------------------------------------------------- 清算引擎

class ClearingEngine:
    def __init__(self):
        self.bills = OrderedDict()          # 票据号 -> Bill
        self.batches = OrderedDict()        # 批次 -> BatchState
        self.errors = []

    # ---- 装载票据定义 ----
    def load_bill(self, bill_id, amount, payer, payee, line=0):
        if bill_id in self.bills:
            self._err("E12", f"票据定义重复：{bill_id}（后者被忽略）",
                      stream=BILLS, line=line, bill=bill_id)
            return
        self.bills[bill_id] = Bill(bill_id, amount, payer, payee)

    # ---- 交换流 ----
    def apply_exchange(self, batch_id, bill_ids, status, line=0):
        if status not in (PRESENT, TAKEBACK, SETTLED, RETURNED):
            self._err("E13", f"未知交换状态：{status}", stream=EXCHANGES,
                      line=line, batch=batch_id)
            return
        batch = self.batches.setdefault(batch_id, BatchState(batch_id))
        for bill_id in bill_ids:
            bill = self.bills.get(bill_id)
            if bill is None:
                self._err("E01", f"交换流引用不存在的票据：{bill_id}",
                          stream=EXCHANGES, line=line, batch=batch_id, bill=bill_id)
                continue
            if status == PRESENT:
                if bill_id in batch.presented:
                    self._err("E02", f"票据 {bill_id} 在批次 {batch_id} 内重复提出",
                              stream=EXCHANGES, line=line, batch=batch_id, bill=bill_id)
                    continue
                if bill.state is not None:
                    self._err("E03", f"票据 {bill_id} 跨批次重复提出"
                              f"（当前状态：{bill.state}）",
                              stream=EXCHANGES, line=line, batch=batch_id, bill=bill_id)
                    continue
                bill.state = PRESENT
                batch.presented[bill_id] = PRESENT
            else:
                if bill.state not in LEGAL_TRANSITIONS or \
                   status not in LEGAL_TRANSITIONS.get(bill.state, set()):
                    cur = bill.state if bill.state else "未提出"
                    self._err("E04", f"票据 {bill_id} 非法状态跳转：{cur} -> {status}",
                              stream=EXCHANGES, line=line, batch=batch_id, bill=bill_id)
                    continue
                bill.state = status
                batch.presented[bill_id] = status
        self._recompute(batch, f"交换流 状态={status} 票据={','.join(bill_ids)}")

    # ---- 退票流 ----
    def apply_return(self, batch_id, bill_id, reason, line=0):
        batch = self.batches.get(batch_id)
        if batch is None:
            self._err("E05", f"退票引用不存在的批次：{batch_id}",
                      stream=RETURNS, line=line, batch=batch_id, bill=bill_id)
            return
        bill = self.bills.get(bill_id)
        if bill is None:
            self._err("E06", f"退票引用不存在的票据：{bill_id}",
                      stream=RETURNS, line=line, batch=batch_id, bill=bill_id)
            return
        if not reason:
            self._err("E07", f"票据 {bill_id} 退票原因缺失",
                      stream=RETURNS, line=line, batch=batch_id, bill=bill_id)
            return
        if bill.state == SETTLED:
            self._err("E08", f"票据 {bill_id} 已清算，不得再退票",
                      stream=RETURNS, line=line, batch=batch_id, bill=bill_id)
            return
        if bill.state not in (PRESENT, TAKEBACK):
            cur = bill.state if bill.state else "未提出"
            self._err("E09", f"票据 {bill_id} 非法状态跳转：{cur} -> 退票",
                      stream=RETURNS, line=line, batch=batch_id, bill=bill_id)
            return
        bill.state = RETURNED
        batch.presented[bill_id] = RETURNED
        self._recompute(batch, f"退票流 票据={bill_id} 原因={reason}")

    # ---- 轧差与级联重算 ----
    def _recompute(self, batch, trigger):
        recv, pay = {}, {}
        for bill_id, state in batch.presented.items():
            if state not in ACTIVE_STATES:
                continue  # 退票不参与轧差
            bill = self.bills[bill_id]
            recv[bill.payee] = recv.get(bill.payee, ZERO) + bill.amount
            pay[bill.payer] = pay.get(bill.payer, ZERO) + bill.amount
        nets = {}
        for bank in sorted(set(recv) | set(pay)):
            nets[bank] = recv.get(bank, ZERO) - pay.get(bank, ZERO)
        batch.nets = nets
        batch.trail.append((trigger, dict(nets)))

    # ---- 报告 ----
    def report(self):
        lines = ["=" * 64, "清算结果（各批次轧差净额）", "=" * 64]
        for batch_id, batch in self.batches.items():
            lines.append("")
            lines.append(f"【批次 {batch_id}】")
            lines.append("  票据状态：")
            for bill_id, state in batch.presented.items():
                bill = self.bills[bill_id]
                mark = "（参与轧差）" if state in ACTIVE_STATES else "（已剔除）"
                lines.append(f"    {bill_id}  金额={fmt(bill.amount)}  "
                             f"{bill.payer}->{bill.payee}  状态={state}{mark}")
            lines.append("  轧差净额（应收-应付，正=净应收）：")
            if batch.nets:
                for bank, net in batch.nets.items():
                    lines.append(f"    {pad(bank, 8)} 净额={signed(net)}")
            else:
                lines.append("    （无有效票据）")
            total = sum(batch.nets.values(), ZERO)
            lines.append(f"  差额合计={signed(total)}  "
                         f"{'平衡' if total == ZERO else '不平衡！'}")
            if len(batch.trail) > 1:
                lines.append("  级联重算轨迹：")
                for trigger, snap in batch.trail:
                    nets = "  ".join(f"{b}={signed(n)}" for b, n in snap.items())
                    lines.append(f"    <- {trigger}：{nets}")
        lines.append("")
        lines.append("=" * 64)
        lines.append(f"错误清单（共 {len(self.errors)} 条）")
        lines.append("=" * 64)
        for i, e in enumerate(self.errors, 1):
            loc = f"{e.stream}"
            if e.line:
                loc += f":第{e.line}行"
            ctx = " ".join(x for x in
                           (f"批次={e.batch}" if e.batch else "",
                            f"票据={e.bill}" if e.bill else "") if x)
            lines.append(f"  {i:>2}. [{e.code}] ({loc}) {e.message} {ctx}".rstrip())
        if not self.errors:
            lines.append("  （无错误）")
        return "\n".join(lines)

    def to_json(self):
        return {
            "batches": {
                bid: {
                    "bills": {i: {"state": s,
                                  "amount": str(self.bills[i].amount),
                                  "payer": self.bills[i].payer,
                                  "payee": self.bills[i].payee}
                              for i, s in b.presented.items()},
                    "nets": {k: str(v) for k, v in b.nets.items()},
                    "recompute_trail": [
                        {"trigger": t, "nets": {k: str(v) for k, v in s.items()}}
                        for t, s in b.trail],
                } for bid, b in self.batches.items()
            },
            "errors": [e.__dict__ for e in self.errors],
        }

    def _err(self, code, message, stream="", line=0, batch="", bill=""):
        self.errors.append(Error(code, message, stream, line, batch, bill))

# ---------------------------------------------------------------- 工具函数

def fmt(amount):
    return str(amount.quantize(CENT))

def signed(amount):
    return ("+" if amount >= 0 else "") + fmt(amount)

def width(text):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)

def pad(text, target):
    return text + " " * max(0, target - width(text))

# ---------------------------------------------------------------- 输入解析

def parse_input(text, engine):
    """解析三段式输入；解析错误记入 engine.errors，返回 (交换事件, 退票事件)。"""
    section = None
    exchanges, returns = [], []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            if name in (BILLS, EXCHANGES, RETURNS):
                section = name
            else:
                engine._err("E14", f"未知小节：[{name}]", line=lineno)
                section = None
            continue
        parts = line.split()
        if section == BILLS:
            if len(parts) != 4:
                engine._err("E10", f"票据定义需 4 个字段（编号 金额 出票行 收款行）：{line}",
                            stream=BILLS, line=lineno)
                continue
            bill_id, amount_s, payer, payee = parts
            try:
                amount = Decimal(amount_s)
            except InvalidOperation:
                engine._err("E11", f"金额无法解析：{amount_s}",
                            stream=BILLS, line=lineno, bill=bill_id)
                continue
            if amount <= ZERO:
                engine._err("E11", f"金额必须为正数：{amount_s}",
                            stream=BILLS, line=lineno, bill=bill_id)
                continue
            engine.load_bill(bill_id, amount, payer, payee, line=lineno)
        elif section == EXCHANGES:
            if len(parts) != 3:
                engine._err("E10", f"交换行需 3 个字段（批次 票据列表 状态）：{line}",
                            stream=EXCHANGES, line=lineno)
                continue
            batch_id, ids_s, status = parts
            bill_ids = [i for i in ids_s.split("|") if i]
            if not bill_ids:
                engine._err("E10", f"交换行票据列表为空：{line}",
                            stream=EXCHANGES, line=lineno, batch=batch_id)
                continue
            exchanges.append((batch_id, bill_ids, status, lineno))
        elif section == RETURNS:
            if len(parts) < 2:
                engine._err("E10", f"退票行需至少 2 个字段（批次 票据编号 [原因]）：{line}",
                            stream=RETURNS, line=lineno)
                continue
            batch_id, bill_id = parts[0], parts[1]
            reason = " ".join(parts[2:])
            returns.append((batch_id, bill_id, reason, lineno))
        else:
            engine._err("E14", f"小节外的内容被忽略：{line}", line=lineno)
    return exchanges, returns

def run(text):
    engine = ClearingEngine()
    exchanges, returns = parse_input(text, engine)
    for batch_id, bill_ids, status, lineno in exchanges:
        engine.apply_exchange(batch_id, bill_ids, status, line=lineno)
    for batch_id, bill_id, reason, lineno in returns:
        engine.apply_return(batch_id, bill_id, reason, line=lineno)
    return engine

# ---------------------------------------------------------------- 内置样例

DEMO_INPUT = """\
# ===== 内置样例：轧差 + 退票级联重算 + 各类错误 =====
[票据]
# 编号 金额 出票行 收款行
T1 100 工行A 农行B
T2 200 工行A 建行C
T3 300 农行B 建行C
T4 150 建行C 工行A
T5  80 工行A 农行B
T6  50 工行A 农行B

[交换]
# 批次 票据列表 状态
B1 T1|T2|T3|T4 提出
B1 T1|T2|T3|T4 提回
B1 T1|T2|T3|T4 清算
B2 T5|T6 提出
B2 T5 提出          # 错误：批次内重复提出
B3 T5 提出          # 错误：跨批次重复提出
B1 T1 退票          # 错误：非法跳转（清算->退票）
B2 T5 清算          # 错误：非法跳转（提出->清算，缺提回）
B2 T9 提出          # 错误：票据不存在

[退票]
# 批次 票据编号 原因
B2 T6 印鉴不符      # 合法退票 -> 触发 B2 级联重算
B2 T5               # 错误：退票原因缺失
B1 T2 超期          # 错误：已清算票据不得再退票
B9 T1 批次不存在    # 错误：批次不存在
B2 T9 票据不存在    # 错误：票据不存在
"""

# ---------------------------------------------------------------- 入口

def main(argv):
    args = [a for a in argv[1:] if a != "--json"]
    as_json = "--json" in argv[1:]
    if args:
        with open(args[0], encoding="utf-8") as f:
            text = f.read()
    else:
        text = DEMO_INPUT
        print("内置样例输入：")
        print("-" * 64)
        print(text.rstrip())
        print("-" * 64)
    engine = run(text)
    if as_json:
        print(json.dumps(engine.to_json(), ensure_ascii=False, indent=2))
    else:
        print(engine.report())
    return 1 if engine.errors else 0

if __name__ == "__main__":
    sys.exit(main(sys.argv))
