#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""影视版权授权管理工具（纯 Python 标准库，单文件）。

输入为行式文本（# 开头为注释，空行忽略），三类指令可任意交错（跨流状态延续）：

    地区 <地区名> <是否全国: 是|否>
    授权 <剧名> <平台> <地区> <开始YYYY-MM-DD> <结束YYYY-MM-DD> <状态: 意向|生效|到期|解约>
    冲突 <剧名> <平台> <地区>

规则：
  * 状态机：意向 -> 生效 -> 到期|解约；意向 -> 解约；到期|解约 -> 意向（新一轮授权）。
    其余跳转（含新授权首事件非「意向」）一律拦截并报告。
  * 冲突：同剧、同平台、窗口重叠，且地区重叠（全国与任何地区重叠；同名单地区自相重叠）。
  * 优先级（自定）：全国授权 > 单地区授权；同级窗口开始早者优先；再同级先登记者优先。
    冲突时低优先级授权被「挂起」；高优先级授权解约/到期后，被挂起者自动恢复生效。
  * 到期/解约后授权失效，不再占用窗口。
  * 错误报告：非法跳转、区间非法（开始晚于结束）、引用不存在地区/剧名、重复冲突检查等。

用法：
    python3 license_tool.py 输入文件        # 省略文件名则读标准输入
    python3 license_tool.py --demo          # 运行内置示例
"""

import sys
from dataclasses import dataclass
from datetime import date

STATUS = ("意向", "生效", "到期", "解约")

# 状态机：允许的跳转
TRANSITIONS = {
    "意向": {"生效", "解约"},
    "生效": {"到期", "解约"},
    "到期": {"意向"},   # 到期后可重新登记意向（新一轮授权）
    "解约": {"意向"},   # 解约后可重新登记意向
}

# 声明状态 -> 有效状态（生效态在重查时细分为 生效中/挂起）
STATE_TO_EFFECTIVE = {
    "意向": "意向",
    "生效": "生效中",
    "到期": "已到期",
    "解约": "已解约",
}


@dataclass
class Region:
    name: str
    national: bool


@dataclass
class License:
    idx: int            # 登记序号
    show: str           # 剧名
    platform: str       # 平台
    region: str         # 地区
    start: date
    end: date
    state: str          # 声明状态：意向/生效/到期/解约
    effective: str      # 有效状态：意向/生效中/挂起/已到期/已解约

    @property
    def key(self):
        return (self.show, self.platform, self.region)


class Engine:
    def __init__(self):
        self.regions = {}          # 地区名 -> Region
        self.licenses = []         # 按登记序
        self.by_key = {}           # (剧名,平台,地区) -> License
        self.errors = []           # (行号, 消息)
        self.reports = []          # (行号, 标签[冲突|恢复], 消息)
        self.checks = []           # 冲突检查结果（已渲染文本）
        self.seen_checks = set()   # 已执行的冲突检查 (剧名,平台,地区)
        self.conflict_pairs = set()  # 已报告过的冲突授权对

    # ---------- 基础判断 ----------

    def regions_overlap(self, a, b):
        if a == b:
            return True
        return self.regions[a].national or self.regions[b].national

    @staticmethod
    def windows_overlap(x, y):
        return x.start <= y.end and y.start <= x.end

    def priority(self, lic):
        """自定优先级：全国 > 单地区；窗口开始早者优先；先登记者优先。"""
        national = 1 if self.regions[lic.region].national else 0
        return (national, -lic.start.toordinal(), -lic.idx)

    def kind(self, region_name):
        return "全国" if self.regions[region_name].national else "单地区"

    def fmt_lic(self, lic):
        return (f"#{lic.idx} {lic.region}({self.kind(lic.region)}) "
                f"{lic.start}~{lic.end} [{lic.effective}]")

    # ---------- 冲突重查与有效状态计算 ----------

    def recompute(self, line_no):
        """任一状态变化后全量重查：报告新冲突，计算挂起/生效中，检测自动恢复。"""
        active = [lic for lic in self.licenses if lic.state == "生效"]
        for lic in self.licenses:
            if lic.state != "生效":
                lic.effective = STATE_TO_EFFECTIVE[lic.state]
        for lic in active:
            suppressor = None
            for other in active:
                if other.idx == lic.idx or other.show != lic.show or other.platform != lic.platform:
                    continue
                if not self.regions_overlap(lic.region, other.region):
                    continue
                if not self.windows_overlap(lic, other):
                    continue
                pair = frozenset((lic.idx, other.idx))
                if pair not in self.conflict_pairs:
                    self.conflict_pairs.add(pair)
                    self._report_conflict(line_no, lic, other)
                if self.priority(other) > self.priority(lic):
                    if suppressor is None or self.priority(other) > self.priority(suppressor):
                        suppressor = other
            old = lic.effective
            lic.effective = "挂起" if suppressor else "生效中"
            if old == "挂起" and lic.effective == "生效中":
                self.reports.append((line_no, "恢复",
                    f"《{lic.show}》@{lic.platform} {self.fmt_lic(lic)} "
                    f"原压制授权已解约/到期，该窗口自动恢复生效"))

    def _report_conflict(self, line_no, a, b):
        loser, winner = (a, b) if self.priority(b) > self.priority(a) else (b, a)
        self.reports.append((line_no, "冲突",
            f"《{a.show}》@{a.platform} 同平台窗口重叠："
            f"#{a.idx} {a.region}({self.kind(a.region)}) {a.start}~{a.end} 与 "
            f"#{b.idx} {b.region}({self.kind(b.region)}) {b.start}~{b.end}；"
            f"按优先级 #{winner.idx} {winner.region} 优先，#{loser.idx} {loser.region} 挂起"))

    # ---------- 指令处理 ----------

    def do_region(self, line_no, parts):
        if len(parts) != 3:
            self.errors.append((line_no, "地区指令格式应为：地区 <名称> <是|否>"))
            return
        _, name, flag = parts
        if flag not in ("是", "否"):
            self.errors.append((line_no, f"地区「{name}」是否全国只能填 是/否，实际为「{flag}」"))
            return
        if name in self.regions:
            self.errors.append((line_no, f"重复定义地区「{name}」"))
            return
        self.regions[name] = Region(name, flag == "是")

    def do_license(self, line_no, parts):
        if len(parts) != 7:
            self.errors.append((line_no,
                "授权指令格式应为：授权 <剧名> <平台> <地区> <开始> <结束> <状态>"))
            return
        _, show, platform, region, start_s, end_s, status = parts
        if region not in self.regions:
            self.errors.append((line_no, f"引用不存在的地区「{region}」"))
            return
        if status not in STATUS:
            self.errors.append((line_no, f"未知授权状态「{status}」（应为 意向/生效/到期/解约）"))
            return
        try:
            start = date.fromisoformat(start_s)
            end = date.fromisoformat(end_s)
        except ValueError:
            self.errors.append((line_no, f"日期格式非法：{start_s} / {end_s}（应为 YYYY-MM-DD）"))
            return
        if start > end:
            self.errors.append((line_no,
                f"授权区间非法：《{show}》@{platform} {region} 开始 {start} 晚于结束 {end}"))
            return
        key = (show, platform, region)
        lic = self.by_key.get(key)
        if lic is None:
            if status != "意向":
                self.errors.append((line_no,
                    f"非法状态跳转：《{show}》@{platform} {region} 新授权首个状态必须为「意向」，"
                    f"实际为「{status}」"))
                return
            lic = License(len(self.licenses) + 1, show, platform, region,
                          start, end, "意向", "意向")
            self.licenses.append(lic)
            self.by_key[key] = lic
        else:
            if status not in TRANSITIONS[lic.state]:
                allowed = "、".join(f"{lic.state}→{s}" for s in sorted(TRANSITIONS[lic.state]))
                self.errors.append((line_no,
                    f"非法状态跳转：《{show}》@{platform} {region} 「{lic.state}」→「{status}」"
                    f"（仅允许：{allowed}）"))
                return
            lic.state = status
            lic.start, lic.end = start, end
        self.recompute(line_no)

    def do_check(self, line_no, parts):
        if len(parts) != 4:
            self.errors.append((line_no, "冲突指令格式应为：冲突 <剧名> <平台> <地区>"))
            return
        _, show, platform, region = parts
        if region not in self.regions:
            self.errors.append((line_no, f"冲突检查引用不存在的地区「{region}」"))
            return
        if all(lic.show != show for lic in self.licenses):
            self.errors.append((line_no, f"冲突检查引用不存在的剧名《{show}》"))
            return
        key = (show, platform, region)
        if key in self.seen_checks:
            self.errors.append((line_no,
                f"重复冲突检查：《{show}》@{platform} {region} 已检查过"))
            return
        self.seen_checks.add(key)
        relevant = [lic for lic in self.licenses
                    if lic.show == show and lic.platform == platform
                    and lic.state == "生效"
                    and self.regions_overlap(lic.region, region)]
        active = [lic for lic in relevant if lic.effective == "生效中"]
        suspended = [lic for lic in relevant if lic.effective == "挂起"]
        if not relevant:
            verdict, detail = "无冲突", "当前无相关生效授权"
        elif suspended:
            verdict = "存在冲突"
            detail = ("生效中：" + "；".join(self.fmt_lic(l) for l in active) if active else "") \
                     + "；被挂起：" + "；".join(self.fmt_lic(l) for l in suspended)
            detail = detail.strip("；")
        else:
            verdict = "无冲突"
            detail = "当前有效授权：" + "；".join(self.fmt_lic(l) for l in active)
        self.checks.append(
            f"第{line_no}行 冲突 《{show}》/{platform}/{region}({self.kind(region)}) "
            f"→ {verdict}（{detail}）")


def run(text):
    engine = Engine()
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        head = parts[0]
        if head == "地区":
            engine.do_region(line_no, parts)
        elif head == "授权":
            engine.do_license(line_no, parts)
        elif head == "冲突":
            engine.do_check(line_no, parts)
        else:
            engine.errors.append((line_no, f"无法识别的指令「{head}」"))
    return engine


def render(engine):
    out = ["========== 授权状态 =========="]
    if not engine.licenses:
        out.append("（无授权）")
    for lic in engine.licenses:
        out.append(f"#{lic.idx} 《{lic.show}》 平台={lic.platform} "
                   f"地区={lic.region}({engine.kind(lic.region)}) "
                   f"窗口={lic.start}~{lic.end} 状态={lic.state} 有效状态={lic.effective}")
    out.append("")
    out.append("========== 冲突检查结果 ==========")
    out.extend(engine.checks or ["（无冲突检查）"])
    out.append("")
    out.append("========== 错误与报告清单 ==========")
    entries = [(ln, "错误", msg) for ln, msg in engine.errors] + engine.reports
    entries.sort(key=lambda e: e[0])
    if not entries:
        out.append("（无）")
    for ln, tag, msg in entries:
        out.append(f"[{tag}] 第{ln}行：{msg}")
    return "\n".join(out)


DEMO = """\
# ===== 地区定义 =====
地区 全国 是
地区 华东 否
地区 华南 否

# ===== 授权流 =====
授权 长风渡 爱奇艺 华东 2026-01-01 2026-12-31 意向
授权 长风渡 爱奇艺 华东 2026-01-01 2026-12-31 生效
授权 长风渡 爱奇艺 全国 2026-06-01 2027-05-31 意向
授权 长风渡 爱奇艺 全国 2026-06-01 2027-05-31 生效
冲突 长风渡 爱奇艺 华东
授权 长风渡 爱奇艺 全国 2026-06-01 2027-05-31 解约
冲突 长风渡 爱奇艺 全国
授权 长风渡 爱奇艺 华东 2026-01-01 2026-12-31 到期
授权 长风渡 爱奇艺 华东 2026-01-01 2026-12-31 生效
授权 琅琊榜 腾讯 华东 2027-06-01 2026-01-01 意向
授权 庆余年 腾讯 华北 2026-03-01 2026-09-30 意向
授权 庆余年 腾讯 华东 2026-03-01 2026-09-30 生效
冲突 不存在的剧 腾讯 华东
冲突 长风渡 爱奇艺 华东
"""


def main(argv):
    args = argv[1:]
    if "--demo" in args:
        text = DEMO
        print("---------- 输入（内置示例） ----------")
        print(text, end="")
        print("---------- 输出 ----------")
    elif args:
        with open(args[0], encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()
    print(render(run(text)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
