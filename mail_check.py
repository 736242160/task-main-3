#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""寄递收寄验视安检工具（纯 Python 标准库，单文件）。

输入:
  1) 禁限寄名录文件, 每行: 物品名 类别(禁寄|限寄) 限量
       例: 枪支 禁寄
           白酒 限寄 2
  2) 收寄/安检事件流文件, 每行一个事件(# 开头为注释, 空行跳过):
       ACCEPT  单号 寄件人 物品x数量,物品x数量,...   -> 建单, 状态=申报
       INSPECT 单号                                -> 申报 -> 验视
       CHECK   单号 实测重量 物品x数量,...          -> 验视 -> 安检(- 表示无)
       RELEASE 单号                                -> 安检 -> 放行
  物品数量可省略, 缺省为 1, 如 "茶叶" 等价于 "茶叶x1"。

状态机(合法跳转):
  申报 --INSPECT--> 验视 --CHECK--> 安检 --RELEASE--> 放行
  任一环节命中禁寄/非法跳转 => 拦截(终态)
  被拦截单的同寄件人其他在途单(申报/验视/安检) 级联复查

校验规则:
  - 禁寄物品: 收寄或安检发现即拦截, 并级联复查同寄件人在途单
  - 限寄物品: 单单超限量报告并定位单号
  - 同一寄件人跨单累计限寄物品超限量: 报告并级联复查其其他单
  - 安检发现物与申报不一致(多出/不同/缺少): 报告
  - 单号引用不存在: 报告
  - 同一单重复安检: 报告

用法:
  python3 mail_check.py 名录文件 事件流文件
  python3 mail_check.py --demo          # 运行内置示例并打印错误样例
退出码: 有错误报告时为 1, 否则 0。
"""

import sys
import tempfile
from dataclasses import dataclass, field

IN_TRANSIT = ("申报", "验视", "安检")

# 事件 -> (合法源状态, 目标状态)
TRANSITIONS = {
    "INSPECT": ("申报", "验视"),
    "CHECK": ("验视", "安检"),
    "RELEASE": ("安检", "放行"),
}


def parse_items(text):
    """'白酒x3,茶叶' -> {'白酒': 3, '茶叶': 1}; '-' 或空串 -> {}。"""
    items = {}
    if not text or text.strip() == "-":
        return items
    for token in text.split(","):
        token = token.strip()
        if not token:
            continue
        name, sep, qty_text = token.rpartition("x")
        if name and sep and qty_text.isdigit():
            items[name] = items.get(name, 0) + int(qty_text)
        else:
            items[token] = items.get(token, 0) + 1
    return items


def load_catalog(path):
    catalog = {}
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 2 or parts[1] not in ("禁寄", "限寄"):
                raise ValueError("名录第%d行格式错误(应为: 物品名 禁寄|限寄 [限量]): %s"
                                 % (lineno, raw.rstrip()))
            name, category = parts[0], parts[1]
            if category == "禁寄":
                catalog[name] = ("禁寄", 0)
            else:
                if len(parts) < 3 or not parts[2].isdigit():
                    raise ValueError("名录第%d行限寄物品缺少合法限量: %s"
                                     % (lineno, raw.rstrip()))
                catalog[name] = ("限寄", int(parts[2]))
    return catalog


@dataclass
class Order:
    order_id: str
    sender: str
    declared: dict
    state: str = "申报"
    flags: list = field(default_factory=list)   # 如 ["复查"]
    weight: str = "-"
    found: object = None


class Engine:
    def __init__(self, catalog):
        self.catalog = catalog
        self.orders = {}          # 单号 -> Order
        self.sender_totals = {}   # 寄件人 -> {物品: 跨单累计数量}
        self.errors = []          # (类型, 单号, 详情)

    def report(self, kind, order_id, detail):
        self.errors.append((kind, order_id, detail))

    def cascade(self, sender, reason, exclude=None):
        """对该寄件人其他在途单级联复查。"""
        for order in self.orders.values():
            if (order.sender == sender and order.order_id != exclude
                    and order.state in IN_TRANSIT
                    and "复查" not in order.flags):
                order.flags.append("复查")
                self.report("级联复查", order.order_id,
                            "寄件人%s触发复查: %s" % (sender, reason))

    def intercept(self, order, reason):
        order.state = "拦截"
        self.cascade(order.sender, reason, exclude=order.order_id)

    # ---- 事件处理 ----

    def handle_accept(self, lineno, parts):
        if len(parts) < 4:
            self.report("格式错误", "-", "第%d行 ACCEPT 参数不足" % lineno)
            return
        order_id, sender, items_text = parts[1], parts[2], parts[3]
        if order_id in self.orders:
            self.report("重复收寄", order_id, "单号已存在, 寄件人=%s" % sender)
            return
        items = parse_items(items_text)
        order = Order(order_id=order_id, sender=sender, declared=items)
        self.orders[order_id] = order

        # 累计该寄件人跨单限寄数量(收寄即计入, 拦截单也保留记录)
        totals = self.sender_totals.setdefault(sender, {})
        other_orders = [o for o in self.orders.values()
                        if o.sender == sender and o.order_id != order_id]
        for name, qty in items.items():
            totals[name] = totals.get(name, 0) + qty

        blocked = False
        for name, qty in items.items():
            rule = self.catalog.get(name)
            if rule is None:
                continue
            category, limit = rule
            if category == "禁寄":
                self.report("禁寄物品", order_id,
                            "申报禁寄物品 %sx%d" % (name, qty))
                blocked = True
            elif qty > limit:
                self.report("限寄超量", order_id,
                            "%s 申报%d 限量%d" % (name, qty, limit))
            if category == "限寄" and other_orders and totals[name] > limit:
                self.report("跨单累计超限", order_id,
                            "寄件人%s %s 跨单累计%d 限量%d"
                            % (sender, name, totals[name], limit))
                self.cascade(sender,
                             "%s跨单累计%d超限量%d" % (name, totals[name], limit),
                             exclude=order_id)
        if blocked:
            self.report("收寄拦截", order_id, "含禁寄物品, 直接拦截")
            self.intercept(order, "单号%s含禁寄物品" % order_id)

    def handle_check(self, lineno, parts, order):
        if len(parts) < 4:
            self.report("格式错误", order.order_id,
                        "第%d行 CHECK 参数不足(单号 重量 发现物)" % lineno)
            return
        weight, found_text = parts[2], parts[3]
        found = parse_items(found_text)

        if order.state in ("安检", "放行"):
            self.report("重复安检", order.order_id,
                        "该单已完成安检(状态=%s)" % order.state)
            return

        expected, _ = TRANSITIONS["CHECK"]
        if order.state != expected:
            self.report("非法跳转", order.order_id,
                        "CHECK: %s -> 安检 (要求从%s)" % (order.state, expected))
            self.report("收寄拦截", order.order_id, "非法跳转, 拦截")
            self.intercept(order, "单号%s非法跳转" % order.order_id)
            return

        order.state = "安检"
        order.weight, order.found = weight, found

        # 发现物与申报一致性
        for name, qty in found.items():
            if name not in order.declared:
                self.report("安检不一致", order.order_id,
                            "发现未申报物品 %sx%d" % (name, qty))
            elif order.declared[name] != qty:
                self.report("安检不一致", order.order_id,
                            "%s 申报%d 实测%d"
                            % (name, order.declared[name], qty))
        for name, qty in order.declared.items():
            if name not in found:
                self.report("安检不一致", order.order_id,
                            "申报物品未发现 %sx%d" % (name, qty))

        # 安检发现禁寄物 -> 拦截并级联
        blocked_item = None
        for name, qty in found.items():
            rule = self.catalog.get(name)
            if rule is not None and rule[0] == "禁寄":
                self.report("禁寄物品", order.order_id,
                            "安检发现禁寄物品 %sx%d" % (name, qty))
                blocked_item = name
        if blocked_item is not None:
            self.report("安检拦截", order.order_id,
                        "安检发现禁寄物品 %s" % blocked_item)
            self.intercept(order, "单号%s安检发现禁寄物品%s" % (order.order_id, blocked_item))

    def handle_event(self, lineno, parts):
        event = parts[0]
        if event == "ACCEPT":
            self.handle_accept(lineno, parts)
            return

        if len(parts) < 2:
            self.report("格式错误", "-", "第%d行缺少单号" % lineno)
            return
        order_id = parts[1]
        order = self.orders.get(order_id)
        if order is None:
            self.report("单号不存在", order_id, "事件%s引用了未收寄单号" % event)
            return
        if order.state == "拦截":
            self.report("拦截单操作", order_id, "事件%s作用于已拦截单" % event)
            return

        if event == "CHECK":
            self.handle_check(lineno, parts, order)
            return

        if event not in TRANSITIONS:
            self.report("未知事件", order_id, "第%d行: %s" % (lineno, event))
            return

        expected, target = TRANSITIONS[event]
        if order.state != expected:
            self.report("非法跳转", order_id,
                        "%s: %s -> %s (要求从%s)"
                        % (event, order.state, target, expected))
            self.report("收寄拦截", order_id, "非法跳转, 拦截")
            self.intercept(order, "单号%s非法跳转(%s)" % (order_id, event))
            return
        order.state = target

    def run(self, events_path):
        with open(events_path, encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, 1):
                line = raw.split("#", 1)[0].strip()
                if line:
                    self.handle_event(lineno, line.split())

    def render(self):
        lines = ["===== 收寄状态 =====",
                 "%-8s %-8s %-6s %-6s %-22s %-22s %s"
                 % ("单号", "寄件人", "状态", "标记", "申报", "安检发现", "重量")]
        for order in self.orders.values():
            declared = ",".join("%sx%d" % (n, q) for n, q in order.declared.items()) or "-"
            if order.found is None:
                found = "(未安检)"
            else:
                found = ",".join("%sx%d" % (n, q) for n, q in order.found.items()) or "-"
            lines.append("%-8s %-8s %-6s %-6s %-22s %-22s %s"
                         % (order.order_id, order.sender, order.state,
                            ",".join(order.flags) or "-", declared, found, order.weight))
        lines.append("")
        lines.append("===== 错误报告 (共%d条) =====" % len(self.errors))
        for index, (kind, order_id, detail) in enumerate(self.errors, 1):
            lines.append("%2d. [%-10s] 单号=%-6s %s" % (index, kind, order_id, detail))
        return "\n".join(lines)


DEMO_CATALOG = """\
枪支 禁寄
毒品 禁寄
白酒 限寄 2
打火机 限寄 2
锂电池 限寄 2
"""

DEMO_EVENTS = """\
# 1. 张三两单: 单单超限 + 跨单累计超限, 触发级联复查
ACCEPT  D001 张三 白酒x3
ACCEPT  D002 张三 白酒x1
# 2. 李四夹带禁寄品: 收寄即拦截, 其在途单 D004 级联复查
ACCEPT  D004 李四 茶叶x2
ACCEPT  D003 李四 毒品x1
# 3. D001 正常流转, 但安检多出未申报的香烟 -> 不一致
INSPECT D001
CHECK   D001 1.5 白酒x3,香烟x1
RELEASE D001
# 4. 已放行单重复安检
CHECK   D001 1.6 白酒x3,香烟x1
# 5. 引用不存在的单号
CHECK   D009 0.5 茶叶x1
# 6. 王五跳过验视直接安检: 非法跳转 -> 拦截
ACCEPT  D005 王五 打火机x1
CHECK   D005 0.3 打火机x1
# 7. 被复查单走完正常流程
INSPECT D004
CHECK   D004 0.8 茶叶x2
RELEASE D004
# 8. 赵六正常单(对照)
ACCEPT  D006 赵六 锂电池x1
INSPECT D006
CHECK   D006 0.2 锂电池x1
RELEASE D006
"""


def run_demo():
    with tempfile.TemporaryDirectory() as tmp:
        cat_path = tmp + "/catalog.txt"
        evt_path = tmp + "/events.txt"
        with open(cat_path, "w", encoding="utf-8") as fh:
            fh.write(DEMO_CATALOG)
        with open(evt_path, "w", encoding="utf-8") as fh:
            fh.write(DEMO_EVENTS)
        print("########## 名录文件 ##########")
        print(DEMO_CATALOG, end="")
        print("########## 事件流文件 ##########")
        print(DEMO_EVENTS, end="")
        engine = Engine(load_catalog(cat_path))
        engine.run(evt_path)
        print(engine.render())


def main(argv):
    if len(argv) == 2 and argv[1] == "--demo":
        run_demo()
        return 0
    if len(argv) != 3:
        print(__doc__)
        return 2
    engine = Engine(load_catalog(argv[1]))
    engine.run(argv[2])
    print(engine.render())
    return 1 if engine.errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
