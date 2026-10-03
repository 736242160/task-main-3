#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""远期结售汇合约状态机工具（纯标准库，单文件）。

输入格式（行式，# 开头为注释，字段以空白分隔，三流可任意交错，按序处理）：
    企业 <企业名称>
    即期 <币种> <汇率>
    合约 <编号> <企业> <币种> <金额> <约定汇率> <到期标记:未到期|到期> <状态:签约|交割|展期|违约>

规则：
  * 状态机：签约 -> 交割/展期/违约；展期 -> 交割/展期/违约；交割、违约 为终态（合约关闭）。
  * 交割人民币金额 = 金额 x 当前约定汇率（含展期级联重算后的新汇率）。
  * 展期：以当时即期汇率作为新约定汇率，级联影响后续交割金额；即期缺失则展期失败并报告，合约维持原状态。
  * 到期标记为“到期”的合约在流结束时仍未交割/未展期成功，级联标记违约并报告。
  * 非法跳转、重复交割、违约后再操作、引用不存在的企业/合约，一律拦截并报告。

用法：
    python3 fx_forward.py [输入文件]     # 缺省运行内置演示样例（含展期重算）
"""

import sys
from dataclasses import dataclass, field

OPEN_STATES = {"签约", "展期"}
CLOSED_STATES = {"交割", "违约"}
ALL_STATES = OPEN_STATES | CLOSED_STATES


@dataclass
class Contract:
    cid: str
    enterprise: str
    currency: str
    amount: float
    rate: float          # 当前约定汇率（展期后会被级联更新）
    maturity: str        # 未到期 / 到期
    status: str = "签约"
    delivered: float = None   # 已交割人民币金额
    extensions: int = 0
    history: list = field(default_factory=list)


class Engine:
    def __init__(self):
        self.enterprises = set()
        self.spot = {}          # 币种 -> 最新即期汇率（跨流延续）
        self.contracts = {}     # 编号 -> Contract（跨流延续）
        self.errors = []

    # ---------- 三流事件 ----------
    def on_enterprise(self, name, lineno):
        if name in self.enterprises:
            self.errors.append(f"第{lineno}行: 企业重复定义: {name}")
            return
        self.enterprises.add(name)

    def on_spot(self, currency, rate, lineno):
        self.spot[currency] = rate

    def on_contract(self, cid, ent, ccy, amount, rate, maturity, action, lineno):
        if action not in ALL_STATES:
            self.errors.append(f"第{lineno}行: 合约 {cid} 未知状态/操作: {action}")
            return
        if maturity not in ("未到期", "到期"):
            self.errors.append(f"第{lineno}行: 合约 {cid} 未知到期标记: {maturity}")
            return

        c = self.contracts.get(cid)

        if action == "签约":
            if c is not None:
                self.errors.append(f"第{lineno}行: 合约 {cid} 重复签约，已拦截（当前状态 {c.status}）")
                return
            if ent not in self.enterprises:
                self.errors.append(f"第{lineno}行: 合约 {cid} 引用不存在的企业: {ent}，签约被拒绝")
                return
            self.contracts[cid] = Contract(cid, ent, ccy, amount, rate, maturity)
            return

        # 非签约事件必须引用已存在合约
        if c is None:
            self.errors.append(f"第{lineno}行: 引用不存在的合约: {cid}，操作 {action} 被拦截")
            return

        # 终态拦截：交割/违约后合约关闭
        if maturity == "到期":
            c.maturity = "到期"  # 到期标记单调推进

        if c.status == "违约":
            self.errors.append(f"第{lineno}行: 合约 {cid} 已违约关闭，操作 {action} 被拦截")
            return
        if c.status == "交割":
            if action == "交割":
                self.errors.append(f"第{lineno}行: 合约 {cid} 重复交割，已拦截"
                                   f"（已交割金额 {c.delivered:.2f}）")
            else:
                self.errors.append(f"第{lineno}行: 合约 {cid} 已交割关闭，操作 {action} 被拦截")
            return

        # 开放状态（签约/展期）下的合法跳转
        if action == "交割":
            c.delivered = c.amount * c.rate   # 按当前约定汇率（含展期级联重算结果）
            c.status = "交割"
            c.history.append(f"第{lineno}行 交割: {c.amount:.2f} {c.currency} x {c.rate:.4f}"
                             f" = {c.delivered:.2f} 人民币")
        elif action == "展期":
            spot = self.spot.get(c.currency)
            if spot is None:
                self.errors.append(f"第{lineno}行: 合约 {cid} 展期失败: 币种 {c.currency} "
                                   f"即期汇率缺失，无法重算，合约维持状态 {c.status}")
                return
            old = c.rate
            c.rate = spot                     # 级联：新约定汇率影响后续交割金额
            c.status = "展期"
            c.extensions += 1
            c.history.append(f"第{lineno}行 展期: 约定汇率 {old:.4f} -> {spot:.4f}（即期）")
        elif action == "违约":
            c.status = "违约"
            c.history.append(f"第{lineno}行 违约: 合约关闭")

    # ---------- 到期级联 ----------
    def finalize(self):
        for c in self.contracts.values():
            if c.maturity == "到期" and c.status in OPEN_STATES:
                c.status = "违约"
                c.history.append("流结束 到期未交割未展期，级联标记违约")
                self.errors.append(f"合约 {c.cid}: 到期后未交割未展期，级联标记违约")

    # ---------- 解析 ----------
    def feed_line(self, line, lineno):
        line = line.strip()
        if not line or line.startswith("#"):
            return
        parts = line.split()
        try:
            if parts[0] == "企业" and len(parts) == 2:
                self.on_enterprise(parts[1], lineno)
            elif parts[0] == "即期" and len(parts) == 3:
                self.on_spot(parts[1], float(parts[2]), lineno)
            elif parts[0] == "合约" and len(parts) == 8:
                self.on_contract(parts[1], parts[2], parts[3], float(parts[4]),
                                 float(parts[5]), parts[6], parts[7], lineno)
            else:
                self.errors.append(f"第{lineno}行: 无法解析: {line}")
        except ValueError:
            self.errors.append(f"第{lineno}行: 数值格式错误: {line}")

    def feed_text(self, text):
        for i, line in enumerate(text.splitlines(), 1):
            self.feed_line(line, i)
        self.finalize()

    # ---------- 输出 ----------
    def report(self):
        out = ["===== 合约状态 ====="]
        for cid in sorted(self.contracts):
            c = self.contracts[cid]
            delivered = f"{c.delivered:.2f}" if c.delivered is not None else "-"
            out.append(f"{cid} | 企业:{c.enterprise} | 币种:{c.currency} | 金额:{c.amount:.2f}"
                       f" | 约定汇率:{c.rate:.4f} | 到期标记:{c.maturity} | 状态:{c.status}"
                       f" | 展期次数:{c.extensions} | 交割金额:{delivered}")
            for h in c.history:
                out.append(f"    - {h}")
        out.append("===== 错误报告 =====")
        if self.errors:
            out.extend(f"[错误] {e}" for e in self.errors)
        else:
            out.append("无错误")
        return "\n".join(out)


DEMO = """\
# 演示样例：含展期级联重算
企业 华兴贸易
企业 恒泰制造
即期 USD 7.0000
合约 C001 华兴贸易 USD 100000 6.9500 未到期 签约
合约 C002 恒泰制造 USD 50000 6.9600 到期 签约
即期 USD 7.1200
合约 C001 华兴贸易 USD 100000 6.9500 到期 展期
合约 C001 华兴贸易 USD 100000 6.9500 到期 交割
合约 C001 华兴贸易 USD 100000 6.9500 到期 交割
合约 C002 恒泰制造 USD 50000 6.9600 到期 签约
合约 C003 幽灵公司 USD 10000 7.0000 未到期 签约
合约 C009 华兴贸易 USD 10000 7.0000 未到期 交割
合约 C004 华兴贸易 EUR 20000 7.8000 到期 签约
合约 C004 华兴贸易 EUR 20000 7.8000 到期 展期
合约 C002 恒泰制造 USD 50000 6.9600 到期 交割
"""


def main():
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8") as f:
            text = f.read()
    else:
        text = DEMO
    engine = Engine()
    engine.feed_text(text)
    print(engine.report())


if __name__ == "__main__":
    main()
