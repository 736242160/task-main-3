#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cosmetic_compliance.py — 化妆品备案/抽检合规校验工具（纯标准库，单文件）

用法
----
    python3 cosmetic_compliance.py \
        --banned banned.json \
        --restricted restricted.json \
        --filings filings.jsonl \
        --inspections inspections.jsonl \
        [--products products.json] \
        [--out report.json]

输入文件（均支持两种格式：JSON 数组整体，或 JSONL 每行一条记录）
------------------------------------------------------------------
1. --banned      禁用成分清单：["成分名", ...]
2. --restricted  限用成分表：{"成分名": 限用量(%), ...}
                 或 [{"name": "成分名", "limit": 限用量(%)}, ...]
3. --filings     备案流：{"product": 产品名, "ingredients": [成分...],
                          "claims": [宣称...]}
4. --inspections 抽检流：{"product": 产品名, "ingredient": 实测成分,
                          "content": 含量(%)}
5. --products    （可选）产品目录：["产品名", ...]
                 提供后，备案引用目录外产品即报 UNKNOWN_PRODUCT；
                 不提供时视为“先备案先注册”，跳过该检查。

校验规则
--------
R1  备案含禁用成分            -> 拦截该备案（REJECTED）并报告 BANNED_INGREDIENT
R2  限用成分超限              -> 以抽检实测含量对照限用量，超限报告
                               RESTRICTED_EXCEEDED 并定位到成分与产品
R3  宣称不含某成分但成分表含   -> 报告 CLAIM_FREE_CONTRADICTION
                               （识别“不含X / 无X / 无添加X / 零X”句式，
                                 含别名表：酒精=乙醇、防腐剂、香精、色素等）
R4  宣称功效与成分不符         -> 报告 CLAIM_EFFICACY_MISMATCH
                               （自定义功效规则表 EFFICACY_RULES，见文件头部
                                 注释：功效宣称必须能被成分表中至少一种
                                 对应功效成分支撑，否则构成误导性宣称）
R5  宣称含某成分但成分表没有   -> 报告 CLAIM_CONTAINS_MISSING（“含X/添加X”句式）
R6  抽检实测成分与备案成分不符 -> 报告 INSPECTION_MISMATCH，并级联把对应备案
                               标记为违规（VIOLATED），即使此前已通过
R7  备案引用不存在的产品       -> 报告 UNKNOWN_PRODUCT（需提供 --products）
R8  同一产品重复备案           -> 报告 DUPLICATE_FILING，保留首条备案
R9  抽检引用不存在备案的产品   -> 报告 INSPECTION_WITHOUT_FILING

跨流状态延续：抽检结果会回写备案状态——备案先审（R1/R3/R4/R5/R7/R8），
抽检后审（R2/R6/R9），抽检发现的问题会把备案状态从 APPROVED 级联改为
VIOLATED；REJECTED 的备案保持拦截终态，问题仍全部记录。

输出
----
JSON 报告（--out 指定文件，缺省打印到 stdout）：
{
  "summary":  {"filings": n, "inspections": n, "errors": n, "warnings": n},
  "statuses": [{"product": ..., "status": "APPROVED|REJECTED|VIOLATED",
                "issues": [错误下标...]}],
  "errors":   [{"severity": "error|warning", "type": ..., "product": ...,
                "ingredient": ...|null, "message": ...}]
}
退出码：存在 error 级问题为 1，否则为 0（便于接入 CI/流水线）。
"""

import argparse
import json
import re
import sys

# ---------------------------------------------------------------------------
# 自定义功效规则表（R4 的依据）
# 理由：功效宣称属于对消费者的实质性承诺，必须有配方层面的支撑。若宣称某
# 功效而成分表中不存在任何一类公认实现该功效的活性物，则该宣称无法被
# 配方证实，构成误导性宣称。每类功效列出若干常见支撑成分（可按企业
# 实际原料库扩充）。
# ---------------------------------------------------------------------------
EFFICACY_RULES = {
    "美白": ["烟酰胺", "抗坏血酸", "维生素C", "熊果苷", "曲酸",
             "传明酸", "苯乙基间苯二酚", "光果甘草"],
    "淡斑": ["烟酰胺", "抗坏血酸", "熊果苷", "曲酸", "传明酸"],
    "防晒": ["二氧化钛", "氧化锌", "甲氧基肉桂酸乙基己酯", "奥克立林",
             "阿伏苯宗", "水杨酸乙基己酯"],
    "保湿": ["透明质酸", "透明质酸钠", "甘油", "丁二醇", "神经酰胺",
             "泛醇", "角鲨烷"],
    "抗皱": ["视黄醇", "胜肽", "羟丙基四氢吡喃三醇", "维生素E",
             "生育酚", "腺苷"],
    "抗衰老": ["视黄醇", "胜肽", "羟丙基四氢吡喃三醇", "维生素E",
               "生育酚", "腺苷"],
    "祛痘": ["水杨酸", "壬二酸", "茶树", "过氧化苯甲酰"],
    "舒缓": ["积雪草", "红没药醇", "尿囊素", "甘草酸二钾", "洋甘菊"],
}

# “不含X”类宣称的别名表：宣称用词 -> 成分表中可能出现的具体名称
FREE_CLAIM_ALIASES = {
    "酒精": ["酒精", "乙醇", "变性乙醇"],
    "防腐剂": ["苯氧乙醇", "羟苯甲酯", "羟苯乙酯", "羟苯丙酯",
               "羟苯丁酯", "苯甲酸", "山梨酸", "苯甲醇", "DMDM乙内酰脲"],
    "香精": ["香精", "香料", "日用香精"],
    "色素": ["CI ", "色淀", "焦糖色"],
    "矿物油": ["矿油", "矿物油", "液体石蜡"],
    "硅油": ["聚二甲基硅氧烷", "环五聚二甲基硅氧烷"],
    "激素": ["氢化可的松", "地塞米松", "氯倍他索"],
    "重金属": ["铅", "汞", "砷", "镉"],
}

FREE_CLAIM_RE = re.compile(
    r"^(?:不含|无添加|无|零)(?P<target>[\w\u4e00-\u9fff·（）()]+)$")
CONTAINS_CLAIM_RE = re.compile(
    r"^(?:含|添加|富含)(?P<target>[\w\u4e00-\u9fff·（）()]+)$")


def norm(name):
    """成分/产品名归一化：去空白、统一大小写（拉丁部分）。"""
    return "".join(str(name).split()).lower()


def load_records(path):
    """读取 JSON 数组或 JSONL（每行一条）。永远返回 list。"""
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]  # 单条记录（限用表 dict 格式由 load_restricted 处理）
    records = []
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{lineno}: JSON 解析失败: {exc}")
    return records


def load_restricted(path):
    """限用表：支持 {成分名: 限值} 或 [{name, limit}] / JSONL。"""
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read().strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = load_records(path)
    if isinstance(data, dict):
        return {norm(k): float(v) for k, v in data.items()}
    return {norm(r["name"]): float(r["limit"]) for r in data}


class Reporter:
    def __init__(self):
        self.errors = []

    def add(self, rtype, product, message, ingredient=None, severity="error"):
        self.errors.append({
            "severity": severity,
            "type": rtype,
            "product": product,
            "ingredient": ingredient,
            "message": message,
        })
        return len(self.errors) - 1  # 下标，供备案记录引用


def match_ingredient(target, ingredients_norm, ingredients_raw):
    """target 是否命中成分表：归一化相等或互为包含。返回原始成分名或 None。"""
    tnorm = norm(target)
    if not tnorm:
        return None
    for inorm, iraw in zip(ingredients_norm, ingredients_raw):
        if tnorm == inorm or tnorm in inorm or inorm in tnorm:
            return iraw
    return None


def free_claim_hit(target, ingredients_norm, ingredients_raw):
    """“不含X”宣称的命中检查，含别名表扩展。

    宣称原词允许子串匹配（如「透明质酸」命中「透明质酸钠」）；
    别名只做精确或前缀匹配，避免「乙醇」误中「苯氧乙醇」这类假阳性。
    """
    hit = match_ingredient(target, ingredients_norm, ingredients_raw)
    if hit is not None:
        return hit
    tnorm = norm(target)
    for alias in FREE_CLAIM_ALIASES.get(target, []):
        anorm = norm(alias)
        for inorm, iraw in zip(ingredients_norm, ingredients_raw):
            if inorm == anorm or inorm.startswith(anorm):
                return iraw
    return None


def check_filings(filings, banned, restricted, products, reporter):
    """备案侧校验（R1/R3/R4/R5/R7/R8）。返回 {产品: 备案记录}。"""
    registry = {}  # product -> {"ingredients":..., "claims":..., "status":..., "issues":[]}

    for idx, filing in enumerate(filings, 1):
        product = str(filing.get("product", "")).strip()
        ingredients = [str(x).strip() for x in filing.get("ingredients", [])]
        claims = [str(x).strip() for x in filing.get("claims", [])]
        if not product:
            reporter.add("INVALID_FILING", "", f"第 {idx} 条备案缺少产品名，已忽略")
            continue

        # R8 重复备案
        if product in registry:
            reporter.add("DUPLICATE_FILING", product,
                         f"产品重复备案（第 {idx} 条），保留首条备案")
            continue

        record = {"ingredients": ingredients, "claims": claims,
                  "status": "APPROVED", "issues": []}
        registry[product] = record

        # R7 引用不存在产品
        if products is not None and product not in products:
            record["issues"].append(reporter.add(
                "UNKNOWN_PRODUCT", product,
                "备案引用的产品不在产品目录中"))
            record["status"] = "REJECTED"
            continue  # 产品不存在，后续成分校验无意义

        ing_norm = [norm(x) for x in ingredients]

        # R1 禁用成分 -> 拦截
        for raw, inorm in zip(ingredients, ing_norm):
            if inorm in banned:
                record["issues"].append(reporter.add(
                    "BANNED_INGREDIENT", product,
                    f"备案含禁用成分「{raw}」，备案被拦截", ingredient=raw))
                record["status"] = "REJECTED"

        # 限用成分提示（限量需抽检数据核实，备案侧仅告警）
        for raw, inorm in zip(ingredients, ing_norm):
            if inorm in restricted:
                record["issues"].append(reporter.add(
                    "RESTRICTED_PRESENT", product,
                    f"含限用成分「{raw}」（限值 {restricted[inorm]}%），"
                    f"是否超限以抽检实测为准", ingredient=raw,
                    severity="warning"))

        # R3/R4/R5 宣称-成分一致性
        for claim in claims:
            m = FREE_CLAIM_RE.match(claim)
            if m:
                target = m.group("target")
                hit = free_claim_hit(target, ing_norm, ingredients)
                if hit is not None:
                    record["issues"].append(reporter.add(
                        "CLAIM_FREE_CONTRADICTION", product,
                        f"宣称「{claim}」与成分表矛盾：成分表含「{hit}」",
                        ingredient=hit))
                continue
            m = CONTAINS_CLAIM_RE.match(claim)
            if m:
                target = m.group("target")
                if match_ingredient(target, ing_norm, ingredients) is None:
                    record["issues"].append(reporter.add(
                        "CLAIM_CONTAINS_MISSING", product,
                        f"宣称「{claim}」但成分表中找不到「{target}」",
                        ingredient=target))
                continue
            for keyword, supporters in EFFICACY_RULES.items():
                if keyword in claim:
                    if not any(match_ingredient(s, ing_norm, ingredients)
                               for s in supporters):
                        record["issues"].append(reporter.add(
                            "CLAIM_EFFICACY_MISMATCH", product,
                            f"宣称「{claim}」但成分表不含任何{keyword}类"
                            f"支撑成分（如 {'/'.join(supporters[:3])} 等）"))
                    break
    return registry


def check_inspections(inspections, registry, restricted, reporter):
    """抽检侧校验（R2/R6/R9），并级联回写备案状态。"""
    for idx, insp in enumerate(inspections, 1):
        product = str(insp.get("product", "")).strip()
        ingredient = str(insp.get("ingredient", "")).strip()
        content = insp.get("content")

        # R9 抽检引用不存在备案
        record = registry.get(product)
        if record is None:
            reporter.add("INSPECTION_WITHOUT_FILING", product,
                         f"第 {idx} 条抽检引用的产品没有对应备案",
                         ingredient=ingredient)
            continue

        ing_norm = [norm(x) for x in record["ingredients"]]

        # R6 实测成分与备案成分不一致 -> 级联标记违规
        if match_ingredient(ingredient, ing_norm,
                            record["ingredients"]) is None:
            record["issues"].append(reporter.add(
                "INSPECTION_MISMATCH", product,
                f"抽检实测成分「{ingredient}」不在备案成分表中，"
                f"备案级联标记为违规", ingredient=ingredient))
            if record["status"] == "APPROVED":
                record["status"] = "VIOLATED"

        # R2 限用成分超限 -> 定位成分
        inorm = norm(ingredient)
        if inorm in restricted and content is not None:
            try:
                value = float(content)
            except (TypeError, ValueError):
                reporter.add("INVALID_CONTENT", product,
                             f"抽检含量 {content!r} 不是数值，无法比对限值",
                             ingredient=ingredient)
                continue
            limit = restricted[inorm]
            if value > limit:
                record["issues"].append(reporter.add(
                    "RESTRICTED_EXCEEDED", product,
                    f"限用成分「{ingredient}」实测 {value}% 超过限值 "
                    f"{limit}%", ingredient=ingredient))
                if record["status"] == "APPROVED":
                    record["status"] = "VIOLATED"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="化妆品备案/抽检合规校验工具（详见文件头部说明）")
    parser.add_argument("--banned", required=True, help="禁用成分清单 JSON")
    parser.add_argument("--restricted", required=True, help="限用成分表 JSON")
    parser.add_argument("--filings", required=True, help="备案流 JSON/JSONL")
    parser.add_argument("--inspections", required=True, help="抽检流 JSON/JSONL")
    parser.add_argument("--products", help="产品目录 JSON（可选）")
    parser.add_argument("--out", help="报告输出文件（缺省 stdout）")
    args = parser.parse_args(argv)

    banned = {norm(x) for x in load_records(args.banned)}

    restricted = load_restricted(args.restricted)

    filings = load_records(args.filings)
    inspections = load_records(args.inspections)
    products = None
    if args.products:
        products = {str(p).strip() for p in load_records(args.products)}

    reporter = Reporter()
    registry = check_filings(filings, banned, restricted, products, reporter)
    check_inspections(inspections, registry, restricted, reporter)

    report = {
        "summary": {
            "filings": len(registry),
            "inspections": len(inspections),
            "errors": sum(1 for e in reporter.errors
                          if e["severity"] == "error"),
            "warnings": sum(1 for e in reporter.errors
                            if e["severity"] == "warning"),
        },
        "statuses": [
            {"product": p, "status": r["status"], "issues": r["issues"]}
            for p, r in registry.items()
        ],
        "errors": reporter.errors,
    }

    out = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(out + "\n")
    else:
        print(out)

    return 1 if report["summary"]["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
