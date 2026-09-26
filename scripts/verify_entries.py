# -*- coding: utf-8 -*-
"""看板写入前核对闸门（数据核实铁律的自动执行器）

流程铁律：分析产出条目 → 跑本脚本核对 → 全部 PASS 才能写入看板/飞书推送。
有 FAIL 的条目一律拦截：不写入、不推送，修掉或删除后才能放行。

核对项（逐条硬核对）：
  1. 引文溯源：每条 quotes 必须在当天导出原件里逐字命中（归一化去空白后子串匹配）
  2. 归因正确：quote 必须命中在 source 对应的群文件里，不是别的群
  3. 时间一致：quote 命中行的 [YYYY-MM-DD HH:MM] 必须等于条目 time
  4. 日期一致：命中行日期必须等于条目归档日期（抓错日期挂档的）
  5. 时间具体：schedule/活动条目的 time 不许用「白天/晚上」等模糊词，要么 HH:MM，要么标「具体时间不明」
  6. 引文必填：notify/brief_chat/activities_new 条目必须有至少一条 quotes，没引文没法核对，直接 FAIL
  7. 校区标注：条目涉及选课/活动/讲座等，且原文无校区字眼 → WARN 建议标注「校区不明」

用法:
    python verify_entries.py <待审json路径> [--date YYYY-MM-DD]
退出码: 0=全部PASS(可写入可推送)  1=有FAIL(拦截)  2=脚本自身错误
报告: 写到 <待审json同目录>/<文件名去后缀>_核对报告.json

导出目录：config.json 的「导出目录」字段指定；留空默认 <技能目录>\\wechat-export\\export
校区关键词：config.json 的「校区关键词」字段可扩展（默认["校区"]）
"""
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_JSON = os.path.join(BASE, "config.json")


def _cfg():
    try:
        with open(CONFIG_JSON, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def export_root():
    p = (_cfg().get("导出目录") or "").strip()
    if p and os.path.isdir(p):
        return p
    return os.path.join(BASE, "wechat-export", "export")


EXPORT_ROOT = export_root()

VAGUE_TIMES = {"白天", "晚上", "上午", "下午", "中午", "傍晚", "夜里", "凌晨", "全天", "整天", "早上", "晚间"}
# 涉及地点性事务的关键词（触发「校区不明」检查，按你的场景自定义）
CAMPUS_KEYWORDS = ("选课", "活动", "讲座", "摆摊", "宣讲", "地点", "见面", "面试", "报到", "比赛", "竞选", "训练")
# 原文里出现这些词即认为已指明校区（config.json「校区关键词」可扩展）
CAMPUS_IN_SOURCE = tuple(_cfg().get("校区关键词") or ["校区"])
TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\]")


def norm_name(s):
    """来源名/文件名归一化：只留字母数字汉字"""
    return "".join(ch for ch in s if ch.isalnum() or "一" <= ch <= "鿿")


def norm_text(s):
    """消息文本归一化：去所有空白，用于 quote 逐字比对"""
    return re.sub(r"\s+", "", s or "")


def load_corpus(date):
    """加载当天导出原件。优先 <date>_full（全天），fallback <date>（增量）。
    返回 {norm文件名: {blocks: [文本], ts_by_block: [HH:MM], date: str}}"""
    corpus = {}
    for sub in (f"{date}_full", date):
        d = os.path.join(EXPORT_ROOT, sub)
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if not fn.endswith(".md"):
                continue
            with open(os.path.join(d, fn), encoding="utf-8") as f:
                blocks, ts_list, block_dates = [], [], []
                cur_ts, cur_date, cur_block = None, None, ""
                for line in f:
                    line = line.rstrip("\n")
                    m = TS_RE.match(line.strip())
                    if m:
                        # 新消息时间戳：先收尾上一块，再开新块
                        if cur_ts is not None and cur_block:
                            blocks.append(norm_text(cur_block))
                            ts_list.append(cur_ts)
                            block_dates.append(cur_date)
                        cur_date, cur_ts = m.group(1), m.group(2)
                        # 无发送者的消息：内容紧跟时间戳在同一行，] 后的文本就是消息内容，别丢
                        cur_block = line.strip()[m.end():].lstrip()
                        continue
                    if not line.strip():
                        continue
                    if cur_ts is None:
                        continue
                    cur_block += line  # 同一时间戳的多行（含发送者行）合并为一条消息块
                if cur_ts is not None and cur_block:
                    blocks.append(norm_text(cur_block))
                    ts_list.append(cur_ts)
                    block_dates.append(cur_date)
            corpus[norm_name(fn)] = {"blocks": blocks, "ts": ts_list, "dates": block_dates}
        if corpus:
            return corpus
    return {}


def load_biz_articles(date):
    """公众号条目备查源：biz_articles.json 的 name/title 列表。
    返回 {"names": [norm公众号名], "titles": [norm标题]}"""
    for sub in (f"{date}_full", date):
        p = os.path.join(EXPORT_ROOT, sub, "biz_articles.json")
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    arts = json.load(f)
                return {
                    "names": [norm_text(a.get("name", "")) for a in arts],
                    "titles": [norm_text(a.get("title", "")) for a in arts],
                }
            except Exception:
                return {"names": [], "titles": []}
    return {"names": [], "titles": []}


def find_source_file(corpus, source):
    """source → 匹配的 corpus 文件（归一化后包含/前缀）。返回 None 表示该群当天无导出"""
    sn = norm_name(source)
    if not sn:
        return None
    for fn in corpus:
        if fn.startswith(sn) or sn in fn:
            return fn
    return None


_corpus_cache = {}


def get_corpus(d):
    """带缓存的 corpus 加载（顺带查前后一天用）"""
    if d not in _corpus_cache:
        _corpus_cache[d] = load_corpus(d)
    return _corpus_cache[d]


def verify_quote(corpus, source, quote, date, biz):
    """核对单条 quote。返回 (verdict, ts_or_None, hit_file_or_None, issue)
    verdict: hit=在source文件命中 / cross=在其他文件命中 / biz=公众号title命中 / miss=无源"""
    qn = norm_text(quote)
    if not qn:
        return "miss", None, None, "quote为空"

    fn = find_source_file(corpus, source)
    if fn:
        data = corpus[fn]
        for i, block in enumerate(data["blocks"]):
            if qn in block:
                ts, d = data["ts"][i], data["dates"][i]
                if d != date:
                    return "miss", ts, fn, f"日期不符: 命中日期{d} ≠ 归档日期{date}（错挂档？）"
                return "hit", ts, fn, None

    # source 文件里没找到 → 全目录找
    for ffn, data in corpus.items():
        for i, block in enumerate(data["blocks"]):
            if qn in block:
                return "cross", data["ts"][i], ffn, None

    # 公众号 title 兜底
    if biz and any(qn in t for t in biz["titles"]):
        return "biz", None, "biz_articles.json", None

    # 当天哪儿都没有 → 顺带查前后一天目录（日期错挂的常见情况）
    try:
        import datetime as _dt
        day = _dt.date.fromisoformat(date)
        for d in (day - _dt.timedelta(days=1), day + _dt.timedelta(days=1)):
            ds = d.isoformat()
            if ds in _corpus_cache:
                continue
            c2 = load_corpus(ds)
            if not c2:
                continue
            for ffn, data in c2.items():
                for i, block in enumerate(data["blocks"]):
                    if qn in block:
                        return "miss", None, None, f"疑似日期错挂: 原文在 {ds} 导出[{ffn}]里找到，条目却挂在 {date}"
    except Exception:
        pass

    if fn is None:
        return "miss", None, None, f"无源: 原文在当天导出里找不到，且[{source}]当天无导出文件"
    return "miss", None, None, f"无源: 原文在当天导出里逐字找不到（疑似捏造/转述）"


def check_time_specific(time_str):
    """模糊时间检测。返回 (ok, issue)"""
    t = (time_str or "").strip()
    if not t:
        return False, "缺时间"
    if t in VAGUE_TIMES:
        return False, f"时间模糊[{t}]: 必须写 HH:MM 具体时间，查不到就标「具体时间不明」"
    if re.match(r"^\d{2}:\d{2}", t):
        return True, None
    if "不明" in t or "无记录" in t:
        return True, None
    return True, None  # 其他格式（如 21:30（21:23 可入会））放行


def check_campus(entry, quotes, has_campus_field):
    """校区标注启发式：涉及地点性事务且无校区信息 → WARN"""
    joined = entry.get("text", "") + entry.get("title", "") + entry.get("detail", "")
    if not any(k in joined for k in CAMPUS_KEYWORDS):
        return None
    if has_campus_field:
        return None
    if any(k in (q or "") for q in quotes for k in CAMPUS_IN_SOURCE):
        return None
    if any(k in joined for k in CAMPUS_IN_SOURCE):
        return None
    return "建议标注「校区不明」：涉及选课/活动/讲座类，原文无校区信息"


def verify_entry(corpus, entry, date, kind, biz):
    """核对单条条目。返回 [issue, ...]（空=通过）；逐条 issue 带 fail 标记"""
    issues, fails = [], []
    quotes = entry.get("quotes") or []

    if kind in ("notify", "brief_chat", "activities_new", "brief_mp"):
        # 引文核对：逐条判定 hit/cross/biz/miss
        hit_ts, cross_files, miss_reasons, has_source_hit = [], [], [], False
        for q in quotes:
            verdict, ts, hit, issue = verify_quote(corpus, entry.get("source", ""), q, date, biz)
            if verdict == "hit":
                has_source_hit = True
                if ts is not None:
                    hit_ts.append(ts)
            elif verdict == "cross":
                cross_files.append((q[:30], hit))
                if ts is not None:
                    hit_ts.append(ts)
            elif verdict == "biz":
                pass  # 公众号标题命中，放行
            else:
                miss_reasons.append(f"引文「{q[:30]}」: {issue}")

        if kind == "brief_mp":
            # 公众号条目：无引文不拦；但公众号名必须存在于当天 biz_articles.json
            if not quotes:
                issues.append("WARN: 公众号条目无引文，建议从 biz_articles.json 补文章标题")
            if biz and biz["names"]:
                text = norm_text(entry.get("text", ""))
                name_ok = any(n and n in text for n in biz["names"])
                if not name_ok:
                    fails.append(f"公众号无源: 当天 biz_articles.json 里没有[{entry.get('source','')}]推送")
            for r in miss_reasons:
                fails.append(r)
        else:
            # 群聊/通知条目：无引文=FAIL；跨群引文看主体归因
            if not quotes:
                fails.append("无引文: 没有 quotes 无法核对，必须补原文引用")
            for r in miss_reasons:
                fails.append(r)
            for q_head, f in cross_files:
                if has_source_hit:
                    issues.append(f"WARN: 引文「{q_head}…」实际来自[{f}]（跨群聚合，请在 detail 注明）")
                else:
                    fails.append(f"归因错误: 引文「{q_head}…」原文在[{f}]，条目却写[{entry.get('source','')}]")

        # 时间核对：单引文硬比对；多引文聚合条目，time 命中任一引文时间戳即算过，否则 WARN
        if hit_ts and kind != "brief_mp":
            entry_t = (entry.get("time") or "").strip()
            if len(hit_ts) == 1:
                if entry_t and entry_t != hit_ts[0]:
                    fails.append(f"时间不符: 条目写[{entry_t}]，原文时间戳是[{hit_ts[0]}]")
            else:
                if entry_t and entry_t not in hit_ts:
                    issues.append(f"WARN: 条目时间[{entry_t}]未落在引文时间戳{hit_ts}内，确认它是话题起始时间")

    # 时间具体性（schedule/活动必查；notify 也查，模糊时间同样该拦）
    if kind in ("schedule", "activities_new", "notify"):
        ok, issue = check_time_specific(entry.get("time", ""))
        if not ok:
            fails.append(issue)

    # 校区标注提示
    has_campus = bool(entry.get("campus")) or ("校区" in (entry.get("place") or ""))
    cw = check_campus(entry, quotes, has_campus)
    if cw:
        issues.append("WARN: " + cw)

    return issues, fails


def main():
    if len(sys.argv) < 2:
        print("用法: python verify_entries.py <待审json路径> [--date YYYY-MM-DD]", flush=True)
        sys.exit(2)
    path = sys.argv[1]
    date = None
    if "--date" in sys.argv:
        date = sys.argv[sys.argv.index("--date") + 1]
    if not date:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(path))
        date = m.group(1) if m else None
    if not date:
        print("无法从文件名判断日期，请用 --date 指定", flush=True)
        sys.exit(2)

    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as e:
        print(f"读待审文件失败: {e}", flush=True)
        sys.exit(2)

    corpus = load_corpus(date)
    if not corpus:
        print(f"核对失败: {date} 当天导出目录不存在或无 md 文件，禁止放行", flush=True)
        sys.exit(1)
    biz_titles = load_biz_articles(date)

    report = {"date": date, "file": os.path.basename(path),
              "corpus_files": sorted(corpus.keys()), "entries": []}
    total = fails_n = warns_n = 0

    def walk(entries, kind, label):
        nonlocal total, fails_n, warns_n
        for i, e in enumerate(entries):
            total += 1
            issues, fails = verify_entry(corpus, e, date, kind, biz_titles)
            verdict = "FAIL" if fails else ("WARN" if issues else "PASS")
            if fails:
                fails_n += 1
            elif issues:
                warns_n += 1
            report["entries"].append({
                "index": i, "kind": kind, "label": label,
                "head": (e.get("text") or e.get("title") or "")[:50],
                "verdict": verdict, "fails": fails, "issues": issues,
            })

    walk(doc.get("notify") or [], "notify", "通知")
    walk(doc.get("brief_chat") or [], "brief_chat", "群聊知识")
    walk(doc.get("brief_mp") or [], "brief_mp", "公众号")
    walk(doc.get("activities_new") or [], "activities_new", "活动")
    draft = doc.get("daily_draft") or {}
    walk(draft.get("activities_new") or [], "activities_new", "活动(日报稿)")
    for sched in draft.get("schedule") or []:
        for e in sched.get("items") or []:
            walk([e], "schedule", "行程")

    report.update({"total": total, "pass": total - fails_n - warns_n,
                   "fail": fails_n, "warn": warns_n})
    out = os.path.join(os.path.dirname(path) or ".",
                       os.path.splitext(os.path.basename(path))[0] + "_核对报告.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(f"核对完成 [{date}] 共{total}条: PASS {report['pass']} / WARN {warns_n} / FAIL {fails_n}", flush=True)
    for e in report["entries"]:
        if e["verdict"] != "PASS":
            print(f"  [{e['verdict']}] {e['kind']}#{e['index']} {e['head']}", flush=True)
            for x in e["fails"]:
                print(f"      ✗ {x}", flush=True)
            for x in e["issues"]:
                print(f"      ! {x}", flush=True)
    print(f"报告: {out}", flush=True)
    sys.exit(0 if fails_n == 0 else 1)


if __name__ == "__main__":
    main()
