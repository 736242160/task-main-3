#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""远期结售汇合约状态机工具（纯 Python 标准库，单文件）。

用法:
    python3 forward_fx.py [输入文件 ...]      # 不带参数时读标准输入

输入为行式文本流，三类指令可跨文件、跨流交错出现，状态全程延续：
    企业 <名称>
    即期 <币种> <汇率>
    合约 <编号> <企业> <币种> <金额> <约定汇率|即期> <未到期|到期> <签约|交割|展期|违约>

规则:
  * 状态机: 签约 -> {交割, 展期, 违约}; 展期 -> {交割, 展期, 违约};
    交割/违约为终态(合约关闭)，任何后续操作一律拦截并报告。
  * 交割金额(人民币) = 金额 x 当前约定汇率。
  * 展期重算: 展期事件的"约定汇率"字段为数字时作为新约定汇率；
    填 "即期" 时取该币种即期汇率；即期缺失则展期被拦截并报告。
    展期后后续交割按新汇率级联重算。
  * 合约到期(事件到期标记=到期)后仍未交割未展期 -> 级联标记违约并报告。
  * 重复交割、重复签约、引用不存在的企业/合约均报告并拦截。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

ACTIVE = ("签约", "展期")
CLOSED = ("交割", "违约")
ACTIONS = ("签约", "交割", "展期", "违约")
FLAGS = ("未到期", "到期")
CENT = Decimal("0.01")


def money(value: Decimal) -> str:
    return f"{value.quantize(CENT):,}"


@dataclass
class Contract:
    cid: str
    enterprise: str
    currency: str
    amount: Decimal
    rate: Decimal                 # 当前约定汇率（展期后级联更新）
    matured: bool = False         # 是否已观察到"到期"标记
    state: str = "签约"
    extensions: int = 0           # 展期次数
    settled: Decimal | None = None  # 交割人民币金额

    @property
    def notional(self) -> Decimal:
        """交割合约返回实际交割金额，其余返回按当前约定汇率的名义金额。"""
        if self.state == "交割" and self.settled is not None:
            return self.settled
        return self.amount * self.rate


class Engine:
    def __init__(self) -> None:
        self.enterprises: set[str] = set()
        self.spots: dict[str, Decimal] = {}
        self.contracts: dict[str, Contract] = {}
        self.errors: list[tuple[str, str]] = []

    def err(self, where: str, msg: str) -> None:
        self.errors.append((where, msg))

    @staticmethod
    def _num(text: str) -> Decimal | None:
        try:
            return Decimal(text)
        except InvalidOperation:
            return None

    # ---------------------------------------------------------------- 指令分发
    def feed(self, line: str, where: str) -> None:
        parts = line.split()
        if not parts or parts[0].startswith("#"):
            return
        kind, rest = parts[0], parts[1:]
        if kind == "企业":
            self._enterprise(rest, where)
        elif kind == "即期":
            self._spot(rest, where)
        elif kind == "合约":
            self._contract(rest, where)
        else:
            self.err(where, f"未知指令 {kind!r}，已跳过")

    def _enterprise(self, rest: list[str], where: str) -> None:
        if len(rest) != 1:
            self.err(where, "企业指令格式: 企业 <名称>")
            return
        name = rest[0]
        if name in self.enterprises:
            self.err(where, f"企业 {name!r} 重复定义，已忽略")
        else:
            self.enterprises.add(name)

    def _spot(self, rest: list[str], where: str) -> None:
        if len(rest) != 2:
            self.err(where, "即期指令格式: 即期 <币种> <汇率>")
            return
        ccy, rate_s = rest
        rate = self._num(rate_s)
        if rate is None or rate <= 0:
            self.err(where, f"即期汇率无法解析或为非正数: {rate_s!r}")
            return
        self.spots[ccy] = rate  # 后到的即期覆盖先到的（跨流状态延续）

    # ---------------------------------------------------------------- 合约事件
    def _contract(self, rest: list[str], where: str) -> None:
        if len(rest) != 7:
            self.err(where, "合约指令格式: 合约 <编号> <企业> <币种> <金额> "
                            "<约定汇率|即期> <未到期|到期> <签约|交割|展期|违约>")
            return
        cid, ent, ccy, amount_s, rate_s, flag, action = rest
        if flag not in FLAGS:
            self.err(where, f"合约 {cid} 到期标记非法: {flag!r}（应为 未到期/到期）")
            return
        if action not in ACTIONS:
            self.err(where, f"合约 {cid} 状态非法: {action!r}（应为 签约/交割/展期/违约）")
            return
        amount = self._num(amount_s)
        if amount is None or amount <= 0:
            self.err(where, f"合约 {cid} 金额无法解析或为非正数: {amount_s!r}")
            return

        if action == "签约":
            self._sign(cid, ent, ccy, amount, rate_s, flag, where)
            return

        contract = self.contracts.get(cid)
        if contract is None:
            self.err(where, f"引用不存在的合约 {cid}（{action} 被拦截）")
            return
        if ent not in self.enterprises:
            self.err(where, f"合约 {cid} 引用不存在的企业 {ent!r}（{action} 被拦截）")
            return
        if ent != contract.enterprise:
            self.err(where, f"合约 {cid} 企业不匹配: 事件为 {ent!r}，"
                            f"登记为 {contract.enterprise!r}（{action} 被拦截）")
            return
        if ccy != contract.currency:
            self.err(where, f"合约 {cid} 币种不匹配: 事件为 {ccy}，"
                            f"登记为 {contract.currency}（{action} 被拦截）")
            return
        if amount != contract.amount:
            self.err(where, f"合约 {cid} 金额不一致: 事件为 {money(amount)}，"
                            f"登记为 {money(contract.amount)}（以登记金额为准）")
        if flag == "到期":
            contract.matured = True  # 到期观察独立于动作合法性，供级联违约使用

        if contract.state in CLOSED:
            if contract.state == "交割" and action == "交割":
                self.err(where, f"合约 {cid} 重复交割，已拦截"
                                f"（首次交割金额 {money(contract.settled)} 人民币）")
            else:
                self.err(where, f"合约 {cid} 已{contract.state}关闭，"
                                f"禁止再操作（{action} 被拦截）")
            return

        if action == "签约":
            self.err(where, f"合约 {cid} 非法跳转: {contract.state} -> 签约，已拦截")
        elif action == "违约":
            contract.state = "违约"
        elif action == "展期":
            self._extend(contract, rate_s, where)
        else:  # 交割
            self._settle(contract, rate_s, where)

    def _sign(self, cid, ent, ccy, amount, rate_s, flag, where) -> None:
        if cid in self.contracts:
            contract = self.contracts[cid]
            if flag == "到期":
                contract.matured = True
            self.err(where, f"合约 {cid} 非法跳转: {contract.state} -> 签约"
                            f"（重复签约，已拦截）")
            return
        if ent not in self.enterprises:
            self.err(where, f"合约 {cid} 引用不存在的企业 {ent!r}（签约被拦截）")
            return
        rate = self._num(rate_s)
        if rate is None or rate <= 0:
            self.err(where, f"合约 {cid} 签约必须携带有效约定汇率，得到 {rate_s!r}")
            return
        self.contracts[cid] = Contract(
            cid=cid, enterprise=ent, currency=ccy, amount=amount,
            rate=rate, matured=(flag == "到期"),
        )

    def _extend(self, contract: Contract, rate_s: str, where: str) -> None:
        """展期：确定新约定汇率并级联重算名义金额。"""
        if rate_s == "即期":
            spot = self.spots.get(contract.currency)
            if spot is None:
                self.err(where, f"合约 {contract.cid} 展期重算失败: 币种 "
                                f"{contract.currency} 即期汇率缺失（展期被拦截，"
                                f"维持原状态 {contract.state}）")
                return
            new_rate = spot
        else:
            new_rate = self._num(rate_s)
            if new_rate is None or new_rate <= 0:
                self.err(where, f"合约 {contract.cid} 展期新约定汇率非法: "
                                f"{rate_s!r}（展期被拦截）")
                return
        contract.rate = new_rate
        contract.extensions += 1
        contract.state = "展期"

    def _settle(self, contract: Contract, rate_s: str, where: str) -> None:
        event_rate = self._num(rate_s)
        if event_rate is not None and event_rate != contract.rate:
            self.err(where, f"合约 {contract.cid} 交割事件汇率 {event_rate} 与当前约定汇率 "
                            f"{contract.rate} 不一致，按合约当前约定汇率交割")
        contract.settled = contract.amount * contract.rate
        contract.state = "交割"

    # ---------------------------------------------------------------- 级联与输出
    def finalize(self) -> None:
        """期末扫描：已到期但仍处活动状态的合约级联标记违约。"""
        for contract in self.contracts.values():
            if contract.matured and contract.state in ACTIVE:
                contract.state = "违约"
                self.err("级联", f"合约 {contract.cid} 到期后未交割亦未展期，"
                                 f"级联标记为违约（企业 {contract.enterprise}，"
                                 f"名义金额 {money(contract.notional)} 人民币）")

    def report(self, out=sys.stdout) -> None:
        print("=" * 78, file=out)
        print("合约状态清单", file=out)
        print("=" * 78, file=out)
        header = ("编号", "企业", "币种", "金额", "当前约定汇率",
                  "到期", "状态", "展期次数", "交割/名义金额(人民币)")
        print("%-8s %-10s %-5s %14s %12s %-5s %-4s %8s %20s" % header, file=out)
        print("-" * 78, file=out)
        for cid in sorted(self.contracts):
            c = self.contracts[cid]
            print("%-8s %-10s %-5s %14s %12s %-5s %-4s %8d %20s" % (
                c.cid, c.enterprise, c.currency, money(c.amount), c.rate,
                "到期" if c.matured else "未到期", c.state,
                c.extensions, money(c.notional)), file=out)
        print(file=out)
        print("=" * 78, file=out)
        print(f"错误报告（共 {len(self.errors)} 条）", file=out)
        print("=" * 78, file=out)
        if not self.errors:
            print("无", file=out)
        for where, msg in self.errors:
            print(f"[{where}] {msg}", file=out)


def main(argv: list[str]) -> int:
    engine = Engine()
    sources = argv[1:]
    if not sources:
        for lineno, line in enumerate(sys.stdin, 1):
            engine.feed(line, f"stdin:{lineno}")
    else:
        for path in sources:
            with open(path, encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    engine.feed(line, f"{path}:{lineno}")
    engine.finalize()
    engine.report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
