# -*- coding: utf-8 -*-
"""把已核对通过的待审 json 合并进 kanban/data.json 的 today 区
用法: python merge_pending.py <待审json...>
"""
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "kanban", "data.json")


def main():
    d = json.load(open(DATA, encoding="utf-8"))
    today = d["today"]
    n_notify = n_chat = n_draft = 0
    for p in sys.argv[1:]:
        doc = json.load(open(p, encoding="utf-8"))
        for e in doc.get("notify") or []:
            if e not in today.setdefault("notify", []):
                today["notify"].append(e)
                n_notify += 1
        for e in doc.get("brief_chat") or []:
            if e not in today.setdefault("brief_chat", []):
                today["brief_chat"].append(e)
                n_chat += 1
        draft = doc.get("daily_draft") or {}
        for k in ("brief", "review"):
            if draft.get(k):
                today[k] = draft[k]
                n_draft += 1
        for k in ("voice",):
            if draft.get(k):
                existing = today.setdefault(k, [])
                for v in draft[k]:
                    if v not in existing:
                        existing.append(v)
                        n_draft += 1
    today["date"] = "2026-09-27"
    with open(DATA, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    print(f"合并完成: notify +{n_notify}, brief_chat +{n_chat}, draft字段 {n_draft}",
          flush=True)


if __name__ == "__main__":
    main()
