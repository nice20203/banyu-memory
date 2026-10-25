#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记忆库 · 跨会话长期记忆，按权重遗忘（半衰）

这是 banyu-memory 的 memory 部分：一个不依赖 DSH 的独立记忆库。
用法：
    python memory.py init                    # 建库（第一次）
    python memory.py add-fact "她怕黑"        # 加一条事实（默认 base 1）
    python memory.py add-fact "她生日是三月" --base 5   # 刻石头（永不衰减）
    python memory.py add-agreement 称呼 本鱼
    python memory.py context                 # 生成给 AI 的上下文块
    python memory.py recall "考试"           # 搜索
    python memory.py stats                   # 库大小统计
    python memory.py prune                   # 清理 30 天前的普通记忆（半衰过期的）

设计：
- facts 表按 base 权重决定遗忘速度：
    base 5 = 刻石头（约定/方向/身份）—— 永不衰减
    base 4 = 教训/情绪浓/里程碑 —— 半衰 180 天
    base 3 = 有用知识 —— 半衰 90 天
    base 1 = 普通偏好/琐事 —— 半衰 30 天
- 半衰只影响「取出来时的分量」，不删原文。
- 取用按 decayed_score 排序：分量 × 时间衰减 × 命中次数。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
DB = ROOT / "memory.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    fact      TEXT NOT NULL,
    kind      TEXT DEFAULT 'general',
    base      INTEGER DEFAULT 1,
    sensitive INTEGER DEFAULT 0,
    hits      INTEGER DEFAULT 1,
    created   TEXT NOT NULL,
    updated   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_facts_text ON facts(fact);

CREATE TABLE IF NOT EXISTS agreements (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    key       TEXT NOT NULL UNIQUE,
    value     TEXT NOT NULL,
    created   TEXT NOT NULL,
    updated   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS digests (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    day       TEXT NOT NULL,
    source    TEXT NOT NULL DEFAULT 'chat',
    summary   TEXT NOT NULL,
    created   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_digests_day ON digests(day);
"""

# 半衰天数：base -> 天。None = 永不衰减。
HALF_LIFE = {5: None, 4: 180, 3: 90}
DEFAULT_HALF_LIFE = 30


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init() -> None:
    with conn() as c:
        c.executescript(SCHEMA)
    print(f"已建库：{DB}")


def _looks_sensitive(text: str) -> bool:
    return any(k in text for k in ("密码", "裸照", "身份证号", "银行卡号"))


def add_fact(fact: str, kind: str = "general", base: int = 1,
             sensitive: bool | None = None) -> str:
    if sensitive is None:
        sensitive = _looks_sensitive(fact)
    base = max(1, min(5, int(base)))
    ts = now()
    with conn() as c:
        row = c.execute("SELECT id, hits FROM facts WHERE fact=?", (fact,)).fetchone()
        if row:
            c.execute(
                "UPDATE facts SET hits=hits+1, updated=?, base=?, kind=? WHERE id=?",
                (ts, base, kind, row["id"]),
            )
            msg = f"已更新（第 {row['hits'] + 1} 次提到）：{fact}"
        else:
            c.execute(
                "INSERT INTO facts(fact, kind, base, sensitive, created, updated) VALUES(?,?,?,?,?,?)",
                (fact, kind, base, int(sensitive), ts, ts),
            )
            msg = f"已记下（base {base}）：{fact}"
    return msg


def add_agreement(key: str, value: str) -> str:
    ts = now()
    with conn() as c:
        c.execute(
            "INSERT INTO agreements(key, value, created, updated) VALUES(?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
            (key, value, ts, ts),
        )
    return f"约定已记：{key} = {value}"


def add_digest(summary: str, source: str = "chat") -> str:
    with conn() as c:
        c.execute(
            "INSERT INTO digests(day, source, summary, created) VALUES(?,?,?,?)",
            (today(), source, summary, now()),
        )
    return "摘要已记"


def decayed_score(base: int, hits: int, updated: str) -> float:
    """base 越高越不容易忘；普通记忆按时间半衰；命中次数有加成。"""
    base = max(1, min(5, int(base)))
    half = HALF_LIFE.get(base, DEFAULT_HALF_LIFE)
    if half is None:
        return 1e9  # 刻石头：永远排最前
    try:
        age_days = max(0.0, (datetime.now() - datetime.strptime(updated, "%Y-%m-%d %H:%M:%S")).total_seconds() / 86400)
    except Exception:
        age_days = 0.0
    decay = 0.5 ** (age_days / half)
    return (1.0 + 0.15 * max(0, int(hits) - 1)) * decay * base


def ranked_facts(c: sqlite3.Connection, limit: int) -> list:
    rows = c.execute("SELECT * FROM facts WHERE sensitive=0").fetchall()
    rows = sorted(rows, key=lambda r: decayed_score(r["base"], r["hits"], r["updated"]), reverse=True)
    return rows[:limit]


def recall(query: str = "", limit: int = 8) -> dict:
    with conn() as c:
        results = []
        if query:
            rows = c.execute(
                "SELECT * FROM facts WHERE sensitive=0 AND fact LIKE ? ORDER BY updated DESC LIMIT ?",
                (f"%{query}%", limit),
            ).fetchall()
        else:
            rows = ranked_facts(c, limit)
        for r in rows:
            results.append({"fact": r["fact"], "base": r["base"], "hits": r["hits"]})
        ags = c.execute("SELECT key, value FROM agreements ORDER BY id").fetchall()
        return {"facts": results, "agreements": {a["key"]: a["value"] for a in ags}}


def context_for_ai(max_facts: int = 12) -> str:
    with conn() as c:
        ags = c.execute("SELECT key, value FROM agreements ORDER BY id").fetchall()
        facts = ranked_facts(c, max_facts)
        digests = c.execute(
            "SELECT day, summary FROM digests ORDER BY id DESC LIMIT 4"
        ).fetchall()
    lines = []
    if ags:
        lines.append("## 约定\n" + "\n".join(f"- {a['key']}：{a['value']}" for a in ags))
    if facts:
        lines.append("## 我记着的事")
        for f in facts:
            mark = {5: "【刻石头】", 4: "【重要】", 3: "【知识】"}.get(int(f["base"]), "")
            lines.append(f"- {mark}{f['fact']}")
    if digests:
        lines.append("## 最近聊过")
        for d in digests:
            lines.append(f"- {d['day']}：{d['summary']}")
    return "\n\n".join(lines)


def stats() -> str:
    with conn() as c:
        parts = []
        for table in ("facts", "agreements", "digests"):
            try:
                n = c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                parts.append(f"{table} {n} 条")
            except Exception:
                pass
        n5 = c.execute("SELECT COUNT(*) FROM facts WHERE base>=5").fetchone()[0]
        parts.append(f"刻石头 {n5} 条")
    return " | ".join(parts)


def prune(days: int = 30) -> str:
    """清理半衰过期的普通记忆（base<=1 且超过 days 天没更新）。"""
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with conn() as c:
        cur = c.execute("DELETE FROM facts WHERE base<=1 AND updated < ?", (cutoff,))
        return f"清理了 {cur.rowcount} 条普通记忆（超过 {days} 天）"


def main() -> int:
    ap = argparse.ArgumentParser(description="记忆库：跨会话长期记忆 + 半衰遗忘")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")
    p = sub.add_parser("add-fact")
    p.add_argument("fact")
    p.add_argument("--kind", default="general")
    p.add_argument("--base", type=int, default=1)
    p.add_argument("--sensitive", action="store_true")
    p = sub.add_parser("add-agreement")
    p.add_argument("key")
    p.add_argument("value")
    p = sub.add_parser("digest")
    p.add_argument("summary")
    p = sub.add_parser("context")
    p.add_argument("--max", type=int, default=12)
    p = sub.add_parser("recall")
    p.add_argument("query", nargs="?", default="")
    p = sub.add_parser("stats")
    p = sub.add_parser("prune")
    p.add_argument("--days", type=int, default=30)

    args = ap.parse_args()
    if args.cmd == "init":
        init()
    elif args.cmd == "add-fact":
        print(add_fact(args.fact, args.kind, args.base, args.sensitive))
    elif args.cmd == "add-agreement":
        print(add_agreement(args.key, args.value))
    elif args.cmd == "digest":
        print(add_digest(args.summary))
    elif args.cmd == "context":
        print(context_for_ai(args.max))
    elif args.cmd == "recall":
        print(json.dumps(recall(args.query), ensure_ascii=False, indent=2))
    elif args.cmd == "stats":
        print(stats())
    elif args.cmd == "prune":
        print(prune(args.days))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
