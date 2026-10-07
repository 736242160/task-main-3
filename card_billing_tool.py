#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
信用卡账单/消费/还款事件流处理工具（纯标准库，单文件）

用法:
    python3 card_billing_tool.py 输入文件      # 处理文件中的事件流
    python3 card_billing_tool.py              # 从标准输入读取事件流
    python3 card_billing_tool.py --demo       # 运行内置自测样例

输入格式（每行一个事件，# 之后为注释，空行忽略）:
    CARD  <卡号> <总额度>
    SPEND <卡号> <金额>
    REPAY <卡号> <金额> <类型>     类型: 全额/FULL、最低/MIN、部分/PART
    BILL  <卡号> <周期> <应还> <最低应还>

==================== 自定规则说明（理由） ====================
1. 计息规则: 账单周期结算时（即同一卡下一张账单到达时），若上期账单
   未全额结清，则对【结算时点剩余全部欠款】按月利率 1.5%（约日息万分之五）
   计收利息并计入欠款。最低还款只免除"逾期"状态，不免利息——这符合
   国内信用卡通行做法：最低还款额是银行给的"不算违约"底线，不是免息线。
2. 逾期规则: 周期结算时，若上期累计还款 < 上期最低应还，则标记逾期，
   逾期缺口 = 最低应还 - 已还。逾期后卡片立即冻结（可用额度=0），
   后续消费一律拦截，直到逾期后累计还款补足逾期缺口才解冻。
   理由：逾期意味着连最低义务都未履行，继续放款风险不可控，必须冻结。
3. 跨周期延续: 欠款（含未出账消费、上期结转、利息）始终占用总额度，
   可用额度 = 总额度 - 当前欠款；上期未还清自然压缩本期可用额度。
4. 还款联动: "全额"必须覆盖当前全部欠款，不足则报错并降级按部分还款
   处理；"最低"必须 >= 当期最低应还，不足则报错并降级（结算时可能逾期）；
   任何还款超出当前欠款的部分拒绝入账（视为溢缴退回）并报告。
5. 消费拦截: 消费 > 可用额度、或卡片冻结、或卡号不存在，一律拒绝入账
   并报告，不产生任何状态变化。
=============================================================
"""
import sys
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

MONTHLY_RATE = Decimal("0.015")   # 月利率 1.5%
CENT = Decimal("0.01")

FULL, MIN, PART = "FULL", "MIN", "PART"
REPAY_ALIASES = {
    "全额": FULL, "FULL": FULL,
    "最低": MIN,  "MIN": MIN,
    "部分": PART, "PART": PART,
}


def money(x):
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


class Card:
    def __init__(self, no, limit):
        self.no = no
        self.limit = limit
        self.outstanding = Decimal("0")          # 当前总欠款（未出账消费+结转+利息）
        self.bill = None                         # 当期未结账单: dict(period, due, min_due, paid)
        self.overdue = False                     # 是否逾期
        self.overdue_amount = Decimal("0")       # 逾期缺口（未补足的最低应还）
        self.repaid_since_overdue = Decimal("0") # 逾期后累计还款
        self.interest_total = Decimal("0")       # 累计利息
        self.frozen = False                      # 冻结（逾期引起）

    @property
    def available(self):
        if self.frozen:
            return Decimal("0")
        return max(Decimal("0"), self.limit - self.outstanding)


class Processor:
    def __init__(self):
        self.cards = {}
        self.errors = []   # (行号, 级别, 消息)

    # ---------- 报告 ----------
    def report(self, lineno, level, msg):
        self.errors.append((lineno, level, msg))

    def error(self, lineno, msg):
        self.report(lineno, "ERROR", msg)

    def warn(self, lineno, msg):
        self.report(lineno, "WARN", msg)

    def info(self, lineno, msg):
        self.report(lineno, "INFO", msg)

    # ---------- 事件处理 ----------
    def do_card(self, lineno, no, limit):
        if no in self.cards:
            self.error(lineno, f"卡 {no} 重复定义，忽略本次定义")
            return
        if limit <= 0:
            self.error(lineno, f"卡 {no} 额度必须为正数，忽略本次定义")
            return
        self.cards[no] = Card(no, limit)

    def do_spend(self, lineno, no, amount):
        card = self.cards.get(no)
        if card is None:
            self.error(lineno, f"消费失败: 卡 {no} 不存在，金额 {amount} 已拒绝")
            return
        if amount <= 0:
            self.error(lineno, f"卡 {no} 消费金额 {amount} 非正数，已拒绝")
            return
        if card.frozen:
            self.error(lineno,
                f"卡 {no} 消费 {amount} 被拦截: 卡片逾期冻结中"
                f"（逾期缺口 {money(card.overdue_amount)}，需先补足）")
            return
        if amount > card.available:
            self.error(lineno,
                f"卡 {no} 消费 {amount} 被拦截: 超可用额度"
                f"（可用 {money(card.available)} / 总额度 {money(card.limit)}）")
            return
        card.outstanding += amount

    def do_repay(self, lineno, no, amount, rtype):
        card = self.cards.get(no)
        if card is None:
            self.error(lineno, f"还款失败: 卡 {no} 不存在，金额 {amount} 已拒绝")
            return
        if amount <= 0:
            self.error(lineno, f"卡 {no} 还款金额 {amount} 非正数，已拒绝")
            return

        # 还款类型联动校验（不足额时降级为部分还款，仍入账）
        if rtype == FULL and amount < card.outstanding:
            self.error(lineno,
                f"卡 {no} 声明全额还款 {amount}，但当前欠款 "
                f"{money(card.outstanding)}，未覆盖全部应还，降级按部分还款处理")
        elif rtype == MIN:
            if card.bill is None:
                self.warn(lineno, f"卡 {no} 当前无未结账单，最低还款按部分还款处理")
            elif amount < card.bill["min_due"]:
                self.error(lineno,
                    f"卡 {no} 最低还款 {amount} 低于当期最低应还 "
                    f"{money(card.bill['min_due'])}，降级按部分还款处理，"
                    f"周期结算时将标记逾期")

        # 超出应还部分拒绝入账
        if amount > card.outstanding:
            self.error(lineno,
                f"卡 {no} 还款 {amount} 超过当前应还 {money(card.outstanding)}，"
                f"超出部分 {money(amount - card.outstanding)} 拒绝入账（退回）")
            amount = card.outstanding
        if amount <= 0:
            return

        card.outstanding -= amount
        if card.bill is not None:
            card.bill["paid"] += amount

        # 逾期解除判定：逾期后累计还款补足逾期缺口即解冻
        if card.overdue:
            card.repaid_since_overdue += amount
            if card.repaid_since_overdue >= card.overdue_amount:
                card.overdue = False
                card.frozen = False
                card.overdue_amount = Decimal("0")
                card.repaid_since_overdue = Decimal("0")
                self.info(lineno, f"卡 {no} 已补足逾期缺口，解除逾期并解冻")

    def do_bill(self, lineno, no, period, due, min_due):
        card = self.cards.get(no)
        if card is None:
            self.error(lineno,
                f"账单引用不存在的卡 {no}（周期 {period}，应还 {due}），已忽略")
            return
        if due < 0 or min_due < 0:
            self.error(lineno, f"卡 {no} 账单金额为负，已忽略")
            return
        if min_due > due:
            self.error(lineno,
                f"卡 {no} 周期 {period} 最低应还 {min_due} 大于应还 {due}，已忽略")
            return

        # ---- 周期结算：结清上一期 ----
        prev = card.bill
        if prev is not None:
            if prev["paid"] < prev["min_due"]:
                shortfall = prev["min_due"] - prev["paid"]
                card.overdue = True
                card.frozen = True
                card.overdue_amount += shortfall
                card.repaid_since_overdue = Decimal("0")
                self.error(lineno,
                    f"卡 {no} 逾期: 周期 {prev['period']} 已还 {money(prev['paid'])} "
                    f"< 最低应还 {money(prev['min_due'])}，缺口 {money(shortfall)}，"
                    f"卡片冻结，后续消费将被拦截")
            if prev["paid"] < prev["due"] and card.outstanding > 0:
                interest = money(card.outstanding * MONTHLY_RATE)
                card.outstanding += interest
                card.interest_total += interest
                self.info(lineno,
                    f"卡 {no} 周期 {prev['period']} 未全额结清，计息 {interest}"
                    f"（基数 {money(card.outstanding - interest)} × 月利率 "
                    f"{MONTHLY_RATE}），计入欠款")

        # ---- 新账单 sanity check ----
        if due > card.outstanding:
            self.warn(lineno,
                f"卡 {no} 周期 {period} 账单应还 {due} 大于系统记录欠款 "
                f"{money(card.outstanding)}，请核对消费流是否完整")

        card.bill = {"period": period, "due": due, "min_due": min_due,
                     "paid": Decimal("0")}

    # ---------- 解析 ----------
    def parse_amount(self, lineno, text, what):
        try:
            value = Decimal(text)
        except InvalidOperation:
            self.error(lineno, f"{what}金额 '{text}' 无法解析，该事件已忽略")
            return None
        return value

    def process_line(self, lineno, line):
        line = line.split("#", 1)[0].strip()
        if not line:
            return
        parts = line.split()
        cmd = parts[0].upper()

        if cmd == "CARD" and len(parts) == 3:
            limit = self.parse_amount(lineno, parts[2], "额度")
            if limit is not None:
                self.do_card(lineno, parts[1], limit)
        elif cmd == "SPEND" and len(parts) == 3:
            amount = self.parse_amount(lineno, parts[2], "消费")
            if amount is not None:
                self.do_spend(lineno, parts[1], amount)
        elif cmd == "REPAY" and len(parts) == 4:
            amount = self.parse_amount(lineno, parts[2], "还款")
            rtype = REPAY_ALIASES.get(parts[3].upper())
            if rtype is None:
                self.error(lineno,
                    f"未知还款类型 '{parts[3]}'（应为 全额/最低/部分 或 FULL/MIN/PART），已忽略")
                return
            if amount is not None:
                self.do_repay(lineno, parts[1], amount, rtype)
        elif cmd == "BILL" and len(parts) == 5:
            due = self.parse_amount(lineno, parts[3], "账单应还")
            min_due = self.parse_amount(lineno, parts[4], "最低应还")
            if due is not None and min_due is not None:
                self.do_bill(lineno, parts[1], parts[2], due, min_due)
        else:
            self.error(lineno, f"无法识别的行: {line!r}，已忽略")

    def run(self, lines):
        for lineno, line in enumerate(lines, 1):
            self.process_line(lineno, line)

    # ---------- 输出 ----------
    def print_status(self, out):
        out.write("=" * 60 + "\n")
        out.write("卡状态\n")
        out.write("=" * 60 + "\n")
        for no in sorted(self.cards):
            c = self.cards[no]
            state = "逾期冻结" if c.frozen else ("逾期" if c.overdue else "正常")
            out.write(
                f"卡号 {no} | 状态 {state} | 总额度 {money(c.limit)} | "
                f"欠款 {money(c.outstanding)} | 可用 {money(c.available)} | "
                f"累计利息 {money(c.interest_total)}\n"
            )
            if c.bill is not None:
                b = c.bill
                out.write(
                    f"    当期账单: 周期 {b['period']} 应还 {money(b['due'])} "
                    f"最低应还 {money(b['min_due'])} 已还 {money(b['paid'])}\n"
                )
            if c.overdue:
                out.write(f"    逾期缺口: {money(c.overdue_amount)}\n")
        out.write("\n" + "=" * 60 + "\n")
        out.write(f"错误/事件报告（共 {len(self.errors)} 条）\n")
        out.write("=" * 60 + "\n")
        if not self.errors:
            out.write("（无）\n")
        for lineno, level, msg in self.errors:
            out.write(f"[行{lineno:>3}] {level:<5} {msg}\n")


DEMO_INPUT = """\
# ===== 自测样例：覆盖全部需求点 =====
CARD C001 10000
CARD C002 5000

# --- C001: 正常消费 + 全额还款 ---
SPEND C001 2000
SPEND C001 500
BILL  C001 2026-09 2500 250
REPAY C001 2500 全额

# --- C001: 超可用额度消费被拦截 ---
SPEND C001 12000

# --- C002: 最低还款 -> 下期对剩余欠款计息 ---
SPEND C002 4000
BILL  C002 2026-09 4000 400
REPAY C002 400 最低
SPEND C002 500            # 未出账消费，占用额度
# 结算时欠款 4100，利息 61.50，结转后 4161.50
BILL  C002 2026-10 4161.50 416.15
REPAY C002 4161.50 FULL

# --- C001: 部分还款不足最低额 -> 逾期级联冻结 ---
SPEND C001 3000
BILL  C001 2026-10 3000 300
REPAY C001 100 部分       # 低于最低应还 300
# 结算: 逾期缺口 200，冻结；欠款 2900 计息 43.50 -> 2943.50
BILL  C001 2026-11 2943.50 294.35
SPEND C001 100            # 被拦截: 冻结中
REPAY C001 200 部分       # 补足逾期缺口 -> 解冻
SPEND C001 100            # 解冻后成功

# --- 错误演示 ---
BILL  C999 2026-09 100 10     # 账单引用不存在的卡
SPEND C888 10                 # 消费引用不存在的卡
REPAY C001 99999 全额         # 还款超应还，超出部分退回
SPEND C002 1000
BILL  C002 2026-11 1000 100
REPAY C002 50 最低            # 低于最低应还，降级并报告
CARD C001 999                 # 重复定义
"""


def main(argv):
    if len(argv) > 1 and argv[1] == "--demo":
        lines = DEMO_INPUT.splitlines()
    elif len(argv) > 1:
        with open(argv[1], encoding="utf-8") as f:
            lines = f.read().splitlines()
    else:
        lines = sys.stdin.read().splitlines()

    proc = Processor()
    proc.run(lines)
    proc.print_status(sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
