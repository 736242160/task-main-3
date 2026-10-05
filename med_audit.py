#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
med_audit.py — 处方动态审核工具（纯 Python 标准库，单文件，Python 3.7+）

用法:
    python3 med_audit.py 输入.json            输出文本报告（有错误时退出码为 1）
    python3 med_audit.py 输入.json --json     输出 JSON 报告
    cat 输入.json | python3 med_audit.py -    从标准输入读取

输入 JSON 结构（四个段均可省略，默认为空）:
{
  "drugs": [
    {"name": "阿司匹林", "max_daily_dose": 300, "max_course_days": 14}
  ],
  "interactions": [
    {"a": "阿司匹林", "b": "华法林", "level": "禁忌"}
  ],
  "prescriptions": [
    {"patient": "P1", "date": "2026-01-01", "drug": "阿司匹林",
     "daily_dose": 100, "days": 7}
  ],
  "stops": [
    {"patient": "P1", "date": "2026-01-05", "drug": "阿司匹林"}
  ]
}

审核规则:
  * 处方流与停药流按日期合并为统一事件流；同日先处理停药、再处理处方
    （便于同日"停旧药、开新药"），同类型内保持输入先后顺序。
  * 疗程为开始日期起 days 天（含当日）；停药当日及以后该药不计入当前用药。
  * 同一患者当前用药集内：禁忌 -> 错误（拦截），慎用 -> 警告（标记）。
  * 同药跨处方累计日剂量超过单日上限 -> 错误，并定位涉及的处方。
  * 同药累计疗程天数（跨处方、含已停药）超过疗程上限 -> 错误。
  * 停药后对该患者剩余用药级联重审：报告相互作用解除或新增矛盾。
  * 处方引用未定义药品 -> 错误，该处方被忽略。
  * 同一患者同日重复开同一药 -> 错误，重复处方被忽略。
  * 全部状态跨事件延续，最终输出各患者当前用药状态与完整发现清单。
"""

import argparse
import itertools
import json
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta

SEV_ERROR = "错误"
SEV_WARN = "警告"
SEV_INFO = "信息"
SEV_ORDER = {SEV_ERROR: 0, SEV_WARN: 1, SEV_INFO: 2}

LEVEL_FORBID = "禁忌"
LEVEL_CAUTION = "慎用"
LEVELS = (LEVEL_FORBID, LEVEL_CAUTION)


def fmt_num(x):
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
    return str(x)


def parse_date(value, ctx):
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise SystemExit("%s: 非法日期 %r（应为 YYYY-MM-DD）" % (ctx, value))


@dataclass
class Drug:
    name: str
    max_daily_dose: float
    max_course_days: int


@dataclass
class Prescription:
    seq: int
    patient: str
    drug: str
    start: date
    days: int
    daily_dose: float
    stop_date: date = None

    @property
    def end(self):
        natural = self.start + timedelta(days=self.days)
        if self.stop_date is not None and self.stop_date < natural:
            return self.stop_date
        return natural

    def active_on(self, day):
        return self.start <= day < self.end

    def label(self):
        return "处方#%d(%s起%s天 %s/日)" % (
            self.seq, self.start.isoformat(), self.days, fmt_num(self.daily_dose))


@dataclass
class PatientState:
    name: str
    prescriptions: list = field(default_factory=list)
    cum_days: dict = field(default_factory=dict)
    reported_pairs: dict = field(default_factory=dict)
    prescribed_on: set = field(default_factory=set)
    last_date: date = None


class Auditor:
    def __init__(self, drugs, interactions):
        self.drugs = drugs
        self.interactions = interactions
        self.patients = {}
        self.findings = []

    def patient(self, name):
        if name not in self.patients:
            self.patients[name] = PatientState(name)
        return self.patients[name]

    def add(self, day, severity, patient, message):
        self.findings.append({
            "date": day.isoformat(),
            "severity": severity,
            "patient": patient,
            "message": message,
        })

    def active_meds(self, ps, day):
        return [rx for rx in ps.prescriptions if rx.active_on(day)]

    def active_pairs(self, ps, day):
        names = sorted({rx.drug for rx in self.active_meds(ps, day)})
        pairs = {}
        for a, b in itertools.combinations(names, 2):
            key = frozenset((a, b))
            if key in self.interactions:
                pairs[key] = self.interactions[key]
        return pairs

    def diff_pairs(self, ps, day):
        now = self.active_pairs(ps, day)
        for key in sorted(now, key=lambda k: sorted(k)):
            if key not in ps.reported_pairs:
                a, b = sorted(key)
                if now[key] == LEVEL_FORBID:
                    self.add(day, SEV_ERROR, ps.name,
                             "禁忌拦截：当前用药中「%s」与「%s」存在禁忌相互作用" % (a, b))
                else:
                    self.add(day, SEV_WARN, ps.name,
                             "慎用标记：当前用药中「%s」与「%s」需慎用" % (a, b))
        for key in sorted(set(ps.reported_pairs) - set(now), key=lambda k: sorted(k)):
            a, b = sorted(key)
            self.add(day, SEV_INFO, ps.name,
                     "相互作用解除：「%s」与「%s」不再同时出现在当前用药中（原级别 %s）"
                     % (a, b, ps.reported_pairs[key]))
        ps.reported_pairs = now

    def process_rx(self, day, patient, drug, dose, days, seq):
        ps = self.patient(patient)
        ps.last_date = day
        if drug not in self.drugs:
            self.add(day, SEV_ERROR, patient,
                     "处方#%d 引用了不存在的药品「%s」，已忽略该处方" % (seq, drug))
            return
        if (day, drug) in ps.prescribed_on:
            self.add(day, SEV_ERROR, patient,
                     "同日重复开药：%s 在 %s 重复开具「%s」（处方#%d），已忽略该处方"
                     % (patient, day.isoformat(), drug, seq))
            return
        ps.prescribed_on.add((day, drug))
        rx = Prescription(seq, patient, drug, day, days, dose)
        ps.prescriptions.append(rx)
        ps.cum_days[drug] = ps.cum_days.get(drug, 0) + days
        spec = self.drugs[drug]
        same = [r for r in self.active_meds(ps, day) if r.drug == drug]
        total = sum(r.daily_dose for r in same)
        if total > spec.max_daily_dose:
            loc = "、".join(r.label() for r in same)
            self.add(day, SEV_ERROR, patient,
                     "日剂量超限：「%s」当前累计日剂量 %s 超过上限 %s；涉及 %s"
                     % (drug, fmt_num(total), fmt_num(spec.max_daily_dose), loc))
        if ps.cum_days[drug] > spec.max_course_days:
            self.add(day, SEV_ERROR, patient,
                     "疗程超限：「%s」累计疗程 %d 天超过上限 %d 天（本次处方#%d 计入 %d 天）"
                     % (drug, ps.cum_days[drug], spec.max_course_days, seq, days))
        self.diff_pairs(ps, day)

    def process_stop(self, day, patient, drug):
        ps = self.patient(patient)
        ps.last_date = day
        targets = [r for r in self.active_meds(ps, day) if r.drug == drug]
        if not targets:
            self.add(day, SEV_WARN, patient,
                     "停药记录：「%s」在 %s 不在当前用药中，无实际效果"
                     % (drug, day.isoformat()))
        for rx in targets:
            rx.stop_date = day
        self.diff_pairs(ps, day)

    def run(self, prescriptions, stops):
        events = []
        for rx in prescriptions:
            events.append((rx["date"], 1, rx["seq"], "rx", rx))
        for st in stops:
            events.append((st["date"], 0, st["seq"], "stop", st))
        events.sort(key=lambda e: (e[0], e[1], e[2]))
        for day, _, _, kind, ev in events:
            if kind == "rx":
                self.process_rx(day, ev["patient"], ev["drug"],
                                ev["daily_dose"], ev["days"], ev["seq"])
            else:
                self.process_stop(day, ev["patient"], ev["drug"])


def load_input(text):
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit("输入不是合法 JSON：%s" % exc)
    if not isinstance(data, dict):
        raise SystemExit("输入顶层必须是 JSON 对象")

    drugs = {}
    for i, item in enumerate(data.get("drugs") or []):
        ctx = "drugs[%d]" % i
        name = str(item["name"])
        if name in drugs:
            raise SystemExit("%s: 药品「%s」重复定义" % (ctx, name))
        drugs[name] = Drug(name, float(item["max_daily_dose"]),
                           int(item["max_course_days"]))

    interactions = {}
    for i, item in enumerate(data.get("interactions") or []):
        ctx = "interactions[%d]" % i
        a, b = str(item["a"]), str(item["b"])
        level = str(item["level"])
        if level not in LEVELS:
            raise SystemExit("%s: 级别必须是 %s 之一" % (ctx, "/".join(LEVELS)))
        if a == b:
            raise SystemExit("%s: 相互作用两端的药品相同（%s）" % (ctx, a))
        key = frozenset((a, b))
        if key not in interactions or level == LEVEL_FORBID:
            interactions[key] = level

    prescriptions = []
    for i, item in enumerate(data.get("prescriptions") or []):
        ctx = "prescriptions[%d]" % i
        days = int(item["days"])
        dose = float(item["daily_dose"])
        if days < 1:
            raise SystemExit("%s: 疗程天数必须 >= 1" % ctx)
        if dose <= 0:
            raise SystemExit("%s: 每日剂量必须 > 0" % ctx)
        prescriptions.append({
            "seq": i + 1,
            "patient": str(item["patient"]),
            "date": parse_date(item["date"], ctx),
            "drug": str(item["drug"]),
            "daily_dose": dose,
            "days": days,
        })

    stops = []
    for i, item in enumerate(data.get("stops") or []):
        ctx = "stops[%d]" % i
        stops.append({
            "seq": i + 1,
            "patient": str(item["patient"]),
            "date": parse_date(item["date"], ctx),
            "drug": str(item["drug"]),
        })

    return drugs, interactions, prescriptions, stops


def patient_report(auditor, ps):
    day = ps.last_date
    meds = auditor.active_meds(ps, day)
    return {
        "as_of": day.isoformat(),
        "current_medications": [{
            "drug": rx.drug,
            "daily_dose": rx.daily_dose,
            "start": rx.start.isoformat(),
            "days": rx.days,
            "effective_until": (rx.end - timedelta(days=1)).isoformat(),
            "stopped": rx.stop_date is not None,
        } for rx in sorted(meds, key=lambda r: (r.drug, r.start))],
        "findings": [f for f in auditor.findings if f["patient"] == ps.name],
    }


def build_json_report(auditor):
    return {
        "findings": sorted(auditor.findings,
                           key=lambda f: (f["date"], SEV_ORDER[f["severity"]])),
        "patients": {name: patient_report(auditor, auditor.patients[name])
                     for name in sorted(auditor.patients)},
    }


def render_text(auditor):
    lines = ["=" * 60, "审核发现（按时间序）", "=" * 60]
    ordered = sorted(auditor.findings,
                     key=lambda f: (f["date"], SEV_ORDER[f["severity"]]))
    if not ordered:
        lines.append("（无发现）")
    for f in ordered:
        lines.append("[%s] %s %s: %s" % (f["severity"], f["date"],
                                         f["patient"], f["message"]))
    lines += ["", "=" * 60, "患者用药状态", "=" * 60]
    for name in sorted(auditor.patients):
        ps = auditor.patients[name]
        rep = patient_report(auditor, ps)
        lines.append("患者 %s（截至 %s）" % (name, rep["as_of"]))
        if rep["current_medications"]:
            lines.append("  当前用药：")
            for m in rep["current_medications"]:
                lines.append("    - %s %s/日，%s 起 %d 天（有效至 %s）"
                             % (m["drug"], fmt_num(m["daily_dose"]),
                                m["start"], m["days"], m["effective_until"]))
        else:
            lines.append("  当前用药：（无）")
        errs = sum(1 for f in rep["findings"] if f["severity"] == SEV_ERROR)
        warns = sum(1 for f in rep["findings"] if f["severity"] == SEV_WARN)
        infos = len(rep["findings"]) - errs - warns
        lines.append("  发现：%d 错误 / %d 警告 / %d 信息" % (errs, warns, infos))
        lines.append("")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="处方动态审核工具（纯标准库单文件），详见文件头部文档")
    ap.add_argument("input", help="输入 JSON 文件路径，'-' 表示标准输入")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出报告")
    args = ap.parse_args(argv)
    if args.input == "-":
        text = sys.stdin.read()
    else:
        with open(args.input, encoding="utf-8") as fh:
            text = fh.read()
    drugs, interactions, prescriptions, stops = load_input(text)
    auditor = Auditor(drugs, interactions)
    auditor.run(prescriptions, stops)
    if args.json:
        print(json.dumps(build_json_report(auditor), ensure_ascii=False, indent=2))
    else:
        print(render_text(auditor))
    has_error = any(f["severity"] == SEV_ERROR for f in auditor.findings)
    return 1 if has_error else 0


if __name__ == "__main__":
    sys.exit(main())
