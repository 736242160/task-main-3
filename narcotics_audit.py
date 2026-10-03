#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
麻醉药品账物核对工具（纯标准库，单文件）

输入格式（文本行，# 开头为注释，空行忽略；字段以空白分隔）：
    DRUG  <药品名称> <初始库存>          药品定义
    RX    <处方编号> <患者> <药品> <数量>  处方流
    REC   <处方编号> <空安瓿数量>          回收流
    COUNT <药品名称> <实存数>             盘点流

规则：
    1. 处方数量超过当前账面库存 -> 拦截该处方并报告，不发放、不登记
    2. 空安瓿回收数量与处方数量不一致（含到处方流结束仍未回收）-> 报告
    3. 盘点实存与账面（期初-已发放）不符 -> 报告，并将该药品级联标记为异常
    4. 处方编号重复使用 -> 报告并忽略后者
    5. 回收引用不存在的处方（含被拦截的处方）-> 报告
    6. 盘点引用不存在的药品 -> 报告
    7. 同一患者同一药品累计超过 PATIENT_DRUG_LIMIT -> 报告（仍发放，属预警）
    8. 所有流按输入顺序依次处理，状态跨流延续

用法：
    python3 narcotics_audit.py <输入文件>
    python3 narcotics_audit.py demo      # 运行内置自测样例
"""

import sys

# 同一患者同一药品累计允许上限（自定规则，可按需调整）
PATIENT_DRUG_LIMIT = 30


class Auditor:
    def __init__(self):
        # 药品名 -> {initial, dispensed, actual(None=未盘点), abnormal}
        self.drugs = {}
        # 处方编号 -> {patient, drug, qty, recovered(None=未回收)}
        self.prescriptions = {}
        # (患者, 药品) -> 累计发放量
        self.patient_totals = {}
        # 错误清单: (行号, 类型, 描述)
        self.errors = []

    # ---------- 内部工具 ----------
    def _report(self, lineno, kind, msg, drug=None):
        self.errors.append((lineno, kind, msg))
        if drug is not None and drug in self.drugs:
            self.drugs[drug]["abnormal"] = True  # 级联标记异常

    @staticmethod
    def _to_int(text):
        try:
            value = int(text)
        except ValueError:
            return None
        return value if value >= 0 else None

    # ---------- 各流处理 ----------
    def handle_drug(self, lineno, name, stock_text):
        stock = self._to_int(stock_text)
        if stock is None:
            self._report(lineno, "格式错误", "药品 %s 初始库存不是非负整数: %r" % (name, stock_text))
            return
        if name in self.drugs:
            self._report(lineno, "重复定义", "药品 %s 重复定义，保留首次定义" % name, drug=name)
            return
        self.drugs[name] = {"initial": stock, "dispensed": 0, "actual": None, "abnormal": False}

    def handle_rx(self, lineno, rx_id, patient, drug, qty_text):
        qty = self._to_int(qty_text)
        if qty is None or qty == 0:
            self._report(lineno, "格式错误", "处方 %s 数量不是正整数: %r" % (rx_id, qty_text))
            return
        if drug not in self.drugs:
            self._report(lineno, "药品不存在", "处方 %s 引用未定义药品 %s，已拦截" % (rx_id, drug))
            return
        if rx_id in self.prescriptions:
            self._report(lineno, "处方编号重复",
                         "处方编号 %s 重复使用（患者 %s），已忽略该处方" % (rx_id, patient), drug=drug)
            return
        stock = self.drugs[drug]["initial"] - self.drugs[drug]["dispensed"]
        if qty > stock:
            self._report(lineno, "超库存拦截",
                         "处方 %s（患者 %s）申请 %s %d 支，当前库存仅 %d 支，已拦截"
                         % (rx_id, patient, drug, qty, stock), drug=drug)
            return
        self.drugs[drug]["dispensed"] += qty
        self.prescriptions[rx_id] = {"patient": patient, "drug": drug, "qty": qty, "recovered": None}
        key = (patient, drug)
        self.patient_totals[key] = self.patient_totals.get(key, 0) + qty
        if self.patient_totals[key] > PATIENT_DRUG_LIMIT:
            self._report(lineno, "患者超量预警",
                         "患者 %s 累计领取 %s 达 %d 支，超过上限 %d 支"
                         % (patient, drug, self.patient_totals[key], PATIENT_DRUG_LIMIT), drug=drug)

    def handle_rec(self, lineno, rx_id, count_text):
        count = self._to_int(count_text)
        if count is None:
            self._report(lineno, "格式错误", "回收记录 %s 空安瓿数量不是非负整数: %r" % (rx_id, count_text))
            return
        rx = self.prescriptions.get(rx_id)
        if rx is None:
            self._report(lineno, "回收处方不存在",
                         "回收记录引用不存在（或已被拦截）的处方 %s" % rx_id)
            return
        if rx["recovered"] is not None:
            self._report(lineno, "重复回收",
                         "处方 %s 已回收 %d 支，本次重复回收 %d 支已忽略"
                         % (rx_id, rx["recovered"], count), drug=rx["drug"])
            return
        rx["recovered"] = count
        if count != rx["qty"]:
            self._report(lineno, "回收数量不符",
                         "处方 %s（患者 %s，%s）应回收空安瓿 %d 支，实收 %d 支，差 %d 支"
                         % (rx_id, rx["patient"], rx["drug"], rx["qty"], count, rx["qty"] - count),
                         drug=rx["drug"])

    def handle_count(self, lineno, drug, actual_text):
        actual = self._to_int(actual_text)
        if actual is None:
            self._report(lineno, "格式错误", "盘点 %s 实存数不是非负整数: %r" % (drug, actual_text))
            return
        if drug not in self.drugs:
            self._report(lineno, "盘点药品不存在", "盘点引用未定义药品 %s" % drug)
            return
        self.drugs[drug]["actual"] = actual
        book = self.drugs[drug]["initial"] - self.drugs[drug]["dispensed"]
        if actual != book:
            self._report(lineno, "账物不符",
                         "药品 %s 账面库存 %d 支，实盘 %d 支，差异 %d 支"
                         % (drug, book, actual, actual - book), drug=drug)

    # ---------- 主流程 ----------
    def process_line(self, lineno, line):
        line = line.strip()
        if not line or line.startswith("#"):
            return
        parts = line.split()
        cmd, args = parts[0].upper(), parts[1:]
        handlers = {"DRUG": (self.handle_drug, 2), "RX": (self.handle_rx, 4),
                    "REC": (self.handle_rec, 2), "COUNT": (self.handle_count, 2)}
        if cmd not in handlers:
            self._report(lineno, "格式错误", "未知指令 %r" % parts[0])
            return
        handler, arity = handlers[cmd]
        if len(args) != arity:
            self._report(lineno, "格式错误", "%s 需要 %d 个字段，实际 %d 个: %s"
                         % (cmd, arity, len(args), line))
            return
        handler(lineno, *args)

    def finalize(self):
        # 流结束后仍未回收的处方，按回收 0 支处理并报告
        for rx_id, rx in self.prescriptions.items():
            if rx["recovered"] is None:
                self._report("-", "回收数量不符",
                             "处方 %s（患者 %s，%s）应回收空安瓿 %d 支，至流程结束未回收"
                             % (rx_id, rx["patient"], rx["drug"], rx["qty"]), drug=rx["drug"])

    # ---------- 输出 ----------
    def render(self):
        out = ["===== 药品状态 =====",
               "%-12s %8s %8s %8s %8s %s" % ("药品", "期初库存", "已发放", "账面库存", "实盘数", "状态")]
        for name, d in self.drugs.items():
            book = d["initial"] - d["dispensed"]
            actual = "-" if d["actual"] is None else str(d["actual"])
            status = "异常" if d["abnormal"] else "正常"
            out.append("%-12s %8d %8d %8d %8s %s"
                       % (name, d["initial"], d["dispensed"], book, actual, status))
        out.append("")
        out.append("===== 错误报告（%d 条）=====" % len(self.errors))
        if not self.errors:
            out.append("无错误，账物相符。")
        for lineno, kind, msg in self.errors:
            out.append("[行 %s] %s: %s" % (lineno, kind, msg))
        return "\n".join(out)


DEMO_INPUT = """\
# ---- 药品定义 ----
DRUG 吗啡注射液 100
DRUG 芬太尼贴剂 50
DRUG 哌替啶注射液 40
# ---- 处方流 ----
RX RX001 张三 吗啡注射液 10
RX RX002 李四 芬太尼贴剂 20
RX RX003 张三 吗啡注射液 25
RX RX001 王五 哌替啶注射液 5
RX RX004 赵六 哌替啶注射液 45
RX RX005 张三 吗啡注射液 8
RX RX006 孙七 安定片 3
# ---- 回收流 ----
REC RX001 10
REC RX002 18
REC RX003 25
REC RX099 5
REC RX004 45
# ---- 盘点流 ----
COUNT 吗啡注射液 55
COUNT 芬太尼贴剂 30
COUNT 哌替啶注射液 40
COUNT 安定片 10
"""


def run(text):
    auditor = Auditor()
    for lineno, line in enumerate(text.splitlines(), 1):
        auditor.process_line(lineno, line)
    auditor.finalize()
    return auditor.render()


def main(argv):
    if len(argv) == 2 and argv[1] == "demo":
        print("----- 输入 -----\n" + DEMO_INPUT + "----- 输出 -----")
        print(run(DEMO_INPUT))
        return 0
    if len(argv) != 2:
        print(__doc__)
        return 2
    with open(argv[1], "r", encoding="utf-8") as fh:
        print(run(fh.read()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
