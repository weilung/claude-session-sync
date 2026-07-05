"""合成 session 樣本（make_fork 的單元測試版）。確定性、純標準庫。"""
from __future__ import annotations

import json


def ts(n: int) -> str:
    return f"2026-06-18T00:00:{n:02d}.000Z"


def umsg(uuid: str, parent: str | None, mtype: str = "user", t: int = 0,
         sid: str = "s", sidechain: bool = False, **extra) -> dict:
    o = {
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": sid,
        "type": mtype,
        "timestamp": ts(t),
        "isSidechain": sidechain,
    }
    o.update(extra)
    return o


def lastprompt(leaf: str, sid: str = "s", prompt: str = "p") -> dict:
    return {"type": "last-prompt", "leafUuid": leaf, "sessionId": sid, "lastPrompt": prompt}


def summary(text: str = "sum") -> dict:
    return {"type": "summary", "isCompactSummary": True, "content": text}


def write_jsonl(objs: list[dict], path: str) -> str:
    # newline="" → 不做平台換行轉換：寫出的 "\n" 在 Windows 也保持 LF（真實 Claude JSONL 為 LF；
    # text-mode 預設會把 \n 譯成 \r\n，使 raw-bytes 斷言在 Windows 失敗）。
    with open(path, "w", encoding="utf-8", newline="") as f:
        for o in objs:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")
    return path


# ── 標準場景（回傳 list[dict]）────────────────────────────────────────────

def linear() -> list[dict]:
    """u1→u2→u3，last-prompt 指 u3。乾淨線性。"""
    return [
        umsg("u1", None, "user", 1),
        umsg("u2", "u1", "assistant", 2),
        umsg("u3", "u2", "user", 3),
        lastprompt("u3"),
    ]


def fast_forward_of_linear() -> list[dict]:
    """linear 再延伸一則 u4，last-prompt 指 u4。"""
    return [
        umsg("u1", None, "user", 1),
        umsg("u2", "u1", "assistant", 2),
        umsg("u3", "u2", "user", 3),
        umsg("u4", "u3", "assistant", 4),
        lastprompt("u4"),
    ]


def superset_branch_of_linear() -> list[dict]:
    """含 linear 全部，另從 u2 長出新枝 u9（較晚 ts），last-prompt 指 u9（一致）。"""
    return [
        umsg("u1", None, "user", 1),
        umsg("u2", "u1", "assistant", 2),
        umsg("u3", "u2", "user", 3),
        umsg("u9", "u2", "user", 9),
        lastprompt("u9"),
    ]


def stale_rewind_of_linear() -> list[dict]:
    """rewind 情境：含 linear 全部，另從 u2 長新枝 u9（更晚 ts），但 last-prompt 仍停在舊 tip u3。
    → active-tip 與最新 leaf 不一致 → 應 needs-decision（B2 stale last-prompt 保護）。"""
    return [
        umsg("u1", None, "user", 1),
        umsg("u2", "u1", "assistant", 2),
        umsg("u3", "u2", "user", 3),
        umsg("u9", "u2", "user", 9),
        lastprompt("u3"),  # 落後：指舊枝
    ]


def linear_with_title() -> list[dict]:
    """linear + 一條 custom-title（揮發 meta，比較時排除，但覆蓋不可靜默丟）。"""
    return [
        umsg("u1", None, "user", 1),
        umsg("u2", "u1", "assistant", 2),
        umsg("u3", "u2", "user", 3),
        {"type": "custom-title", "title": "我的對話", "sessionId": "s"},
        lastprompt("u3"),
    ]


def fork_of_linear() -> list[dict]:
    """與 linear 共享 u1,u2，但 tip 是 u4（u3 只在 linear）。互有對方沒有的行。"""
    return [
        umsg("u1", None, "user", 1),
        umsg("u2", "u1", "assistant", 2),
        umsg("u4", "u2", "user", 4),
        lastprompt("u4"),
    ]


def disjoint() -> list[dict]:
    """完全不同 uuid → 零共同 → identity-collision。"""
    return [
        umsg("a1", None, "user", 1),
        umsg("a2", "a1", "assistant", 2),
        lastprompt("a2"),
    ]


def compact_system_root() -> list[dict]:
    """linear 之後 compact：新增 system 根 + isCompactSummary，再續一則。
    與既有鏈有共同 uuid（u1..u3）→ 多根但非 collision。"""
    return [
        umsg("u1", None, "user", 1),
        umsg("u2", "u1", "assistant", 2),
        umsg("u3", "u2", "user", 3),
        umsg("sysroot", None, "system", 4),         # compact 新根
        umsg("c1", "sysroot", "user", 5, isCompactSummary=True),
        umsg("c2", "c1", "assistant", 6),
        lastprompt("c2"),
    ]


def disconnected_root_injection() -> list[dict]:
    """linear 全部（u3 為最新 ts、last-prompt 指 u3，故 active-tip 一致）外加一棵
    『非 system 的 disconnected 根』x1→x2（不相關對話）。superset 但**絕不可 ff**。
    舊規則(只看 small_tip 是 big_tip 祖先)會誤判 FAST_FORWARD → 注入不相關對話。"""
    return [
        umsg("u1", None, "user", 1),
        umsg("u2", "u1", "assistant", 2),
        umsg("u3", "u2", "user", 3),          # 與 linear 的 u3 同 ts → 維持 superset 關係
        umsg("x1", None, "user", 1),          # 注入的 disconnected 根（type=user）
        umsg("x2", "x1", "assistant", 2),     # 比 u3 早 → u3 仍是最新 leaf、active-tip 一致
        lastprompt("u3"),
    ]


def linear_extra_summary() -> list[dict]:
    """linear + 一條內容性 summary（no-uuid 但非揮發 meta）→ 比較時應計入。"""
    return [
        umsg("u1", None, "user", 1),
        umsg("u2", "u1", "assistant", 2),
        umsg("u3", "u2", "user", 3),
        summary("討論摘要 X"),
        lastprompt("u3"),
    ]


def linear_diff_volatile_only() -> list[dict]:
    """與 linear 對話完全相同，只有揮發性 meta 不同（多一條 mode + last-prompt 指 u3）。
    比較應排除揮發 meta → 不得判成 fork。"""
    return [
        umsg("u1", None, "user", 1),
        umsg("u2", "u1", "assistant", 2),
        umsg("u3", "u2", "user", 3),
        {"type": "mode", "mode": "default", "sessionId": "s"},
        lastprompt("u3"),
    ]


def linear_no_lastprompt() -> list[dict]:
    """無 last-prompt 的線性鏈（active_tip=None；單一 genuine leaf u3）。"""
    return [
        umsg("u1", None, "user", 1),
        umsg("u2", "u1", "assistant", 2),
        umsg("u3", "u2", "user", 3),
    ]


def ff_no_lastprompt() -> list[dict]:
    """linear_no_lastprompt 再延伸 u4（仍無 last-prompt，單一 genuine leaf u4）。"""
    return [
        umsg("u1", None, "user", 1),
        umsg("u2", "u1", "assistant", 2),
        umsg("u3", "u2", "user", 3),
        umsg("u4", "u3", "assistant", 4),
    ]


def two_new_genuine_leaves() -> list[dict]:
    """linear 全部 + 從舊 tip u3 長出「兩條」新 genuine 葉 u4、u5（last-prompt 指 u4）。
    這是分枝，**不可自動 ff**（codex r4）。"""
    return [
        umsg("u1", None, "user", 1),
        umsg("u2", "u1", "assistant", 2),
        umsg("u3", "u2", "user", 3),
        umsg("u4", "u3", "assistant", 4),
        umsg("u5", "u3", "assistant", 5),
        lastprompt("u4"),
    ]


def active_tip_missing() -> list[dict]:
    """純延伸但 last-prompt 指向不存在的 uuid → active-tip 無效 → 不可 ff。"""
    return [
        umsg("u1", None, "user", 1),
        umsg("u2", "u1", "assistant", 2),
        umsg("u3", "u2", "user", 3),
        umsg("u4", "u3", "assistant", 4),
        lastprompt("zzzzzzzz-0000-0000-0000-000000000000"),
    ]


def active_tip_to_fanout() -> list[dict]:
    """last-prompt 指向工具 fan-out 行（非 genuine leaf）→ 不可 ff。"""
    return [
        umsg("u1", None, "user", 1),
        umsg("u2", "u1", "assistant", 2),
        umsg("u3", "u2", "user", 3),
        umsg("u4", "u3", "assistant", 4),                                   # genuine leaf
        umsg("tr1", "u3", "user", 4, toolUseResult={"stdout": "x"}),        # fan-out
        lastprompt("tr1"),
    ]


def linear_u2_rewritten() -> list[dict]:
    """與 linear 同 uuid，但 u2 內容被改寫（hash 不同）→ 跨檔同 uuid 異 hash → DAMAGED。"""
    return [
        umsg("u1", None, "user", 1),
        umsg("u2", "u1", "assistant", 2, content="REWRITTEN"),
        umsg("u3", "u2", "user", 3),
        lastprompt("u3"),
    ]


def with_tool_fanout() -> list[dict]:
    """一個 assistant 發兩個 tool → 兩條 user+toolUseResult 共用 parent；只有一條被續。"""
    return [
        umsg("u1", None, "user", 1),
        umsg("u2", "u1", "assistant", 2),
        umsg("tr1", "u2", "user", 3, toolUseResult={"stdout": "a"}),  # fan-out leaf
        umsg("tr2", "u2", "user", 3, toolUseResult={"stdout": "b"}),  # 被續
        umsg("u5", "tr2", "assistant", 4),
        lastprompt("u5"),
    ]
