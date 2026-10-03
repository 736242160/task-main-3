#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同城票据交换轧差清算工具（纯 Python 标准库，单文件）。

用法:
    python3 clearing.py input.json   # 处理输入，输出清算结果与错误报告
    python3 clearing.py --demo       # 运行内置样例（轧差 + 退票级联重算 + 错误拦截）

输入 JSON 格式:
{
  "bills":    [{"编号": "P1", "金额": "100.00", "出票行": "A", "收款行": "B"}],
  "exchanges":[{"批次": 1, "票据": ["P1", "P2"], "状态": "提出"},
               {"批次": 1, "票据": ["P1"], "状态": "退票", "原因": "印鉴不符"}],
  "returns":  [{"批次": 1, "票据": "P2", "原因": "空头"}]
}

事件处理顺序: 按批次排序；同批次内先交换流、后退票流；各流内部保持输入先后
（跨流状态延续：票据在交换流提出后，可在退票流中退票）。

状态机:  (未提出) -> 提出 -> 提回 -> 清算
         提出/提回 -> 退票（清算后禁止退票）
非法跳转一律拦截并记入错误报告。

轧差规则（自定）: 每批次内，以票据"提出"时归属的批次为准，
对处于"清算"状态的票据按行轧差——
    银行净额 = 作为收款行的应收合计 - 作为出票行的应付合计
净额为正为净应收行，为负为净应付行；全行净额合计恒为 0。
批次清算差额 = 净应收合计（= 净应付合计的绝对值）。
退票发生后，该票据从清算集合剔除，所属批次差额立即级联重算并留痕。
"""

import json
import sys
from decimal import Decimal, InvalidOperation

STATES = ("提出", "提回", "清算", "退票")

# 目标状态 -> 允许的源状态集合（None 表示未提出）
TRANSITIONS = {
    "提出": {None},
    "提回": {"提出"},
    "清算": {"提回"},
    "退票": {"提出", "提回"},
}


class Bill:
    __slots__ = ("bid", "amount", "drawer", "payee", "state", "batch")

    def __init__(self, bid, amount, drawer, payee):
        self.bid = bid
        self.amount = amount
        self.drawer = drawer      # 出票行（应付方）
        self.payee = payee        # 收款行（应收方）
        self.state = None         # None=未提出
        self.batch = None         # 提出时归属批次


class Engine:
    def __init__(self):
        self.bills = {}           # 编号 -> Bill
        self.batches = {}         # 批次 -> [票据编号]（提出归属，保持顺序）
        self.errors = []          # 错误清单
        self.recompute_log = []   # 退票后的级联重算留痕

    # ---------- 输入装载 ----------

    def load_bills(self, defs):
        for i, d in enumerate(defs, 1):
            bid = d.get("编号")
            if not bid:
                self.errors.append(f"票据定义第{i}行: 缺少编号")
                continue
            if bid in self.bills:
                self.errors.append(f"票据 {bid}: 重复定义，已忽略后者")
                continue
            try:
                amount = Decimal(str(d.get("金额")))
            except (InvalidOperation, TypeError):
                self.errors.append(f"票据 {bid}: 金额非法 {d.get('金额')!r}")
                continue
            if amount <= 0:
                self.errors.append(f"票据 {bid}: 金额必须为正数")
                continue
            drawer, payee = d.get("出票行"), d.get("收款行")
            if not drawer or not payee:
                self.errors.append(f"票据 {bid}: 出票行/收款行缺失")
                continue
            if drawer == payee:
                self.errors.append(f"票据 {bid}: 出票行与收款行相同")
                continue
            self.bills[bid] = Bill(bid, amount, drawer, payee)

    # ---------- 状态机 ----------

    def _transition(self, bill, target, batch, source):
        if target == "提出" and bill.state is not None:
            self.errors.append(
                f"[批次{batch}] 票据 {bill.bid}: 重复提出"
                f"（首次提出于批次{bill.batch}，当前状态 {bill.state}），已拦截（{source}）")
            return False
        if bill.state not in TRANSITIONS[target]:
            cur = bill.state or "未提出"
            self.errors.append(
                f"[批次{batch}] 票据 {bill.bid}: 非法状态跳转 "
                f"{cur} -> {target}，已拦截（{source}）")
            return False
        if target == "提出":
            bill.batch = batch
            self.batches.setdefault(batch, []).append(bill.bid)
        bill.state = target
        return True

    # ---------- 事件处理 ----------

    def apply_exchange(self, ev):
        batch, status = ev.get("批次"), ev.get("状态")
        ids = ev.get("票据") or []
        if batch is None:
            self.errors.append("交换记录缺少批次，已忽略")
            return
        self.batches.setdefault(batch, [])
        if status not in TRANSITIONS:
            self.errors.append(f"[批次{batch}] 未知交换状态 {status!r}，已忽略")
            return
        if not ids:
            self.errors.append(f"[批次{batch}] 交换记录票据列表为空（状态 {status}）")
            return
        seen = set()
        for bid in ids:
            if bid in seen:
                self.errors.append(f"[批次{batch}] 票据 {bid}: 同一交换记录内重复出现")
                continue
            seen.add(bid)
            bill = self.bills.get(bid)
            if bill is None:
                self.errors.append(f"[批次{batch}] 票据 {bid}: 未定义，已忽略（{status}）")
                continue
            if status == "退票":
                self._do_return(bill, batch, ev.get("原因"), "交换流")
            else:
                self._transition(bill, status, batch, "交换流")

    def apply_return(self, ev):
        batch, bid, reason = ev.get("批次"), ev.get("票据"), ev.get("原因")
        if batch is None or batch not in self.batches:
            self.errors.append(f"退票引用不存在的批次 {batch!r}（票据 {bid!r}），已忽略")
            return
        bill = self.bills.get(bid)
        if bill is None:
            self.errors.append(f"[批次{batch}] 退票引用不存在的票据 {bid!r}，已忽略")
            return
        self._do_return(bill, batch, reason, "退票流")

    def _do_return(self, bill, batch, reason, source):
        if bill.batch != batch:
            self.errors.append(
                f"[批次{batch}] 票据 {bill.bid}: 该票提出归属批次{bill.batch}，"
                f"不能在本批次退票，已拦截（{source}）")
            return
        if not reason or not str(reason).strip():
            self.errors.append(
                f"[批次{batch}] 票据 {bill.bid}: 退票原因缺失，已拦截（{source}）")
            return
        if bill.state == "清算":
            self.errors.append(
                f"[批次{batch}] 票据 {bill.bid}: 已清算，禁止退票，已拦截（{source}）")
            return
        if self._transition(bill, "退票", batch, source):
            self._cascade_recompute(batch, bill, str(reason).strip())

    # ---------- 轧差与级联重算 ----------

    def netting(self, batch):
        """返回 [(银行, 应收, 应付, 净额)]，仅统计本批次处于清算状态的票据。"""
        pos = {}
        for bid in self.batches.get(batch, []):
            b = self.bills[bid]
            if b.state != "清算":
                continue
            pos.setdefault(b.payee, [Decimal(0), Decimal(0)])[0] += b.amount
            pos.setdefault(b.drawer, [Decimal(0), Decimal(0)])[1] += b.amount
        rows = [(bank, r, p, r - p) for bank, (r, p) in sorted(pos.items())]
        return rows

    @staticmethod
    def diff_of(rows):
        """批次清算差额 = 净应收合计。"""
        return sum((net for _, _, _, net in rows if net > 0), Decimal(0))

    def _cascade_recompute(self, batch, bill, reason):
        rows = self.netting(batch)
        self.recompute_log.append(
            f"票据 {bill.bid} 退票（原因: {reason}）-> 批次{batch} 级联重算: "
            f"清算差额 = {_m(self.diff_of(rows))}；"
            + ("；".join(f"{bank} 净额 {net}" for bank, _, _, net in rows) or "本批次已无清算票据"))

    # ---------- 报告 ----------

    def report(self):
        out = ["=" * 56, "各批次清算结果（轧差后净额）", "=" * 56]
        for batch in sorted(self.batches, key=_batch_key):
            rows = self.netting(batch)
            out.append(f"\n批次 {batch}:")
            out.append(f"  {'银行':<8}{'应收':>14}{'应付':>14}{'净额':>14}")
            for bank, r, p, net in rows:
                out.append(f"  {bank:<8}{_m(r):>14}{_m(p):>14}{_m(net):>14}")
            cleared = sum(1 for bid in self.batches[batch]
                          if self.bills[bid].state == "清算")
            returned = sum(1 for bid in self.batches[batch]
                           if self.bills[bid].state == "退票")
            out.append(f"  清算票据 {cleared} 张，退票 {returned} 张，"
                       f"清算差额（净应收合计）= {_m(self.diff_of(rows))}")
        if self.recompute_log:
            out += ["", "=" * 56, "退票级联重算留痕", "=" * 56]
            out += ["  " + s for s in self.recompute_log]
        out += ["", "=" * 56, f"错误报告（共 {len(self.errors)} 条）", "=" * 56]
        out += [f"  {i}. {e}" for i, e in enumerate(self.errors, 1)] or ["  无"]
        return "\n".join(out)


def _batch_key(b):
    try:
        return (0, int(b), "")
    except (TypeError, ValueError):
        return (1, 0, str(b))


def _m(d):
    """金额统一保留两位小数显示。"""
    return f"{d.quantize(Decimal('0.01'))}"


def run(data):
    eng = Engine()
    eng.load_bills(data.get("bills", []))
    # 跨流合并：按批次排序，同批次先交换流后退票流，各流内保持输入顺序
    events = ([(_batch_key(e.get("批次")), 0, i, e)
               for i, e in enumerate(data.get("exchanges", []))]
              + [(_batch_key(e.get("批次")), 1, i, e)
                 for i, e in enumerate(data.get("returns", []))])
    for _, stream, _, ev in sorted(events, key=lambda t: t[:3]):
        (eng.apply_exchange if stream == 0 else eng.apply_return)(ev)
    return eng


# ---------- 内置样例 ----------

DEMO = {
    "bills": [
        {"编号": "P1", "金额": "100.00", "出票行": "A", "收款行": "B"},
        {"编号": "P2", "金额": "200.00", "出票行": "B", "收款行": "A"},
        {"编号": "P3", "金额": "50.00",  "出票行": "A", "收款行": "C"},
        {"编号": "P4", "金额": "80.00",  "出票行": "C", "收款行": "B"},
        {"编号": "P5", "金额": "60.00",  "出票行": "B", "收款行": "C"},
        {"编号": "P6", "金额": "40.00",  "出票行": "C", "收款行": "A"},
    ],
    "exchanges": [
        # 批次1：正常 提出 -> 提回 -> 清算
        {"批次": 1, "票据": ["P1", "P2", "P3", "P4"], "状态": "提出"},
        {"批次": 1, "票据": ["P1", "P2", "P3", "P4"], "状态": "提回"},
        {"批次": 1, "票据": ["P1", "P2", "P3", "P4"], "状态": "清算"},
        # 批次2：P5、P6 提出并提回，P5 先清算，P6 清算前被退票（见 returns）
        {"批次": 2, "票据": ["P5", "P6"], "状态": "提出"},
        {"批次": 2, "票据": ["P5", "P6"], "状态": "提回"},
        {"批次": 2, "票据": ["P5"], "状态": "清算"},
        # 各类错误样例
        {"批次": 1, "票据": ["P1"], "状态": "提出"},        # 跨批次重复提出
        {"批次": 1, "票据": ["P1"], "状态": "退票", "原因": "迟了"},  # 清算后退票
        {"批次": 3, "票据": ["P9"], "状态": "提出"},        # 未定义票据
        {"批次": 3, "票据": ["P5"], "状态": "清算"},        # 非法跳转 提回->清算?（P5在批次2提回）
    ],
    "returns": [
        {"批次": 2, "票据": "P6", "原因": "印鉴不符"},       # 正常退票 -> 级联重算
        {"批次": 1, "票据": "P2", "原因": "空头"},           # 清算后退票 -> 拦截
        {"批次": 1, "票据": "P3"},                           # 退票原因缺失 -> 拦截
        {"批次": 9, "票据": "P1", "原因": "x"},              # 批次不存在
        {"批次": 1, "票据": "PX", "原因": "x"},              # 票据不存在
    ],
}


def main(argv):
    if len(argv) >= 2 and argv[1] == "--demo":
        print("【内置样例输入】")
        print(json.dumps(DEMO, ensure_ascii=False, indent=2))
        print()
        eng = run(DEMO)
    elif len(argv) >= 2:
        with open(argv[1], encoding="utf-8") as f:
            eng = run(json.load(f))
    else:
        print(__doc__)
        return 0
    print(eng.report())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
