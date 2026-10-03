#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
narco_audit.py — 麻醉药品“支级”全程审计工具（纯 Python 标准库，单文件）

输入文件指令（每行一条，空行与 # 开头的行被忽略，四种流可任意交错，状态跨流延续）：

    DRUG <药品名> <期初库存>                药品定义流
    RX   <处方编号> <患者> <药品名> <数量>   处方流
    REC  <处方编号> <空安瓿数量>             回收流
    INV  <药品名> <实存数>                  盘点流

审计规则：
    1. 处方超库存            -> 拦截（不发放）并报告
    2. 空安瓿回收数 != 处方数 -> 报告（含未回收/少回收/多回收）
    3. 盘点账物不符          -> 报告，并将该药品级联标记为“异常”，后续涉及该药的操作带级联警告
    4. 处方编号重复          -> 报告并拒绝
    5. 回收引用不存在处方     -> 报告
    6. 盘点引用不存在药品     -> 报告
    7. 同一患者同一药品累计超量（默认 5 支，--patient-limit 可调）-> 报告（仍发放，留痕）
    8. 处方/回收/盘点引用未定义药品、非法数量等 -> 报告

用法：
    python3 narco_audit.py 数据文件.txt [--patient-limit N]
    python3 narco_audit.py --selftest        # 运行内置自测样例
"""

import argparse
import sys
from dataclasses import dataclass

DEFAULT_PATIENT_LIMIT = 5  # 同一患者同一药品累计限量（支）


@dataclass
class Drug:
    name: str
    initial: int
    dispensed: int = 0
    abnormal: bool = False

    @property
    def book(self) -> int:
        """账面库存 = 期初 - 已发放"""
        return self.initial - self.dispensed


@dataclass
class Prescription:
    rid: str
    patient: str
    drug: str
    qty: int
    line: int
    recycled: int = 0


class Auditor:
    def __init__(self, patient_limit: int = DEFAULT_PATIENT_LIMIT):
        self.patient_limit = patient_limit
        self.drugs = {}           # 药品名 -> Drug
        self.prescriptions = {}   # 处方编号 -> Prescription
        self.patient_used = {}    # (患者, 药品) -> 累计发放量
        self.errors = []          # 错误清单
        self.warnings = []        # 级联警告清单

    # ---------- 报告辅助 ----------
    def error(self, line, msg):
        self.errors.append("第%d行 [错误] %s" % (line, msg))

    def warn(self, line, msg):
        self.warnings.append("第%d行 [级联警告] %s" % (line, msg))

    # ---------- 指令解析 ----------
    @staticmethod
    def _to_int(token):
        try:
            value = int(token)
        except ValueError:
            return None
        return value if value > 0 else None

    def process_line(self, line_no, text):
        parts = text.split()
        op = parts[0].upper()
        args = parts[1:]
        handler = {
            "DRUG": self._do_drug,
            "RX": self._do_rx,
            "REC": self._do_rec,
            "INV": self._do_inv,
        }.get(op)
        if handler is None:
            self.error(line_no, "无法识别的指令 '%s'" % parts[0])
            return
        handler(line_no, args)

    # ---------- 药品定义流 ----------
    def _do_drug(self, line, args):
        if len(args) != 2:
            self.error(line, "DRUG 需要 2 个参数：<药品名> <期初库存>")
            return
        name, stock = args[0], self._to_int(args[1])
        if stock is None:
            self.error(line, "药品 '%s' 期初库存非法：'%s'" % (args[0], args[1]))
            return
        if name in self.drugs:
            self.error(line, "药品 '%s' 重复定义" % name)
            return
        self.drugs[name] = Drug(name, stock)

    # ---------- 处方流 ----------
    def _do_rx(self, line, args):
        if len(args) != 4:
            self.error(line, "RX 需要 4 个参数：<处方编号> <患者> <药品名> <数量>")
            return
        rid, patient, drug_name, qty = args[0], args[1], args[2], self._to_int(args[3])
        if qty is None:
            self.error(line, "处方 %s 数量非法：'%s'" % (rid, args[3]))
            return
        if rid in self.prescriptions:
            self.error(line, "处方编号 %s 重复使用（首次出现于第%d行），本次处方被拒绝"
                       % (rid, self.prescriptions[rid].line))
            return
        drug = self.drugs.get(drug_name)
        if drug is None:
            self.error(line, "处方 %s 引用未定义药品 '%s'，已拦截" % (rid, drug_name))
            return
        if drug.abnormal:
            self.warn(line, "处方 %s 涉及已标记异常的药品 '%s'（此前盘点账物不符），请人工复核"
                      % (rid, drug_name))
        if qty > drug.book:
            self.error(line, "处方 %s 超库存被拦截：'%s' 账面库存 %d 支，申请 %d 支"
                       % (rid, drug_name, drug.book, qty))
            return
        key = (patient, drug_name)
        used = self.patient_used.get(key, 0)
        if used + qty > self.patient_limit:
            self.error(line, "患者 '%s' 药品 '%s' 累计 %d 支，超过限量 %d 支（本处方 %d 支，仍发放留痕）"
                       % (patient, drug_name, used + qty, self.patient_limit, qty))
        drug.dispensed += qty
        self.patient_used[key] = used + qty
        self.prescriptions[rid] = Prescription(rid, patient, drug_name, qty, line)

    # ---------- 回收流 ----------
    def _do_rec(self, line, args):
        if len(args) != 2:
            self.error(line, "REC 需要 2 个参数：<处方编号> <空安瓿数量>")
            return
        rid, count = args[0], self._to_int(args[1])
        if count is None:
            self.error(line, "回收记录（处方 %s）数量非法：'%s'" % (rid, args[1]))
            return
        rx = self.prescriptions.get(rid)
        if rx is None:
            self.error(line, "回收引用了不存在的处方编号 %s" % rid)
            return
        rx.recycled += count
        if rx.recycled > rx.qty:
            self.error(line, "处方 %s 空安瓿回收累计 %d 支，超过处方量 %d 支"
                       % (rid, rx.recycled, rx.qty))

    # ---------- 盘点流 ----------
    def _do_inv(self, line, args):
        if len(args) != 2:
            self.error(line, "INV 需要 2 个参数：<药品名> <实存数>")
            return
        name, actual = args[0], self._to_int(args[1])
        if actual is None and args[1] == "0":
            actual = 0
        if actual is None:
            self.error(line, "盘点记录（药品 '%s'）实存数非法：'%s'" % (name, args[1]))
            return
        drug = self.drugs.get(name)
        if drug is None:
            self.error(line, "盘点引用了不存在的药品 '%s'" % name)
            return
        diff = actual - drug.book
        if diff != 0:
            drug.abnormal = True
            self.error(line, "药品 '%s' 账物不符：账面 %d 支，实存 %d 支，差异 %+d 支；"
                       "该药品已级联标记为异常" % (name, drug.book, actual, diff))

    # ---------- 收尾核对 ----------
    def finalize(self):
        for rx in self.prescriptions.values():
            if rx.recycled != rx.qty:
                self.errors.append(
                    "收尾核对 [错误] 处方 %s（患者 '%s'，药品 '%s'）空安瓿账物不符："
                    "应回收 %d 支，实回收 %d 支" % (rx.rid, rx.patient, rx.drug, rx.qty, rx.recycled))

    # ---------- 输出 ----------
    def report(self, out=sys.stdout):
        out.write("=" * 60 + "\n药品状态\n" + "=" * 60 + "\n")
        out.write("%-14s %6s %6s %6s   %s\n" % ("药品", "期初", "已发放", "账面库存", "状态"))
        for drug in self.drugs.values():
            status = "异常(账物不符)" if drug.abnormal else "正常"
            out.write("%-14s %6d %6d %6d   %s\n"
                      % (drug.name, drug.initial, drug.dispensed, drug.book, status))
        out.write("\n" + "=" * 60 + "\n错误与警告报告\n" + "=" * 60 + "\n")
        if not self.errors and not self.warnings:
            out.write("未发现任何异常，账物相符。\n")
        else:
            for item in self.errors:
                out.write(item + "\n")
            for item in self.warnings:
                out.write(item + "\n")
            out.write("\n合计：错误 %d 条，级联警告 %d 条。\n" % (len(self.errors), len(self.warnings)))


def run_text(text, patient_limit):
    auditor = Auditor(patient_limit)
    for line_no, raw in enumerate(text.splitlines(), 1):
        text_line = raw.strip()
        if not text_line or text_line.startswith("#"):
            continue
        auditor.process_line(line_no, text_line)
    auditor.finalize()
    return auditor


SELFTEST_INPUT = """\
# ===== 自测样例：覆盖全部审计规则 =====
DRUG 吗啡注射液 10
DRUG 杜冷丁 5

RX RX001 张三 吗啡注射液 3
RX RX002 李四 吗啡注射液 8
RX RX001 王五 杜冷丁 1
RX RX003 张三 吗啡注射液 4
REC RX001 2
REC RX999 1
RX RX004 赵六 安定注射液 1
INV 吗啡注射液 2
RX RX005 孙七 吗啡注射液 1
INV 安定注射液 5
REC RX003 4
"""


def selftest():
    print("----- 自测输入 -----")
    print(SELFTEST_INPUT)
    print("----- 审计输出 -----")
    auditor = run_text(SELFTEST_INPUT, DEFAULT_PATIENT_LIMIT)
    auditor.report()

    joined = "\n".join(auditor.errors + auditor.warnings)
    checks = [
        ("超库存拦截", "超库存被拦截"),
        ("处方编号重复", "重复使用"),
        ("回收引用不存在处方", "不存在的处方编号 RX999"),
        ("处方引用未定义药品", "未定义药品 '安定注射液'"),
        ("盘点引用不存在药品", "盘点引用了不存在的药品 '安定注射液'"),
        ("患者超量", "超过限量"),
        ("账物不符+级联标记", "账物不符"),
        ("级联警告", "级联警告"),
        ("空安瓿少回收", "应回收 3 支，实回收 2 支"),
        ("空安瓿未回收", "应回收 1 支，实回收 0 支"),
    ]
    print("\n----- 自测断言 -----")
    failed = 0
    for name, keyword in checks:
        ok = keyword in joined
        failed += 0 if ok else 1
        print("[%s] %s" % ("PASS" if ok else "FAIL", name))
    if auditor.drugs["吗啡注射液"].abnormal:
        print("[PASS] 吗啡注射液已被级联标记为异常")
    else:
        failed += 1
        print("[FAIL] 吗啡注射液未被标记为异常")
    print("\n自测结果：%s" % ("全部通过" if failed == 0 else "%d 项失败" % failed))
    return 0 if failed == 0 else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="麻醉药品支级全程审计工具")
    parser.add_argument("input", nargs="?", help="输入数据文件（DRUG/RX/REC/INV 指令流）")
    parser.add_argument("--patient-limit", type=int, default=DEFAULT_PATIENT_LIMIT,
                        help="同一患者同一药品累计限量（支），默认 %d" % DEFAULT_PATIENT_LIMIT)
    parser.add_argument("--selftest", action="store_true", help="运行内置自测样例")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()
    if not args.input:
        parser.error("请提供输入数据文件，或使用 --selftest 运行自测样例")
    with open(args.input, "r", encoding="utf-8") as fh:
        auditor = run_text(fh.read(), args.patient_limit)
    auditor.report()
    return 1 if auditor.errors else 0


if __name__ == "__main__":
    sys.exit(main())
