#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vending.py — 售货机交易状态机 / 零钱池动态 / 故障退款联动 模拟器
纯 Python 标准库，单文件，Python 3.7+。

输入格式（行导向，空白分隔，`#` 之后为注释）：

    商品 <名称> <价格> <库存>
    币种 <面额> <初始零钱数>
    交易 <ID> <商品> <投币列表> <状态> [状态...]
    交易 <ID> <状态> [状态...]          # 续写未完结交易（配合跨行故障注入）
    故障 <机器ID> <故障|恢复>

    投币列表：逗号分隔的面额（如 5,1,1），`-` 表示未投币。
    状态序列：投币/选购/出货/找零/完成/退款，状态机对每一次跳转做合法性校验。

用法：
    python3 vending.py 输入文件      # 从文件读取
    python3 vending.py               # 从标准输入读取
    python3 vending.py --demo        # 运行内置示例
"""

import sys
from dataclasses import dataclass, field

ALL_STATES = ("投币", "选购", "出货", "找零", "完成", "退款")

# 状态机：当前状态 -> 允许的下一状态集合（用户/流程驱动）
# 系统级联退款不走此表，由系统在任何非终态直接触发。
LEGAL = {
    "空闲": {"投币"},
    "投币": {"投币", "选购", "退款"},   # 投币可重复（分多次投）；退款=用户取消
    "选购": {"出货", "退款"},
    "出货": {"找零"},
    "找零": {"完成"},
    "完成": set(),                      # 终态
    "退款": set(),                      # 终态
}
TERMINAL = {"完成", "退款"}


def total(coins):
    return sum(d * c for d, c in coins.items())


def fmt_coins(coins):
    if not coins:
        return "0"
    return "+".join(f"{d}x{c}" for d, c in sorted(coins.items(), reverse=True))


def make_change(pool, amount):
    """大面额优先贪心找零。返回 {面额: 张数}，无法凑出返回 None。

    理由：张数最少、出币机构动作最少、符合用户习惯；对人民币这类
    canonical 币系贪心即最优。对非 canonical 币系贪心可能失败，
    此时宁可判定“找零不足”并全额退款，也不少找用户一分钱。
    """
    plan = {}
    rest = amount
    for d in sorted(pool, reverse=True):
        if d <= 0:
            continue
        take = min(pool[d], rest // d)
        if take:
            plan[d] = take
            rest -= d * take
    return plan if rest == 0 else None


@dataclass
class Txn:
    tid: str
    product: str
    state: str = "空闲"
    inserted: dict = field(default_factory=dict)   # 有效投币（已入暂存仓）
    escrow: dict = field(default_factory=dict)     # 暂存仓：完成前不计入零钱池
    change_plan: dict = field(default_factory=dict)
    done: bool = False


class Machine:
    def __init__(self):
        self.products = {}   # 名称 -> {"price": int, "stock": int}
        self.pool = {}       # 面额 -> 枚数（零钱池）
        self.denoms = set()  # 合法面额
        self.faults = set()  # 故障中的机器 ID
        self.active = None   # 进行中的交易（跨行悬挂）
        self.results = []    # (tid, 结论, 明细)
        self.errors = []     # 错误清单
        self.trace = []      # 资金/库存轨迹

    def error(self, tid, msg):
        self.errors.append(f"[{tid}] {msg}")

    @staticmethod
    def add_coins(dst, coins):
        for d, c in coins.items():
            dst[d] = dst.get(d, 0) + c

    @staticmethod
    def sub_coins(dst, coins):
        for d, c in coins.items():
            dst[d] -= c
            if dst[d] == 0:
                del dst[d]

    # ---------- 定义类指令 ----------
    def cmd_product(self, name, price, stock):
        self.products[name] = {"price": price, "stock": stock}

    def cmd_coin(self, denom, count):
        self.denoms.add(denom)
        self.pool[denom] = self.pool.get(denom, 0) + count

    # ---------- 交易指令 ----------
    def cmd_txn(self, tid, product, coins, states):
        if product is None and self.active is not None and self.active.tid == tid:
            txn = self.active  # 续写未完结交易
        else:
            if self.faults:
                self.error(tid, f"机器 {','.join(sorted(self.faults))} 故障中，新交易被拦截")
                self.results.append((tid, "拒绝", "机器故障，交易未受理"))
                return
            if self.active is not None:
                self.error(tid, f"交易 {self.active.tid} 未完结，新交易被拦截")
                self.results.append((tid, "拒绝", "上一交易未完结"))
                return
            if product not in self.products:
                self.error(tid, f"引用不存在的商品: {product}")
                self.results.append((tid, "失败", f"商品 {product} 不存在"))
                return
            txn = Txn(tid=tid, product=product)
            for d, c in coins.items():
                if d in self.denoms:
                    txn.escrow[d] = txn.escrow.get(d, 0) + c
                else:
                    self.error(tid, f"未知面额 {d}，已拒收退回")
            txn.inserted = dict(txn.escrow)
            self.active = txn
            self.trace.append(f"{tid}: 开始 商品={product} 投币={fmt_coins(txn.escrow)}")
        for st in states:
            if txn.done or self.active is not txn:
                break
            self.step(txn, st)

    # ---------- 状态机 ----------
    def step(self, txn, nxt):
        tid = txn.tid
        if nxt not in ALL_STATES:
            self.error(tid, f"未知状态 '{nxt}'，交易拦截并级联退款")
            self.cascade_refund(txn, f"未知状态 {nxt}")
            return
        if nxt not in LEGAL[txn.state]:
            self.error(tid, f"非法状态跳转: {txn.state} -> {nxt}，已拦截并级联退款")
            self.cascade_refund(txn, f"非法跳转 {txn.state}->{nxt}")
            return
        handler = {
            "投币": self.on_insert,
            "选购": self.on_select,
            "出货": self.on_dispense,
            "找零": self.on_change,
            "完成": self.on_done,
            "退款": self.on_cancel,
        }[nxt]
        handler(txn)
        if not txn.done:
            txn.state = nxt

    def on_insert(self, txn):
        pass  # 投币已在交易开始时入暂存仓；重复投币状态为空转

    def on_select(self, txn):
        p = self.products[txn.product]
        paid = total(txn.escrow)
        if p["stock"] <= 0:
            self.error(txn.tid, f"商品 {txn.product} 缺货，级联退款 {fmt_coins(txn.escrow)}")
            self.cascade_refund(txn, "缺货")
        elif paid < p["price"]:
            self.error(txn.tid, f"投入不足: 需 {p['price']} 实投 {paid}，级联退款 {fmt_coins(txn.escrow)}")
            self.cascade_refund(txn, "投入不足")

    def on_dispense(self, txn):
        p = self.products[txn.product]
        change = total(txn.escrow) - p["price"]
        avail = dict(self.pool)
        self.add_coins(avail, txn.escrow)  # 用“零钱池+在途投币”预估找零能力
        plan = make_change(avail, change) if change > 0 else {}
        if plan is None:
            self.error(txn.tid, f"找零不足: 应找 {change}，零钱池无法凑出，级联退款 {fmt_coins(txn.escrow)}")
            self.cascade_refund(txn, "找零不足")
            return
        p["stock"] -= 1  # 库存跨交易延续扣减
        txn.change_plan = plan
        self.trace.append(f"{txn.tid}: 出货 {txn.product} 库存 {p['stock'] + 1}->{p['stock']} 待找零 {fmt_coins(plan)}")

    def on_change(self, txn):
        # 资金结算点：暂存仓收入入账，找零出账
        self.add_coins(self.pool, txn.escrow)
        self.sub_coins(self.pool, txn.change_plan)
        self.trace.append(f"{txn.tid}: 零钱池 收入+{fmt_coins(txn.escrow)} 找零-{fmt_coins(txn.change_plan)}")
        txn.escrow = {}

    def on_done(self, txn):
        p = self.products[txn.product]
        txn.done = True
        self.active = None
        self.results.append((txn.tid, "成功",
                             f"商品={txn.product} 售价={p['price']} 实收={total(txn.inserted)} 找零={fmt_coins(txn.change_plan)}"))

    def on_cancel(self, txn):
        self.trace.append(f"{txn.tid}: 用户取消，原币退款 {fmt_coins(txn.escrow)}")
        txn.escrow = {}
        txn.done = True
        self.active = None
        self.results.append((txn.tid, "取消", "用户主动退款"))

    # ---------- 级联退款 ----------
    def cascade_refund(self, txn, reason):
        if txn.state == "找零":
            # 货款已结、找零已出，资金侧无遗留，按完成结算
            txn.done = True
            self.active = None
            self.results.append((txn.tid, "成功", f"{reason} 发生于结算后，按完成处理"))
            return
        if txn.escrow:
            self.trace.append(f"{txn.tid}: 级联退款 原币退回 {fmt_coins(txn.escrow)}（原因: {reason}）")
        txn.escrow = {}
        txn.done = True
        if self.active is txn:
            self.active = None
        self.results.append((txn.tid, "失败", f"{reason}，已级联退款"))

    # ---------- 故障指令 ----------
    def cmd_fault(self, mid, action):
        if action == "故障":
            self.faults.add(mid)
            self.trace.append(f"机器 {mid} 进入故障")
            if self.active is not None:
                self.error(self.active.tid, f"机器 {mid} 故障，交易中途级联退款")
                self.cascade_refund(self.active, "机器故障")
        elif action == "恢复":
            if mid in self.faults:
                self.faults.discard(mid)
                self.trace.append(f"机器 {mid} 恢复，交易放行")
            else:
                self.error("-", f"机器 {mid} 未处于故障状态，恢复指令无效")
        else:
            self.error("-", f"未知故障动作: {action}")

    def finalize(self):
        if self.active is not None:
            self.error(self.active.tid, f"输入结束时交易停留在状态 {self.active.state}，强制级联退款")
            self.cascade_refund(self.active, "交易未完结")


def parse_coins(text):
    coins = {}
    if text in ("-", ""):
        return coins
    for part in text.replace("，", ",").split(","):
        d = int(part)
        coins[d] = coins.get(d, 0) + 1
    return coins


def run(src):
    m = Machine()
    for ln, raw in enumerate(src.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        kw = parts[0]
        try:
            if kw == "商品":
                m.cmd_product(parts[1], int(parts[2]), int(parts[3]))
            elif kw == "币种":
                m.cmd_coin(int(parts[1]), int(parts[2]))
            elif kw == "交易":
                tid = parts[1]
                if parts[2] in ALL_STATES:  # 续行：交易 <ID> <状态...>
                    m.cmd_txn(tid, None, {}, parts[2:])
                else:
                    m.cmd_txn(tid, parts[2], parse_coins(parts[3]), parts[4:])
            elif kw == "故障":
                m.cmd_fault(parts[1], parts[2])
            else:
                m.error("-", f"第{ln}行: 未知指令 '{kw}'")
        except (IndexError, ValueError):
            m.error("-", f"第{ln}行: 格式错误: {raw.strip()}")
    m.finalize()
    return m


def report(m):
    out = ["========== 交易结果 =========="]
    out += [f"{tid:<5} {status}  {detail}" for tid, status, detail in m.results] or ["（无）"]
    out.append("========== 错误报告 ==========")
    out += [f"{i:>2}. {e}" for i, e in enumerate(m.errors, 1)] or ["（无）"]
    out.append("========== 资金与库存轨迹 ==========")
    out += m.trace or ["（无）"]
    out.append("========== 期末零钱池 ==========")
    out += [f"面额 {d}: {m.pool.get(d, 0)} 枚" for d in sorted(m.denoms, reverse=True)]
    out.append("========== 期末库存 ==========")
    out += [f"{name}: {p['stock']}（售价 {p['price']}）" for name, p in m.products.items()]
    return "\n".join(out)


DEMO = r"""
# ===== 商品定义：名称 价格 库存 =====
商品 可乐 3 2
商品 薯片 5 1
商品 矿泉水 2 0
商品 口香糖 1 1

# ===== 币种定义：面额 初始零钱数 =====
币种 1 3
币种 5 1
币种 10 1

# T1 正常交易：投 5 买 3 元可乐，找 2（1x2）
交易 T1 可乐 5 投币 选购 出货 找零 完成

# T2 非法跳转：投币后直接出货，拦截并级联退款
交易 T2 可乐 5 投币 出货 找零 完成

# T3 投入不足：3 元可乐只投 2 元，级联退款
交易 T3 可乐 1,1 投币 选购 出货 找零 完成

# T4 缺货：矿泉水库存 0，级联退款
交易 T4 矿泉水 5 投币 选购 出货 找零 完成

# T5 找零不足：1 元口香糖投 10 元，零钱池凑不出 9 元，级联退款
交易 T5 口香糖 10 投币 选购 出货 找零 完成

# T6 中途故障：交易跨行悬挂在选购，故障事件触发级联退款
交易 T6 可乐 5 投币 选购
故障 M1 故障

# T7 故障期间新交易被拦截
交易 T7 可乐 5 投币 选购 出货 找零 完成

# 恢复后交易放行
故障 M1 恢复

# T8 恢复后正常交易：足额投 3 元买可乐，找零 0
交易 T8 可乐 1,1,1 投币 选购 出货 找零 完成

# T9 引用不存在的商品
交易 T9 奶茶 5 投币 选购 出货 找零 完成

# T10 用户主动取消：投币后退款
交易 T10 可乐 5 投币 退款
"""


def main(argv):
    if len(argv) > 1 and argv[1] == "--demo":
        src = DEMO
    elif len(argv) > 1:
        with open(argv[1], encoding="utf-8") as f:
            src = f.read()
    else:
        src = sys.stdin.read()
    print(report(run(src)))


if __name__ == "__main__":
    main(sys.argv)
