#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mailcheck.py — 寄递收寄验视安检仲裁器（纯 Python 标准库，单文件）

功能：
  * 收寄单状态机：申报 -> 验视 -> 安检 -> 放行 / 拦截（终态），非法跳转拦截并报告
  * 禁寄物品：验视或安检发现即拦截，并级联复查同寄件人其他在途单
  * 限寄物品：单内超限量报告并定位单号；同寄件人跨单累计超限量报告并级联复查
  * 安检发现物与申报物品不一致（多出 / 缺失 / 数量不同）报告
  * 单号引用不存在收寄、重复安检、重复申报均报告
  * 收寄流与安检流跨流状态延续（先收寄流、后安检流，状态贯穿）

用法：
  python3 mailcheck.py demo                          # 运行内置示例（含全部错误样例）
  python3 mailcheck.py run 名录.csv 收寄.csv 安检.csv  # 处理真实数据，有错误时退出码为 1

输入文件格式（CSV，UTF-8；空行与 # 开头行被忽略；允许表头行）：
  名录.csv：物品名,类别,限量        类别为 禁寄/限寄；禁寄限量留空
      炸药,禁寄,
      锂电池,限寄,2
  收寄.csv：单号,寄件人,物品列表    物品列表为 物品x数量 用 ; 分隔（数量可省，默认 1）
      YD001,张三,锂电池x2;服装x3
  安检.csv：单号,实测重量,发现物列表
      YD001,1.25,锂电池x2;服装x3
"""

import argparse
import csv
import re
import sys
from collections import defaultdict

TERMINAL_STATES = ("放行", "拦截")
HEADER_TOKENS = {"物品名", "单号"}


class Waybill:
    """一张收寄单及其状态机。"""

    def __init__(self, no, sender, items):
        self.no = no
        self.sender = sender
        self.items = dict(items)      # 申报物品 -> 数量
        self.state = "申报"
        self.recheck = False          # 是否被级联复查标记
        self.weight = None            # 安检实测重量
        self.found = None             # 安检发现物 -> 数量


class Engine:
    """跨收寄流/安检流延续状态的仲裁引擎。"""

    def __init__(self, catalog):
        self.catalog = catalog        # 物品名 -> (类别, 限量 or None)
        self.waybills = {}            # 单号 -> Waybill
        self.sender_total = defaultdict(lambda: defaultdict(int))  # 寄件人 -> 物品 -> 跨单累计
        self.errors = []              # (错误类别, 单号, 说明)

    def report(self, kind, no, msg):
        self.errors.append((kind, no, msg))

    def _cascade_recheck(self, sender, exclude_no, reason):
        """级联复查：标记同寄件人其他在途单并报告。"""
        for wb in self.waybills.values():
            if wb.sender == sender and wb.no != exclude_no and wb.state not in TERMINAL_STATES:
                if not wb.recheck:
                    wb.recheck = True
                    self.report("级联复查", wb.no,
                                "寄件人[%s]%s，其在途收寄单需复查" % (sender, reason))

    # ---------------- 收寄流事件 ----------------
    def accept(self, no, sender, items):
        if no in self.waybills:
            self.report("重复申报", no, "收寄单号已存在，拒绝重复收寄")
            return
        wb = Waybill(no, sender, items)
        self.waybills[no] = wb

        # 该寄件人已有被拦截单 -> 新收寄单直接列入级联复查
        if any(w.sender == sender and w.state == "拦截" for w in self.waybills.values()
               if w.no != no):
            wb.recheck = True
            self.report("级联复查", no,
                        "寄件人[%s]已有收寄单被拦截，本单列入复查" % sender)

        # 状态机：申报 -> 验视（验视即对申报物品的名录核查）
        banned = [n for n in items if self.catalog.get(n, (None, None))[0] == "禁寄"]
        if banned:
            wb.state = "拦截"
            self.report("禁寄拦截", no, "验视发现禁寄物品: %s" % ", ".join(banned))
            self._cascade_recheck(sender, no, "的单号[%s]因禁寄被拦截" % no)
            return
        wb.state = "验视"

        # 限寄：单内超限量，报告并定位单号
        for name, qty in items.items():
            cat, limit = self.catalog.get(name, (None, None))
            if cat == "限寄" and limit is not None and qty > limit:
                self.report("限寄超量", no,
                            "限寄物品[%s]申报数量%d超过单件限量%d" % (name, qty, limit))

        # 限寄：同寄件人跨单累计（仅统计通过验视的在途/放行单）
        for name, qty in items.items():
            cat, limit = self.catalog.get(name, (None, None))
            if cat == "限寄" and limit is not None:
                prev = self.sender_total[sender][name]
                total = prev + qty
                self.sender_total[sender][name] = total
                if total > limit and prev <= limit:  # 越过阈值时报告一次
                    self.report("累计超量", no,
                                "寄件人[%s]跨单累计[%s]=%d超过限量%d"
                                % (sender, name, total, limit))
                    self._cascade_recheck(sender, no,
                                          "限寄物品[%s]跨单累计超量" % name)

    # ---------------- 安检流事件 ----------------
    def security(self, no, weight, found):
        wb = self.waybills.get(no)
        if wb is None:
            self.report("单号不存在", no, "安检引用了不存在的收寄单号")
            return
        if wb.state == "拦截":
            self.report("非法跳转", no, "收寄单已拦截（终态），不能再进入安检")
            return
        if wb.state in ("安检", "放行"):
            self.report("重复安检", no,
                        "收寄单当前状态[%s]，重复安检被拒绝" % wb.state)
            return
        if wb.state != "验视":
            self.report("非法跳转", no,
                        "收寄单状态[%s]不能跳转到安检" % wb.state)
            return

        # 状态机：验视 -> 安检
        wb.state = "安检"
        wb.weight = weight
        wb.found = dict(found)

        # 安检发现物 vs 申报物品一致性
        extra = [n for n in found if n not in wb.items]
        missing = [n for n in wb.items if n not in found]
        qty_diff = [n for n in found if n in wb.items and found[n] != wb.items[n]]
        if extra:
            self.report("过检不符", no, "安检发现申报外物品: %s" % ", ".join(extra))
        if missing:
            self.report("过检不符", no, "申报物品未在安检中发现: %s" % ", ".join(missing))
        if qty_diff:
            detail = ", ".join("%s(申报%d/实测%d)" % (n, wb.items[n], found[n])
                               for n in qty_diff)
            self.report("过检不符", no, "物品数量不一致: %s" % detail)

        # 安检环节发现禁寄 -> 拦截并级联复查
        banned = [n for n in found if self.catalog.get(n, (None, None))[0] == "禁寄"]
        if banned:
            wb.state = "拦截"
            self.report("禁寄拦截", no, "安检发现禁寄物品: %s" % ", ".join(banned))
            self._cascade_recheck(wb.sender, no, "的单号[%s]因禁寄被拦截" % no)
            return

        # 状态机：安检 -> 放行
        wb.state = "放行"

    # ---------------- 输出 ----------------
    def print_report(self, out=sys.stdout):
        print("========== 收寄状态 ==========", file=out)
        if not self.waybills:
            print("(无收寄单)", file=out)
        for no in sorted(self.waybills):
            wb = self.waybills[no]
            items = ";".join("%sx%d" % (n, q) for n, q in wb.items.items())
            flags = " [级联复查]" if wb.recheck else ""
            weight = "" if wb.weight is None else " 重量=%s" % wb.weight
            print("单号=%s 寄件人=%s 状态=%s%s 申报=[%s]%s"
                  % (wb.no, wb.sender, wb.state, flags, items, weight), file=out)
        print("", file=out)
        print("========== 错误报告 ==========", file=out)
        if not self.errors:
            print("(无错误)", file=out)
        for i, (kind, no, msg) in enumerate(self.errors, 1):
            print("%2d. [%s] 单号=%s %s" % (i, kind, no, msg), file=out)
        print("", file=out)
        print("合计: 收寄单 %d 张, 错误 %d 条" % (len(self.waybills), len(self.errors)),
              file=out)


# ---------------- 解析 ----------------
def parse_items(text):
    """解析 '物品x数量;物品x数量'，数量可省（默认 1）。支持 x/*/： 分隔数量。"""
    items = {}
    for part in re.split(r"[;；|]", text.strip()):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(.*?)\s*[xX*：:]\s*(\d+)$", part)
        if m:
            name, qty = m.group(1).strip(), int(m.group(2))
        else:
            name, qty = part, 1
        items[name] = items.get(name, 0) + qty
    return items


def _rows(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.reader(fh):
            cells = [c.strip() for c in row]
            if not cells or not any(cells):
                continue
            if cells[0].startswith("#") or cells[0] in HEADER_TOKENS:
                continue
            yield cells


def load_catalog(path):
    catalog = {}
    for cells in _rows(path):
        if len(cells) < 2:
            raise ValueError("名录行格式错误: %s" % cells)
        name, cat = cells[0], cells[1]
        if cat not in ("禁寄", "限寄"):
            raise ValueError("名录类别须为 禁寄/限寄: %s" % cells)
        limit = None
        if len(cells) >= 3 and cells[2]:
            limit = int(cells[2])
        if cat == "限寄" and limit is None:
            raise ValueError("限寄物品必须给限量: %s" % cells)
        catalog[name] = (cat, limit)
    return catalog


def load_acceptance(engine, path):
    for cells in _rows(path):
        if len(cells) < 3:
            raise ValueError("收寄行格式错误: %s" % cells)
        engine.accept(cells[0], cells[1], parse_items(cells[2]))


def load_security(engine, path):
    for cells in _rows(path):
        if len(cells) < 3:
            raise ValueError("安检行格式错误: %s" % cells)
        engine.security(cells[0], cells[1], parse_items(cells[2]))


# ---------------- 内置示例 ----------------
DEMO_CATALOG = """\
# 物品名,类别,限量
炸药,禁寄,
管制刀具,禁寄,
锂电池,限寄,2
白酒,限寄,4
"""

DEMO_ACCEPTANCE = """\
# 单号,寄件人,物品列表
YD001,张三,锂电池x2;服装x3
YD002,张三,锂电池x1
YD003,李四,白酒x5
YD004,王五,书籍x2;管制刀具x1
YD005,王五,服装x1
YD006,赵六,茶叶x1
YD007,赵六,茶叶x1
"""

DEMO_SECURITY = """\
# 单号,实测重量,发现物列表
YD001,1.20,锂电池x2;服装x3
YD002,0.80,锂电池x1;打火机x2
YD003,2.50,白酒x5
YD005,0.50,服装x1
YD006,0.30,茶叶x1
YD006,0.30,茶叶x1
YD999,1.00,服装x1
YD004,1.10,书籍x2
"""


def run_demo():
    import io
    import tempfile
    import os

    def tmp(text):
        fd, path = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    paths = [tmp(DEMO_CATALOG), tmp(DEMO_ACCEPTANCE), tmp(DEMO_SECURITY)]
    try:
        engine = Engine(load_catalog(paths[0]))
        load_acceptance(engine, paths[1])   # 收寄流
        load_security(engine, paths[2])     # 安检流（跨流状态延续）
        engine.print_report()
    finally:
        for p in paths:
            os.unlink(p)


def main(argv=None):
    ap = argparse.ArgumentParser(description="寄递收寄验视安检仲裁器（纯标准库单文件）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo", help="运行内置示例（含全部错误样例）")
    rp = sub.add_parser("run", help="处理真实数据文件")
    rp.add_argument("catalog", help="禁限寄名录 CSV")
    rp.add_argument("acceptance", help="收寄流 CSV")
    rp.add_argument("security", help="安检流 CSV")
    args = ap.parse_args(argv)

    if args.cmd == "demo":
        run_demo()
        return 0
    engine = Engine(load_catalog(args.catalog))
    load_acceptance(engine, args.acceptance)
    load_security(engine, args.security)
    engine.print_report()
    return 1 if engine.errors else 0


if __name__ == "__main__":
    sys.exit(main())
