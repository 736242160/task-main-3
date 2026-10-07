#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vending.py — 售货机交易状态机 + 零钱池动态 + 故障退款联动（纯 Python 标准库，单文件）

用法:
    python3 vending.py [输入文件 ...]        不指定文件时运行内置示例

输入格式（每行一条指令，# 之后为注释，字段空白分隔，列表用英文逗号）:
    商品 <名称> <价格> <库存>                       定义商品（金额为整数，单位:分）
    币种 <面额> <初始零钱数>                        定义零钱池币种
    交易 <TID> <商品> <币,币,...> <状态,状态,...>    发起交易并执行状态序列（币列表用 - 表示无投入）
    状态 <TID> <状态,状态,...>                      为进行中的交易追加状态（与故障事件交错可模拟“故障中途”）
    故障 <机器> <故障|恢复>                         故障 / 恢复事件

状态机:
    投币 -> 选购 -> 出货 -> 找零 -> 完成
    任意非终态 -> 退款（主动取消 / 级联退款）；终态: 完成、退款
    出货 -> 完成 为合法跳转；若仍有应找零钱，完成时自动结算找零

关键机制:
    * 投币先进入“暂存(escrow)”，找零结算时才并入零钱池；退款直接退还暂存币，
      因此级联退款永远精确、不依赖零钱池余量。
    * 找零优先大面额贪心（币数最少、结果可预期、符合真机出币策略）；
      贪心失败时回退精确搜索（DFS+记忆化），保证“只要零钱池存在可行组合就一定能找出”；
      无法组合则报告“找零不足”并级联退款。
    * 级联退款 = 交易作废：退还全部暂存投币；若已出货则回滚库存。
    * 故障事件：立即对所有进行中交易级联退款；故障期间新交易被拦截并报告；恢复后放行。
    * 库存保存在机器层，跨交易延续扣减。
"""

import sys
from collections import Counter, OrderedDict

STATES = ['投币', '选购', '出货', '找零', '完成', '退款']
LEGAL = {
    '投币': {'选购'},
    '选购': {'出货'},
    '出货': {'找零', '完成'},
    '找零': {'完成'},
}
TERMINAL = {'完成', '退款'}


def fmt_coins(coins):
    if not coins:
        return '无'
    coins = sorted(coins, reverse=True)
    return '{}={}分'.format('+'.join(map(str, coins)), sum(coins))


def make_change(pool, amount):
    """从零钱池 pool(Counter) 中组合出 amount。
    先大面额贪心；失败则回退精确搜索。返回币列表或 None。"""
    result, remaining = [], amount
    for denom in sorted(pool, reverse=True):
        take = min(pool[denom], remaining // denom)
        result += [denom] * take
        remaining -= denom * take
    if remaining == 0:
        return result

    denoms = sorted((d for d in pool if pool[d] > 0), reverse=True)
    memo = {}

    def dfs(i, rem):
        if rem == 0:
            return []
        if i == len(denoms) or rem < 0:
            return None
        key = (i, rem)
        if key in memo:
            return memo[key]
        d = denoms[i]
        for t in range(min(pool[d], rem // d), -1, -1):
            rest = dfs(i + 1, rem - d * t)
            if rest is not None:
                memo[key] = [d] * t + rest
                return memo[key]
        memo[key] = None
        return None

    return dfs(0, amount)


class Transaction:
    def __init__(self, tid, product, coins):
        self.tid = tid
        self.product = product
        self.inserted = list(coins)  # 计划投入的币
        self.escrow = []             # 已投币暂存（未入零钱池）
        self.state = None            # 当前状态机位置
        self.status = '进行中'       # 进行中/完成/退款/拦截
        self.change = []             # 已找零
        self.refunded = []           # 已退款
        self.dispensed = False       # 是否已出货
        self.path = []               # 实际执行的状态路径


class Machine:
    def __init__(self):
        self.products = {}           # 名称 -> 价格
        self.stock = {}              # 名称 -> 库存（跨交易延续）
        self.pool = Counter()        # 零钱池：面额 -> 数量
        self.txns = OrderedDict()
        self.faulted = set()         # 故障中的机器
        self.events = []
        self.errors = []
        self._seq = 0

    def error(self, msg):
        self._seq += 1
        self.errors.append('[E%02d] %s' % (self._seq, msg))

    def event(self, msg):
        self.events.append(msg)

    # ---------- 级联退款 ----------
    def cascade_refund(self, txn, reason):
        if txn.status != '进行中':
            return
        txn.refunded = txn.escrow[:]
        txn.escrow = []
        if txn.dispensed:                       # 交易作废，回滚库存
            self.stock[txn.product] += 1
            txn.dispensed = False
        txn.state = '退款'
        txn.status = '退款'
        txn.path.append('退款')
        self.error('交易 %s 级联退款（%s），退款 %s'
                   % (txn.tid, reason, fmt_coins(txn.refunded)))

    # ---------- 找零结算 ----------
    def settle_change(self, txn):
        price = self.products[txn.product]
        due = sum(txn.escrow) - price
        if due < 0:                             # 理论上选购已拦截
            self.error('交易 %s 结算异常：投入 %d 不足价格 %d'
                       % (txn.tid, sum(txn.escrow), price))
            self.cascade_refund(txn, '结算异常')
            return False
        if due > 0:
            combo = make_change(self.pool, due)
            if combo is None:
                self.error('交易 %s 找零不足：需找 %d 分，零钱池无法组合'
                           % (txn.tid, due))
                self.cascade_refund(txn, '找零不足')
                return False
            for c in combo:                     # 零钱池付出找零
                self.pool[c] -= 1
            txn.change = combo
        for c in txn.escrow:                    # 投币并入零钱池（收入）
            self.pool[c] += 1
        txn.escrow = []
        return True

    # ---------- 状态处理 ----------
    def step(self, txn, state):
        if txn.status != '进行中':
            self.error('交易 %s 已终态（%s），状态「%s」被忽略'
                       % (txn.tid, txn.status, state))
            return
        if state not in STATES:
            self.error('交易 %s 未知状态「%s」' % (txn.tid, state))
            return
        if state == '退款':                     # 任意非终态可主动退款
            txn.refunded = txn.escrow[:]
            txn.escrow = []
            if txn.dispensed:
                self.stock[txn.product] += 1
                txn.dispensed = False
            txn.state = '退款'
            txn.status = '退款'
            txn.path.append('退款')
            self.event('交易 %s 主动退款 %s' % (txn.tid, fmt_coins(txn.refunded)))
            return
        prev = txn.state
        if prev is None:
            if state != '投币':
                self.error('交易 %s 非法跳转 开始->%s（首状态须为投币）' % (txn.tid, state))
                self.cascade_refund(txn, '非法跳转')
                return
        elif state not in LEGAL.get(prev, set()):
            self.error('交易 %s 非法跳转 %s->%s' % (txn.tid, prev, state))
            self.cascade_refund(txn, '非法跳转')
            return
        handler = getattr(self, 'do_' + state)
        handler(txn)
        if txn.status == '进行中':
            txn.state = state
            txn.path.append(state)
        elif txn.status == '完成':
            txn.state = '完成'
            txn.path.append('完成')

    def do_投币(self, txn):
        txn.escrow.extend(txn.inserted)
        self.event('交易 %s 投币 %s' % (txn.tid, fmt_coins(txn.inserted)))

    def do_选购(self, txn):
        price = self.products[txn.product]
        if self.stock.get(txn.product, 0) <= 0:
            self.error('交易 %s 商品「%s」缺货' % (txn.tid, txn.product))
            self.cascade_refund(txn, '缺货')
            return
        if sum(txn.escrow) < price:
            self.error('交易 %s 投入不足：已投 %d 分，需 %d 分'
                       % (txn.tid, sum(txn.escrow), price))
            self.cascade_refund(txn, '投入不足')
            return

    def do_出货(self, txn):
        self.stock[txn.product] -= 1
        txn.dispensed = True
        self.event('交易 %s 出货「%s」（剩余库存 %d）'
                   % (txn.tid, txn.product, self.stock[txn.product]))

    def do_找零(self, txn):
        self.settle_change(txn)

    def do_完成(self, txn):
        if txn.escrow:                          # 跳过找零时自动结算
            self.event('交易 %s 完成前自动结算找零' % txn.tid)
            if not self.settle_change(txn):
                return
        txn.status = '完成'

    # ---------- 指令入口 ----------
    def start_txn(self, tid, product, coins, states):
        if tid in self.txns:
            self.error('交易 %s 重复定义，被忽略' % tid)
            return
        txn = Transaction(tid, product, coins)
        self.txns[tid] = txn
        if self.faulted:
            txn.status = '拦截'
            self.error('交易 %s 被拦截：机器故障中（%s）'
                       % (tid, ','.join(sorted(self.faulted))))
            return
        if product not in self.products:
            txn.status = '拦截'
            self.error('交易 %s 引用不存在的商品「%s」' % (tid, product))
            return
        self.run_states(txn, states)

    def continue_txn(self, tid, states):
        txn = self.txns.get(tid)
        if txn is None:
            self.error('状态指令引用不存在的交易 %s' % tid)
            return
        if self.faulted and txn.status == '进行中':
            self.error('交易 %s 状态被拦截：机器故障中' % tid)
            return
        self.run_states(txn, states)

    def run_states(self, txn, states):
        for i, s in enumerate(states):
            self.step(txn, s)
            if txn.status != '进行中' and i < len(states) - 1:
                self.error('交易 %s 已终止，后续状态 %s 被忽略'
                           % (txn.tid, ','.join(states[i + 1:])))
                break

    def on_fault(self, machine, kind):
        if kind == '故障':
            self.faulted.add(machine)
            self.event('机器 %s 故障' % machine)
            for txn in self.txns.values():
                if txn.status == '进行中':
                    self.cascade_refund(txn, '机器 %s 故障' % machine)
        elif kind == '恢复':
            self.faulted.discard(machine)
            self.event('机器 %s 恢复，交易放行' % machine)
        else:
            self.error('故障指令未知类型「%s」' % kind)


def parse_coins(token):
    if token in ('-', ''):
        return []
    return [int(x) for x in token.split(',')]


def run(text):
    m = Machine()
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        kind = parts[0]
        try:
            if kind == '商品':
                _, name, price, stock = parts
                m.products[name] = int(price)
                m.stock[name] = int(stock)
            elif kind == '币种':
                m.pool[int(parts[1])] += int(parts[2])
            elif kind == '交易':
                tid, product = parts[1], parts[2]
                coins = parse_coins(parts[3])
                states = parts[4].split(',') if len(parts) > 4 else []
                m.start_txn(tid, product, coins, states)
            elif kind == '状态':
                m.continue_txn(parts[1], parts[2].split(','))
            elif kind == '故障':
                m.on_fault(parts[1], parts[2])
            else:
                m.error('第 %d 行：无法识别的指令「%s」' % (lineno, kind))
        except (ValueError, IndexError) as exc:
            m.error('第 %d 行解析失败：%s（%s）' % (lineno, line, exc))
    return m


def report(m):
    out = ['===== 交易结果 =====']
    for txn in m.txns.values():
        out.append('%s: 商品=%s 结果=%s | 投入=%s | 找零=%s | 退款=%s | 路径=%s'
                   % (txn.tid, txn.product, txn.status,
                      fmt_coins(txn.inserted), fmt_coins(txn.change),
                      fmt_coins(txn.refunded),
                      '>'.join(txn.path) if txn.path else '-'))
    out.append('===== 零钱池 =====')
    if m.pool:
        for denom in sorted(m.pool, reverse=True):
            out.append('面额 %d 分 x %d' % (denom, m.pool[denom]))
        out.append('合计 %d 分' % sum(d * n for d, n in m.pool.items()))
    else:
        out.append('（空）')
    out.append('===== 剩余库存 =====')
    for name in m.products:
        out.append('%s: %d' % (name, m.stock[name]))
    out.append('===== 事件日志 =====')
    out.extend(m.events or ['（无）'])
    out.append('===== 错误清单 =====')
    out.extend(m.errors or ['（无）'])
    return '\n'.join(out)


DEMO = """\
# ===== 商品与币种定义 =====
商品 可乐 300 2
商品 薯片 150 1
商品 果汁 200 5
币种 100 1
币种 10 3

# ===== 正常交易：足额 / 找零 =====
交易 T1 可乐 100,100,100 投币,选购,出货,找零,完成
交易 T2 可乐 500 投币,选购,出货,找零,完成

# ===== 缺货级联退款（可乐库存已跨交易扣减为 0） =====
交易 T3 可乐 500 投币,选购,出货,找零,完成

# ===== 投入不足级联退款 =====
交易 T4 薯片 100 投币,选购,出货,找零,完成

# ===== 找零不足级联退款（薯片150，投200需找50，池中只有10x3） =====
交易 T5 薯片 200 投币,选购,出货,找零,完成

# ===== 非法跳转拦截（投币后直接出货，跳过选购） =====
交易 T6 果汁 200 投币,出货,找零,完成

# ===== 引用不存在的商品 =====
交易 T7 奶茶 500 投币,选购,出货,找零,完成

# ===== 故障中途：T9 投币选购后机器故障，级联退款 =====
交易 T9 果汁 200 投币,选购
故障 M1 故障
状态 T9 出货,找零,完成

# ===== 故障期间新交易被拦截 =====
交易 T8 果汁 200 投币,选购,出货,找零,完成

# ===== 恢复后交易放行 =====
故障 M1 恢复
交易 T10 果汁 200 投币,选购,出货,找零,完成
"""


def main(argv):
    if len(argv) > 1:
        texts = []
        for path in argv[1:]:
            with open(path, encoding='utf-8') as f:
                texts.append(f.read())
        text = '\n'.join(texts)
    else:
        print('（未指定输入文件，运行内置示例；用法: python3 vending.py <输入文件>）\n')
        text = DEMO
    print(report(run(text)))


if __name__ == '__main__':
    main(sys.argv)
