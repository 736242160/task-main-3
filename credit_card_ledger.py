#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
信用卡账单/消费/还款/额度联动核算工具（纯 Python 标准库，单文件）。

用法：
    python3 credit_card_ledger.py 输入文件      # 从文件读取事件流
    python3 credit_card_ledger.py              # 从标准输入读取
    python3 credit_card_ledger.py --demo       # 运行内置自测样例（含断言）

输入格式（每行一个事件，# 之后为注释，空行忽略）：
    CARD  <卡号> <总额度>
    SPEND <卡号> <金额>
    REPAY <卡号> <金额> <类型>        类型：全额 / 最低 / 部分（或 FULL/MIN/PART）
    BILL  <卡号> <周期> <应还> <最低应还>

自定规则（理由附后）：
1. 可用额度 = 当前总额度 - 当前欠款；消费实时扣减，超可用额度即拦截并报错。
2. 账单出具时核对：账单"应还"必须等于系统内部欠款（含上期循环利息），
   不一致即报错——防止"最低额算错导致逾期"；最低应还必须 <= 应还。
3. 还款类型联动：
   - 全额：金额必须等于当前总欠款，否则报错并拒绝；
   - 最低：金额必须等于当期账单最低应还；最低还款后剩余未还本金自下期
     账单起按 1.5%/周期 计循环利息（理由：行业惯例日息万分之五，30 天约
     合 1.5%），利息计入下期欠款；
   - 部分：任意金额；若到下期账单出具时，本期累计还款 < 本期最低应还，
     判定逾期。
4. 逾期级联：卡片立即冻结（可用额度=0，后续消费一律拦截），逾期次数+1；
   还清全部欠款（含息）后解冻，但总额度下调为原来的 80%（下限 1000 元），
   直接压缩后续可用额度。
5. 还款金额超过当前总欠款 -> 报错并拒绝该笔还款。
6. 消费/还款/账单引用不存在的卡 -> 报错并忽略该事件。
7. 跨周期状态延续：欠款、未结账单、本期已还金额沿事件流持续累积，
   上期未还清部分自动滚入下期并计息，持续占用额度。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

CENT = Decimal("0.01")
CYCLE_INTEREST_RATE = Decimal("0.015")   # 日息万分之五 x 30 天
LIMIT_KEEP_RATIO = Decimal("0.8")        # 逾期解冻后额度保留比例
MIN_LIMIT = Decimal("1000")              # 额度下调下限

REPAY_TYPE_MAP = {
    "全额": "FULL", "full": "FULL",
    "最低": "MIN", "min": "MIN",
    "部分": "PART", "part": "PART",
}


def money(token):
    """解析金额并统一到分。非法输入抛异常。"""
    value = Decimal(token)
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class Bill:
    cycle: str
    due: Decimal
    min_due: Decimal


@dataclass
class Card:
    card_id: str
    limit: Decimal
    original_limit: Decimal
    debt: Decimal = Decimal("0.00")
    frozen: bool = False
    overdue_count: int = 0
    bill: Bill | None = None          # 当前未结账单
    cycle_paid: Decimal = Decimal("0.00")  # 当前账单周期累计已还

    @property
    def available(self):
        if self.frozen:
            return Decimal("0.00")
        return max(Decimal("0.00"), self.limit - self.debt)


class Engine:
    def __init__(self):
        self.cards = {}
        self.errors = []   # [(行号, 消息)]
        self.notes = []    # [(行号, 消息)] 正常但值得提示的事件

    # ---------- 事件处理 ----------

    def on_card(self, lineno, card_id, limit):
        if card_id in self.cards:
            self.errors.append((lineno, f"卡 {card_id} 重复定义，忽略"))
            return
        if limit <= 0:
            self.errors.append((lineno, f"卡 {card_id} 总额度必须为正数，忽略"))
            return
        self.cards[card_id] = Card(card_id, limit, limit)

    def on_spend(self, lineno, card_id, amount):
        card = self.cards.get(card_id)
        if card is None:
            self.errors.append((lineno, f"消费引用不存在的卡 {card_id}，忽略"))
            return
        if amount <= 0:
            self.errors.append((lineno, f"卡 {card_id} 消费金额必须为正数，忽略"))
            return
        if card.frozen:
            self.errors.append(
                (lineno, f"卡 {card_id} 已因逾期冻结，消费 {amount} 被拦截"))
            return
        if amount > card.available:
            self.errors.append(
                (lineno,
                 f"卡 {card_id} 消费 {amount} 超可用额度 {card.available}，被拦截"))
            return
        card.debt += amount

    def on_repay(self, lineno, card_id, amount, rtype):
        card = self.cards.get(card_id)
        if card is None:
            self.errors.append((lineno, f"还款引用不存在的卡 {card_id}，忽略"))
            return
        if amount <= 0:
            self.errors.append((lineno, f"卡 {card_id} 还款金额必须为正数，忽略"))
            return
        if amount > card.debt:
            self.errors.append(
                (lineno,
                 f"卡 {card_id} 还款 {amount} 超过当前应还 {card.debt}，拒绝该笔还款"))
            return
        if rtype == "FULL":
            if amount != card.debt:
                self.errors.append(
                    (lineno,
                     f"卡 {card_id} 全额还款金额 {amount} 不等于当前总欠款 "
                     f"{card.debt}，拒绝（少还请用“部分”）"))
                return
        elif rtype == "MIN":
            if card.bill is None:
                self.errors.append(
                    (lineno, f"卡 {card_id} 无未结账单，无法按最低应还还款，拒绝"))
                return
            if amount != card.bill.min_due:
                self.errors.append(
                    (lineno,
                     f"卡 {card_id} 最低还款金额 {amount} 不等于账单最低应还 "
                     f"{card.bill.min_due}，拒绝"))
                return
        card.debt -= amount
        card.cycle_paid += amount
        if card.frozen and card.debt == 0:
            card.frozen = False
            new_limit = (card.limit * LIMIT_KEEP_RATIO).quantize(
                CENT, rounding=ROUND_HALF_UP)
            card.limit = max(new_limit, MIN_LIMIT)
            self.notes.append(
                (lineno,
                 f"卡 {card_id} 已还清全部欠款，解除冻结；总额度下调为 {card.limit}"))

    def on_bill(self, lineno, card_id, cycle, due, min_due):
        card = self.cards.get(card_id)
        if card is None:
            self.errors.append((lineno, f"账单引用不存在的卡 {card_id}，忽略"))
            return
        if due < 0 or min_due < 0:
            self.errors.append((lineno, f"卡 {card_id} 账单金额不能为负，忽略"))
            return
        if min_due > due:
            self.errors.append(
                (lineno,
                 f"卡 {card_id} 账单 {cycle} 最低应还 {min_due} 大于应还 {due}，"
                 f"账单数据有误"))
            return
        prev = card.bill
        if prev is not None:
            paid = card.cycle_paid
            if paid < prev.min_due:
                card.overdue_count += 1
                card.frozen = True
                self.errors.append(
                    (lineno,
                     f"卡 {card_id} 上期账单 {prev.cycle} 累计还款 {paid} 低于最低应还 "
                     f"{prev.min_due}，判定逾期（第 {card.overdue_count} 次），卡片冻结"))
            if paid < prev.due:
                unpaid = prev.due - paid
                interest = (unpaid * CYCLE_INTEREST_RATE).quantize(
                    CENT, rounding=ROUND_HALF_UP)
                if interest > 0:
                    card.debt += interest
                    self.notes.append(
                        (lineno,
                         f"卡 {card_id} 上期未全额还款，剩余 {unpaid} 计循环利息 "
                         f"{interest}（{CYCLE_INTEREST_RATE}/周期），计入本期欠款"))
        if due != card.debt:
            self.errors.append(
                (lineno,
                 f"卡 {card_id} 账单 {cycle} 应还 {due} 与系统核算欠款 {card.debt} "
                 f"不一致，请核对（最低额算错会导致误判逾期）"))
        card.bill = Bill(cycle, due, min_due)
        card.cycle_paid = Decimal("0.00")

    # ---------- 驱动 ----------

    def process(self, text):
        for lineno, raw in enumerate(text.splitlines(), 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            op = parts[0].upper()
            try:
                if op in ("CARD", "卡") and len(parts) == 3:
                    self.on_card(lineno, parts[1], money(parts[2]))
                elif op in ("SPEND", "消费") and len(parts) == 3:
                    self.on_spend(lineno, parts[1], money(parts[2]))
                elif op in ("REPAY", "还款") and len(parts) == 4:
                    rtype = REPAY_TYPE_MAP.get(parts[3].lower(), REPAY_TYPE_MAP.get(parts[3]))
                    if rtype is None:
                        self.errors.append(
                            (lineno, f"未知还款类型 {parts[3]}（应为 全额/最低/部分）"))
                        continue
                    self.on_repay(lineno, parts[1], money(parts[2]), rtype)
                elif op in ("BILL", "账单") and len(parts) == 5:
                    self.on_bill(lineno, parts[1], parts[2],
                                 money(parts[3]), money(parts[4]))
                else:
                    self.errors.append((lineno, f"无法解析的行：{raw.strip()}"))
            except (InvalidOperation, ValueError):
                self.errors.append((lineno, f"金额格式错误：{raw.strip()}"))

    def finish(self):
        for card in self.cards.values():
            if card.bill is not None and card.cycle_paid < card.bill.due:
                self.notes.append(
                    (0, f"卡 {card.card_id} 账单 {card.bill.cycle} 尚未结清"
                        f"（已还 {card.cycle_paid} / 应还 {card.bill.due}），"
                        f"待下期账单出具时判定是否逾期"))

    # ---------- 输出 ----------

    def report(self):
        out = ["==== 卡状态 ===="]
        for card in self.cards.values():
            status = "冻结" if card.frozen else "正常"
            bill = (f"未结账单 {card.bill.cycle} 应还 {card.bill.due} "
                    f"最低 {card.bill.min_due}") if card.bill else "无未结账单"
            out.append(
                f"卡 {card.card_id} | 状态 {status} | 总额度 {card.limit}"
                f"（原始 {card.original_limit}） | 欠款 {card.debt} | "
                f"可用 {card.available} | 逾期 {card.overdue_count} 次 | "
                f"本期已还 {card.cycle_paid} | {bill}")
        out.append("")
        out.append(f"==== 错误报告（{len(self.errors)} 条）====")
        for lineno, msg in self.errors:
            out.append(f"[行{lineno}] {msg}")
        out.append("")
        out.append(f"==== 提示（{len(self.notes)} 条）====")
        for lineno, msg in self.notes:
            tag = f"[行{lineno}]" if lineno else "[结束]"
            out.append(f"{tag} {msg}")
        return "\n".join(out)


DEMO = """\
# ---- 卡定义 ----
CARD 6222 10000
CARD 6666 5000

# ---- 卡 6222：最低还款 -> 下期计循环利息 ----
SPEND 6222 3000
SPEND 9999 100            # 错误：卡不存在
BILL  9999 2026-09 100 10 # 错误：账单引用不存在的卡
BILL  6222 2026-09 3000 300
REPAY 6222 300 最低
REPAY 6222 4000 部分      # 错误：超过当前应还 2700
BILL  6222 2026-10 2740.50 300   # 2700*1.5%=40.50 利息，与系统一致
SPEND 6222 8000           # 错误：可用仅 7259.50，拦截
SPEND 6222 7000

# ---- 卡 6666：部分还款不足最低 -> 逾期 -> 冻结 -> 还清解冻降额 ----
SPEND 6666 6000           # 错误：超总额度，拦截
SPEND 6666 4500
BILL  6666 2026-09 4500 450
REPAY 6666 100 部分       # 低于最低应还 450
BILL  6666 2026-10 4466 446.60   # 触发逾期；利息 4400*1.5%=66
SPEND 6666 10             # 错误：已冻结，拦截
REPAY 6666 4466 全额      # 还清解冻，额度 5000 -> 4000
SPEND 6666 4100           # 错误：降额后可用仅 4000，拦截
SPEND 6666 1000           # 正常
"""


def run_demo():
    engine = Engine()
    engine.process(DEMO)
    engine.finish()
    print(engine.report())

    c2 = engine.cards["6222"]
    c6 = engine.cards["6666"]
    assert len(engine.errors) == 8, f"预期 8 条错误，实际 {len(engine.errors)}"
    assert c2.debt == Decimal("9740.50"), c2.debt
    assert c2.available == Decimal("259.50"), c2.available
    assert not c2.frozen and c2.overdue_count == 0
    assert c6.debt == Decimal("1000.00"), c6.debt
    assert c6.limit == Decimal("4000.00"), c6.limit
    assert c6.available == Decimal("3000.00"), c6.available
    assert not c6.frozen and c6.overdue_count == 1
    assert any("循环利息" in m for _, m in engine.notes)
    assert any("解除冻结" in m for _, m in engine.notes)
    print("\n自测断言全部通过。")


def main(argv):
    if len(argv) > 1 and argv[1] == "--demo":
        run_demo()
        return
    if len(argv) > 1:
        with open(argv[1], encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()
    engine = Engine()
    engine.process(text)
    engine.finish()
    print(engine.report())


if __name__ == "__main__":
    main(sys.argv)
