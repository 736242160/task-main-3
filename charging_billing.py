#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
charging_billing.py — 充电会话计费工具（纯 Python 标准库，单文件）

用法:
    python3 charging_billing.py input.json     # 从文件读入
    python3 charging_billing.py < input.json   # 从 stdin 读入

输入 JSON 结构:
{
  "piles":    [{"name": "P1", "power_kw": 60}, ...],
  "periods":  [{"start": "2026-10-01 00:00", "end": "2026-10-01 08:00",
                "type": "谷", "price": 0.30}, ...],
  "charging": [{"time": "2026-10-01 07:00", "pile": "P1", "plate": "沪A00001",
                "action": "启动"|"停止", "energy_kwh": 40, "occupy_min": 180}, ...],
  "faults":   [{"time": "2026-10-01 11:00", "pile": "P2",
                "action": "故障"|"恢复"}, ...]
}

输出 JSON: {"sessions": [...], "errors": [...]}
  sessions — 每个已关闭会话的完整结算单（含跨时段分摊、预扣/实扣级联）
  errors   — 所有被拦截的非法事件（含时间、桩、类型、原因）

================================================================
自定计费规则及理由
================================================================
1. 跨时段电量分摊: 假设充电功率恒定, 电量按会话时长与各时段的
   重叠时长比例分摊, 分别乘各时段单价。理由: 无功率曲线数据时,
   恒功率是最不偏倚的假设, 且分摊结果对会话起止时刻敏感、可复算。

2. 启动预扣: 预扣额 = 功率上限 × 1小时 × 启动时刻单价 × 120%。
   理由: 至少覆盖一小时满功率充电的费用; 20% 余量用于对冲会话
   跨入峰时段导致的单价上涨, 降低停止时余额不足的概率。
   停止时实扣 = 电费 + 占桩费, 与预扣级联: 多退少补。

3. 超时占桩费: 充电所需时间 = 电量 / 功率上限; 占用时长超出
   "充电时间 + 15 分钟宽限" 后, 按 0.5 元/分钟计, 单次封顶 120 元。
   理由: 15 分钟宽限覆盖正常拔枪离场; 超时计费抑制充满不走;
   封顶避免长时间占位产生争议性巨额账单。

4. 故障处理: 故障时刻自动结算进行中的会话, 估算电量 =
   功率上限 × 已充电时长, 免收占桩费(故障非用户责任), 与预扣
   级联多退少补。故障期间新会话启动一律拦截; 恢复后解除。
================================================================
"""

import json
import sys
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

TIME_FMT = "%Y-%m-%d %H:%M"
CENT = Decimal("0.01")
KWH_Q = Decimal("0.001")

# ---- 规则常量(见模块 docstring) ----
PRE_DEDUCT_HOURS = Decimal("1")        # 预扣估算充电时长
PRE_DEDUCT_RATIO = Decimal("1.2")      # 预扣比例 120%
GRACE_MINUTES = Decimal("15")          # 占桩免费宽限
OCCUPANCY_PER_MIN = Decimal("0.5")     # 超时占桩单价(元/分钟)
OCCUPANCY_CAP = Decimal("120")         # 占桩费单次封顶(元)


def D(x):
    return Decimal(str(x))


def money(x):
    return D(x).quantize(CENT, rounding=ROUND_HALF_UP)


def jm(x):
    """Decimal -> JSON 可序列化的两位小数浮点。"""
    return float(money(x))


def parse_time(s):
    return datetime.strptime(s, TIME_FMT)


def fmt(t):
    return t.strftime(TIME_FMT)


class Period:
    def __init__(self, start, end, ptype, price):
        self.start = start
        self.end = end
        self.type = ptype
        self.price = D(price)


class Session:
    """会话状态机: 充电中 -> (正常停止 | 故障结算)"""

    def __init__(self, pile, plate, start, pre_deduct):
        self.pile = pile
        self.plate = plate
        self.start = start
        self.pre_deduct = money(pre_deduct)
        self.state = "充电中"


def price_at(periods, t):
    for p in periods:
        if p.start <= t < p.end:
            return p.price
    return None


def split_energy(periods, t0, t1, energy):
    """把 energy 按 [t0,t1) 与各时段的重叠时长比例分摊。
    返回 (items, uncovered_seconds); items = [(period, kwh, seconds)]"""
    total = (t1 - t0).total_seconds()
    items = []
    if total <= 0:
        return items, 0.0
    covered = 0.0
    for p in periods:
        lo, hi = max(t0, p.start), min(t1, p.end)
        if lo < hi:
            sec = (hi - lo).total_seconds()
            kwh = D(energy) * D(sec) / D(total)
            items.append((p, kwh, sec))
            covered += sec
    return items, total - covered


def occupancy_fee(power_kw, energy_kwh, occupy_min):
    """返回 (charging_min, overtime_min, fee)。免占桩费时 fee=0。"""
    if power_kw <= 0:
        return 0.0, 0.0, Decimal("0")
    charging_min = float(D(energy_kwh) / D(power_kw) * 60)
    overtime = max(0.0, float(occupy_min) - charging_min - float(GRACE_MINUTES))
    fee = min(D(str(overtime)) * OCCUPANCY_PER_MIN, OCCUPANCY_CAP)
    return charging_min, overtime, money(fee)


def settle_session(sess, end, end_reason, energy_kwh, occupy_min,
                   periods, piles, errors, waive_occupancy=False):
    """停止/故障统一结算: 跨时段电费 + 占桩费, 与预扣级联多退少补。"""
    power = piles[sess.pile]
    items, uncovered = split_energy(periods, sess.start, end, energy_kwh)
    breakdown = []
    energy_fee = Decimal("0")
    for p, kwh, _sec in items:
        fee = money(kwh * p.price)
        energy_fee += fee
        breakdown.append({
            "period_type": p.type, "price": float(p.price),
            "kwh": float(D(kwh).quantize(KWH_Q, rounding=ROUND_HALF_UP)),
            "fee": jm(fee),
        })
    if uncovered > 0:
        errors.append({
            "time": fmt(end), "pile": sess.pile, "type": "时段未覆盖",
            "detail": "会话有 %d 秒未被任何时段定义覆盖, 该部分按 0 元计费"
                      % int(uncovered),
        })
    if waive_occupancy:
        charging_min = float(D(energy_kwh) / power * 60) if power > 0 else 0.0
        overtime, occ_fee = 0.0, Decimal("0")
    else:
        charging_min, overtime, occ_fee = occupancy_fee(power, energy_kwh, occupy_min)
    actual = money(energy_fee + occ_fee)
    diff = money(actual - sess.pre_deduct)
    sess.state = "已停止" if end_reason == "正常停止" else "故障结算"
    return {
        "pile": sess.pile, "plate": sess.plate,
        "start": fmt(sess.start), "end": fmt(end),
        "end_reason": end_reason, "status": sess.state,
        "energy_kwh": float(D(energy_kwh).quantize(KWH_Q, rounding=ROUND_HALF_UP)),
        "breakdown": breakdown,
        "energy_fee": jm(energy_fee),
        "occupancy": {
            "occupy_min": occupy_min,
            "charging_min": round(charging_min, 1),
            "overtime_min": round(overtime, 1),
            "fee": jm(occ_fee),
            "waived": waive_occupancy,
        },
        "pre_deduct": jm(sess.pre_deduct),
        "actual_total": jm(actual),
        "settlement": ("补扣 %.2f 元" % diff) if diff > 0
                      else ("退还 %.2f 元" % -diff),
    }


def run(data):
    errors = []
    sessions_report = []

    # ---- 装载桩定义 ----
    piles = {}
    for p in data.get("piles", []):
        piles[p["name"]] = D(p["power_kw"])

    # ---- 装载时段并检查重叠 ----
    periods = []
    for p in data.get("periods", []):
        periods.append(Period(parse_time(p["start"]), parse_time(p["end"]),
                              p["type"], p["price"]))
    periods.sort(key=lambda p: p.start)
    for a, b in zip(periods, periods[1:]):
        if b.start < a.end:
            errors.append({
                "time": fmt(b.start), "pile": "-", "type": "时段配置重叠",
                "detail": "时段 [%s,%s) 与 [%s,%s) 重叠, 重叠部分按先定义者计费"
                          % (fmt(a.start), fmt(a.end), fmt(b.start), fmt(b.end)),
            })

    # ---- 合并充电流与故障流, 按时间排序(跨流状态延续) ----
    events = []
    for i, e in enumerate(data.get("charging", [])):
        events.append((parse_time(e["time"]), i, "charging", e))
    base = len(events)
    for i, e in enumerate(data.get("faults", [])):
        events.append((parse_time(e["time"]), base + i, "fault", e))
    events.sort(key=lambda x: (x[0], x[1]))

    active = {}        # pile -> Session (同一桩同时只允许一个会话)
    faulted = set()    # 故障中的桩

    def err(t, pile, etype, detail):
        errors.append({"time": fmt(t), "pile": pile,
                       "type": etype, "detail": detail})

    for t, _seq, kind, e in events:
        pile = e.get("pile", "?")

        # 引用不存在的桩
        if pile not in piles:
            err(t, pile, "桩不存在", "事件引用了未定义的充电桩 '%s'" % pile)
            continue

        if kind == "fault":
            action = e.get("action")
            if action == "故障":
                if pile in faulted:
                    err(t, pile, "重复故障", "桩已处于故障状态")
                    continue
                faulted.add(pile)
                sess = active.pop(pile, None)
                if sess is not None:
                    # 故障中断: 按 功率×已充时长 估算电量自动结算, 免占桩费
                    hours = D((t - sess.start).total_seconds()) / D(3600)
                    est_energy = piles[pile] * hours
                    sessions_report.append(settle_session(
                        sess, t, "故障结算", est_energy, 0,
                        periods, piles, errors, waive_occupancy=True))
            elif action == "恢复":
                if pile not in faulted:
                    err(t, pile, "非法恢复", "桩未处于故障状态, 恢复事件被拦截")
                    continue
                faulted.discard(pile)
            else:
                err(t, pile, "未知故障动作", "action 必须为 故障/恢复")
            continue

        # ---- 充电流事件 ----
        action = e.get("action")
        plate = e.get("plate", "?")

        if action == "启动":
            if pile in faulted:
                err(t, pile, "故障期间启动",
                    "桩处于故障状态, 车牌 %s 的启动请求被拦截" % plate)
                continue
            if pile in active:
                err(t, pile, "同桩并发会话",
                    "桩已有车牌 %s 的活动会话, 车牌 %s 的启动被拒绝"
                    % (active[pile].plate, plate))
                continue
            price = price_at(periods, t)
            if price is None:
                err(t, pile, "时段未覆盖",
                    "启动时刻不属于任何时段, 无法确定预扣单价")
                continue
            pre = piles[pile] * PRE_DEDUCT_HOURS * price * PRE_DEDUCT_RATIO
            active[pile] = Session(pile, plate, t, pre)
        elif action == "停止":
            sess = active.get(pile)
            if sess is None:
                note = " (该会话可能已因故障自动结算)" if pile in faulted else ""
                err(t, pile, "会话不存在",
                    "车牌 %s 的停止事件没有对应的活动会话%s" % (plate, note))
                continue
            if sess.plate != plate:
                err(t, pile, "车牌不匹配",
                    "停止车牌 %s 与活动会话车牌 %s 不符, 事件被拦截"
                    % (plate, sess.plate))
                continue
            active.pop(pile)
            sessions_report.append(settle_session(
                sess, t, "正常停止", D(e.get("energy_kwh", 0)),
                e.get("occupy_min", 0), periods, piles, errors))
        else:
            err(t, pile, "非法状态跳转",
                "会话状态机仅允许 启动->充电->停止, 收到未知动作 '%s'" % action)

    # 流结束时仍未关闭的会话: 报告悬挂
    for pile, sess in sorted(active.items()):
        errors.append({
            "time": fmt(sess.start), "pile": pile, "type": "会话未关闭",
            "detail": "车牌 %s 的会话在事件流结束时仍处于充电中" % sess.plate,
        })

    return {"sessions": sessions_report, "errors": errors}


def main(argv):
    if len(argv) > 1 and argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    if len(argv) > 1:
        with open(argv[1], encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = json.load(sys.stdin)
    json.dump(run(data), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
