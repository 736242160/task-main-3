#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""计量器具检定状态链管理工具（纯 Python 标准库，单文件）。

输入为行式 DSL（UTF-8，空白分隔，# 之后为注释，空行忽略）：

    器具 <编号> <类型:压力表|天平> <检定周期月数> [批次]
    检定 <编号> <合格|不合格> <新周期月数>
    使用 <编号> <启用|停用|送修>
    到期 <编号>

事件按行顺序处理，状态跨流延续；可传多个文件，按给定顺序拼接处理。

用法：
    python3 metrology.py 输入.txt [更多文件...]
    cat 输入.txt | python3 metrology.py -

退出码：0 无错误；1 有错误报告；2 用法错误。
"""

import sys

VALID_TYPES = ("压力表", "天平")
STATES = ("在用", "到期", "检定", "停用", "送修", "复役")


class Instrument:
    def __init__(self, iid, itype, cycle, batch):
        self.iid = iid
        self.itype = itype
        self.cycle = cycle          # 当前生效检定周期（月）
        self.batch = batch
        self.state = "在用"          # 在用/到期/检定/停用/送修/复役
        self.expired = False         # 已到期未检定标记（停用/送修期间也记录）
        self.unqualified = False     # 最近一次检定不合格且未复检合格
        self.last_event = "器具"     # 上一个事件种类，用于重复检定判定


class Engine:
    def __init__(self):
        self.instruments = {}
        self.errors = []   # [(loc, msg)] 错误报告（含非法跳转拦截）
        self.actions = []  # [(loc, msg)] 级联/强制处置记录

    def error(self, loc, msg):
        self.errors.append((loc, msg))

    def action(self, loc, msg):
        self.actions.append((loc, msg))

    # ---- 级联 ----
    def _cascade_recheck(self, loc, src):
        """检定不合格：同类型同批次器具级联复查（转入检定态）。"""
        for other in self.instruments.values():
            if other is src:
                continue
            if other.itype == src.itype and other.batch == src.batch \
                    and other.state in ("在用", "到期"):
                other.state = "检定"
                self.action(loc, "级联复查：%s（同类型%s/同批次%s）转入检定"
                            % (other.iid, src.itype, src.batch))

    def _cascade_disable(self, loc, src):
        """到期未检仍启用：同类型同批次在用器具级联停用。"""
        for other in self.instruments.values():
            if other is src:
                continue
            if other.itype == src.itype and other.batch == src.batch \
                    and other.state == "在用":
                other.state = "停用"
                self.action(loc, "级联停用：%s（同类型%s/同批次%s）停用"
                            % (other.iid, src.itype, src.batch))

    # ---- 事件：器具定义 ----
    def do_define(self, loc, iid, itype, cycle, batch):
        if iid in self.instruments:
            self.error(loc, "重复定义器具：%s" % iid)
            return
        if itype not in VALID_TYPES:
            self.error(loc, "未知器具类型：%s（应为 %s）"
                       % (itype, "/".join(VALID_TYPES)))
            return
        self.instruments[iid] = Instrument(iid, itype, cycle, batch)

    # ---- 事件：检定 ----
    def do_verify(self, loc, iid, result, new_cycle):
        inst = self.instruments.get(iid)
        if inst is None:
            self.error(loc, "引用不存在的器具：%s" % iid)
            return
        if inst.last_event == "检定":
            self.error(loc, "重复检定：%s 上一次事件已是检定，中间无使用/到期事件" % iid)
        if inst.state == "停用":
            self.error(loc, "停用期间检定：%s 当前状态为停用" % iid)
        inst.cycle = new_cycle  # 检定合格后新周期生效（不合格也记录申报值，便于追溯）
        if result == "合格":
            inst.unqualified = False
            inst.expired = False
            if inst.state == "送修":
                inst.state = "复役"
                self.action(loc, "%s 送修后复检合格，转入复役（待启用）" % iid)
            elif inst.state in ("检定", "到期"):
                inst.state = "在用"
            # 在用/停用/复役：状态保持，仅新周期生效
        else:  # 不合格
            inst.unqualified = True
            if inst.state in ("在用", "到期", "检定", "复役"):
                inst.state = "停用"
                self.action(loc, "%s 检定不合格，强制停用" % iid)
            self._cascade_recheck(loc, inst)
        inst.last_event = "检定"

    # ---- 事件：使用 ----
    def do_use(self, loc, iid, op):
        inst = self.instruments.get(iid)
        if inst is None:
            self.error(loc, "引用不存在的器具：%s" % iid)
            return
        if op == "启用":
            if inst.unqualified:
                self.error(loc, "检定不合格后使用被拦截：%s 须复检合格后方可启用" % iid)
            elif inst.state == "送修":
                self.error(loc, "送修后未复检启用被拦截：%s" % iid)
            elif inst.state == "检定":
                self.error(loc, "非法跳转：%s 检定/复查期间不能启用" % iid)
            elif inst.state == "到期" or inst.expired:
                self.error(loc, "到期后未检定仍启用：%s，已强制停用并级联停用" % iid)
                inst.state = "停用"
                inst.expired = True
                self._cascade_disable(loc, inst)
            elif inst.state in ("停用", "复役"):
                inst.state = "在用"
            else:  # 在用
                self.error(loc, "非法跳转：%s 重复启用" % iid)
        elif op == "停用":
            if inst.state in ("在用", "到期", "复役"):
                inst.state = "停用"
            elif inst.state == "停用":
                self.error(loc, "非法跳转：%s 重复停用" % iid)
            else:
                self.error(loc, "非法跳转：%s %s状态下不能停用" % (iid, inst.state))
        elif op == "送修":
            if inst.state in ("在用", "到期", "停用", "复役"):
                inst.state = "送修"
            elif inst.state == "送修":
                self.error(loc, "非法跳转：%s 重复送修" % iid)
            else:
                self.error(loc, "非法跳转：%s 检定期间不能送修" % iid)
        inst.last_event = "使用"

    # ---- 事件：到期 ----
    def do_expire(self, loc, iid):
        inst = self.instruments.get(iid)
        if inst is None:
            self.error(loc, "引用不存在的器具：%s" % iid)
            return
        if inst.state == "到期":
            self.error(loc, "重复到期：%s" % iid)
        elif inst.state in ("在用", "复役"):
            inst.state = "到期"
            inst.expired = True
        else:  # 停用/送修/检定期间到期：记录逾期标记，启用时拦截
            inst.expired = True
            self.action(loc, "%s 在%s状态下到期，已记录逾期标记" % (iid, inst.state))
        inst.last_event = "到期"

    # ---- 行解析与分发 ----
    def feed_line(self, loc, line):
        line = line.split("#", 1)[0].strip()
        if not line:
            return
        parts = line.split()
        kind, args = parts[0], parts[1:]
        try:
            if kind == "器具":
                if len(args) not in (3, 4):
                    raise ValueError("器具 需要 3~4 个参数：编号 类型 周期月数 [批次]")
                iid, itype, cycle_s = args[0], args[1], args[2]
                batch = args[3] if len(args) == 4 else "默认"
                cycle = int(cycle_s)
                if cycle <= 0:
                    raise ValueError("检定周期须为正整数月数：%s" % cycle_s)
                self.do_define(loc, iid, itype, cycle, batch)
            elif kind == "检定":
                if len(args) != 3:
                    raise ValueError("检定 需要 3 个参数：编号 合格|不合格 新周期月数")
                iid, result, cycle_s = args
                if result not in ("合格", "不合格"):
                    raise ValueError("检定结果须为 合格/不合格：%s" % result)
                cycle = int(cycle_s)
                if cycle <= 0:
                    raise ValueError("新周期须为正整数月数：%s" % cycle_s)
                self.do_verify(loc, iid, result, cycle)
            elif kind == "使用":
                if len(args) != 2:
                    raise ValueError("使用 需要 2 个参数：编号 启用|停用|送修")
                if args[1] not in ("启用", "停用", "送修"):
                    raise ValueError("使用状态须为 启用/停用/送修：%s" % args[1])
                self.do_use(loc, args[0], args[1])
            elif kind == "到期":
                if len(args) != 1:
                    raise ValueError("到期 需要 1 个参数：编号")
                self.do_expire(loc, args[0])
            else:
                raise ValueError("未知指令：%s（应为 器具/检定/使用/到期）" % kind)
        except ValueError as exc:
            self.error(loc, "解析失败：%s" % exc)

    # ---- 输出 ----
    def report(self, out):
        w = out.write
        w("=== 器具状态 ===\n")
        if not self.instruments:
            w("（无器具）\n")
        header = ("编号", "类型", "批次", "周期(月)", "状态", "标记")
        rows = []
        for inst in self.instruments.values():
            flags = []
            if inst.expired:
                flags.append("逾期未检")
            if inst.unqualified:
                flags.append("检定不合格")
            rows.append((inst.iid, inst.itype, inst.batch,
                         str(inst.cycle), inst.state, ",".join(flags) or "-"))
        widths = [max(len(str(r[i])) for r in [header] + rows) for i in range(6)]
        for r in [header] + rows:
            w("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)).rstrip() + "\n")
        w("\n=== 处置记录（级联/强制） ===\n")
        if not self.actions:
            w("（无）\n")
        for loc, msg in self.actions:
            w("%s: %s\n" % (loc, msg))
        w("\n=== 错误报告 ===\n")
        if not self.errors:
            w("（无错误）\n")
        for i, (loc, msg) in enumerate(self.errors, 1):
            w("%d. %s: %s\n" % (i, loc, msg))
        w("\n汇总：器具 %d 台，处置 %d 条，错误 %d 条\n"
          % (len(self.instruments), len(self.actions), len(self.errors)))


def main(argv):
    paths = argv[1:]
    if not paths:
        sys.stderr.write(__doc__)
        return 2
    engine = Engine()
    for path in paths:
        if path == "-":
            name, fh = "<stdin>", sys.stdin
            lines = fh.read().splitlines()
        else:
            name = path
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    lines = fh.read().splitlines()
            except OSError as exc:
                sys.stderr.write("无法读取 %s：%s\n" % (path, exc))
                return 2
        for lineno, line in enumerate(lines, 1):
            engine.feed_line("%s:%d" % (name, lineno), line)
    engine.report(sys.stdout)
    return 1 if engine.errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
