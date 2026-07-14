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


# ── 「回合中途快照」：last-prompt 落後（指向已有子節點的 user 行）─────────────────
# 實機 2026-07-14（a9f7f783）：hub 快照取自「助理已回覆、使用者尚未送出下一則」的時刻 →
# 檔內最後一條 last-prompt 指向那則 user 訊息，而它在同一檔內已有子節點 → 非葉。
# 這是常態（session 結束→備份→resume 續寫→再同步），不是壞檔 → smaller 側須容許（`_lagging_tip`）。
#
# **行序至關重要**（codex g1）：真實檔的 last-prompt 是在**送出 prompt 當下**寫入的，助理回覆行
# 追加在它**之後**（實機驗證：hub 檔 last-prompt 在 index 284、唯一葉在 index 294）。把 last-prompt
# 放在葉子**之後**的形狀，語意是「使用者刻意 rewind 回祖先」——完全不同的東西，必須擋下。

def mid_turn_snapshot() -> list[dict]:
    """u1→u2→u3→u4；last-prompt 指 u3，且**寫在 u4 之前**（送出 u3 時寫入，助理回覆 u4 追加在後）。
    → 指標非葉但構造上必為過期；唯一 genuine leaf = u4，無歧義。"""
    return [
        umsg("u1", None, "user", 1),
        umsg("u2", "u1", "assistant", 2),
        umsg("u3", "u2", "user", 3),
        lastprompt("u3"),          # 送出 u3 當下寫入 —— 此刻 u4 還不存在
        umsg("u4", "u3", "assistant", 4),   # 助理回覆追加在後 → 指標自然落後
    ]


def mid_turn_continued() -> list[dict]:
    """mid_turn_snapshot 之後續寫 u5、u6（單線延伸），收尾 last-prompt 指 u6（＝其唯一葉，一致）。
    對應實機 local 側（session 結束時 CC 會補寫指向真葉的 last-prompt）。"""
    return [
        umsg("u1", None, "user", 1),
        umsg("u2", "u1", "assistant", 2),
        umsg("u3", "u2", "user", 3),
        lastprompt("u3"),
        umsg("u4", "u3", "assistant", 4),
        umsg("u5", "u4", "user", 5),
        umsg("u6", "u5", "assistant", 6),
        lastprompt("u6"),
    ]


def rewound_to_ancestor() -> list[dict]:
    """**codex g1 的 High 反例**：u4 已存在後，使用者刻意把游標 rewind 回 u3（放棄 u4 那一枝）→
    last-prompt(u3) 寫在 u4 **之後**。圖形與 `mid_turn_snapshot` 相同（同一組 uuid/parent、指標同為
    u3、唯一葉同為 u4），**只有行序不同** → 若誤判為「過期」而 ff local->hub，會覆蓋 hub、把這個
    刻意的游標改掉 ＝ 靜默丟失使用者的 rewind 意圖。條件 4 專擋此形。"""
    return [
        umsg("u1", None, "user", 1),
        umsg("u2", "u1", "assistant", 2),
        umsg("u3", "u2", "user", 3),
        lastprompt("u3"),
        umsg("u4", "u3", "assistant", 4),
        lastprompt("u3"),          # u4 之後才寫 → 刻意退回，不是落後
    ]


def meta_line_with_uuid_branch() -> list[dict]:
    """**codex g3 的反例**：與 linear 共享 u1,u2，但其「tip」是一條**帶 uuid 的 last-prompt meta 行** m1
    （ts 最新）。舊行為：union 把 m1 當內容搬進去、當 genuine leaf、依 ts 自動選成 chosen tip →
    合併檔的 last-prompt 指向 metadata。"""
    return [
        umsg("u1", None, "user", 1),
        umsg("u2", "u1", "assistant", 2),
        {"type": "last-prompt", "uuid": "m1", "parentUuid": "u2", "leafUuid": "u2",
         "sessionId": "s", "lastPrompt": "p", "timestamp": ts(9)},
    ]


def meta_node_as_dag_link() -> list[dict]:
    """**codex g9 反例**：對話鏈 u1 → **m1（帶 uuid 的 last-prompt 簿記行）** → u2。
    ff 的祖先證明 `is_ancestor(u1 → u2)` 會**穿過 m1**——但我們自己宣告 m1 不是對話節點（union 會丟掉它、
    丟掉造成孤兒還會 fail-closed）。同一規則不能在 union 執行、在 ff 放行 → 結構異常，交人。"""
    return [
        umsg("u1", None, "user", 1),
        {"type": "last-prompt", "uuid": "m1", "parentUuid": "u1", "leafUuid": "u1",
         "sessionId": "s", "lastPrompt": "p"},
        umsg("u2", "m1", "assistant", 2),      # 唯一 genuine leaf；到 u1 的唯一路徑穿過簿記行 m1
        lastprompt("u2"),
    ]


def meta_node_as_dag_link_smaller() -> list[dict]:
    """g9 反例的 smaller 側：只有 u1（＋游標指 u1）→ bigger 是它的內容超集。"""
    return [
        umsg("u1", None, "user", 1),
        lastprompt("u1"),
    ]


def uuid_x1_as_meta_line() -> list[dict]:
    """**codex g7 反例**（與 `uuid_x1_as_real_line` 成對）：uuid `x1` 在這一側是**揮發 meta 行**。
    union 在 `_emit` 之前就把 meta 行濾掉 → 跨檔同 uuid 異 hash 的衝突偵測看不到它 → 防線消失。
    須在**濾行之前**鏡射 classify 的 `uuid_hashes` 判準。"""
    return [
        umsg("u1", None, "user", 1),
        umsg("u2", "u1", "assistant", 2),
        {"type": "last-prompt", "uuid": "x1", "parentUuid": "u2", "leafUuid": "u2",
         "sessionId": "s", "lastPrompt": "p", "timestamp": ts(3)},
    ]


def uuid_x1_as_real_line() -> list[dict]:
    """同 uuid `x1` 在另一側是**真對話行**（內容/hash 完全不同）→ 歷史行被改寫 → 不可 union。"""
    return [
        umsg("u1", None, "user", 1),
        umsg("u2", "u1", "assistant", 2),
        umsg("x1", "u2", "assistant", 4, message={"role": "assistant", "content": "real"}),
        lastprompt("x1"),
    ]


def metadata_only_extension() -> list[dict]:
    """**codex g5 反例**：對話內容與 `linear_no_lastprompt` **完全相同**，只多一條**帶 uuid 的 last-prompt
    meta 行** m1（游標 u3）。若「新增 uuid 非空」就算延伸 → 變 superset → auto-ff 覆蓋對側、靜默改掉游標，
    繞過「內容同、active_tip 異 → needs-decision」的守衛。延伸須含**真內容行**才算。"""
    return [
        umsg("u1", None, "user", 1),
        umsg("u2", "u1", "assistant", 2),
        umsg("u3", "u2", "user", 3),
        {"type": "last-prompt", "uuid": "m1", "parentUuid": "u2", "leafUuid": "u3",
         "sessionId": "s", "lastPrompt": "p"},
    ]


def sidechain_only_extension() -> list[dict]:
    """合法無損延伸：只多了**子代理 sidechain 行** s1（主鏈 tip 仍是 u3、未前進）。
    sidechain 是真內容（非簿記）→ 必須**仍可 ff**，否則「延伸須含真內容」的修法會誤傷此形、
    製造新的永久噪音（正是本塊要消滅的那類 bug）。"""
    return [
        umsg("u1", None, "user", 1),
        umsg("u2", "u1", "assistant", 2),
        umsg("u3", "u2", "user", 3),
        umsg("s1", "u2", "assistant", 4, sidechain=True),
        lastprompt("u3"),
    ]


def self_parent_cycle() -> list[dict]:
    """**codex g4 反例①**：u1 的 parentUuid 指向自己（環）。唯一 genuine leaf = u2、指標 u1 在葉之前、
    且 `is_ancestor(u1, u2)` 為真（環被 `seen` 擋住不會無限迴圈）→ 四道守衛全過 → **會誤放行 auto-ff**。
    ff 的整套推理建立在 DAG 上，成環即不成立 → classify 須先拒環（session_merge 早已拒）。"""
    return [
        umsg("u1", "u1", "user", 1),      # 自我指向
        lastprompt("u1"),
        umsg("u2", "u1", "assistant", 2),
    ]


def self_parent_cycle_continued() -> list[dict]:
    return self_parent_cycle() + [
        umsg("u3", "u2", "user", 3),
        lastprompt("u3"),
    ]


def orphan_single_root_local() -> list[dict]:
    """**codex g4 反例②**：被丟棄的 meta 行 m1 正是根 u1 的父親 → union 丟掉 m1 後，u1 遞補成**唯一**的
    非-system 根 → 「根數 >1」檢查放行 → 產出 parent 指向不存在 uuid 的斷鏈檔。"""
    return [
        {"type": "last-prompt", "uuid": "m1", "leafUuid": "u1", "sessionId": "s",
         "lastPrompt": "p", "timestamp": ts(0)},
        umsg("u1", "m1", "user", 1),
        umsg("u2", "u1", "assistant", 2),
        lastprompt("u2"),
    ]


def orphan_single_root_hub() -> list[dict]:
    """與 `orphan_single_root_local` 分岔（u3 取代 u2）→ 兩側可 union，正好觸發斷鏈路徑。"""
    return [
        {"type": "last-prompt", "uuid": "m1", "leafUuid": "u1", "sessionId": "s",
         "lastPrompt": "p", "timestamp": ts(0)},
        umsg("u1", "m1", "user", 1),
        umsg("u3", "u1", "assistant", 3),
        lastprompt("u3"),
    ]


def child_under_meta_line() -> list[dict]:
    """真對話行 u7 掛在**帶 uuid 的 meta 行 m1** 底下。union 不搬 m1（契約）→ u7 失去父親、成為新的
    非-system 對話根 → 應 **fail-closed 退回人工挑選**（不可崩、不可產出斷鏈 DAG）。"""
    return [
        umsg("u1", None, "user", 1),
        umsg("u2", "u1", "assistant", 2),
        {"type": "last-prompt", "uuid": "m1", "parentUuid": "u2", "leafUuid": "u2",
         "sessionId": "s", "lastPrompt": "p", "timestamp": ts(5)},
        umsg("u7", "m1", "assistant", 7),
        lastprompt("u7"),
    ]


def cursor_line_is_the_leaf() -> list[dict]:
    """**codex g2 的 High 反例**：同一條 last-prompt 行**自己帶 uuid**（schema 未禁止）→ 它既是游標
    （leafUuid=u3）、又是唯一「葉子」候選 → `active_tip_index == leaf_first`。相等**不能**證明「寫下
    游標時葉子不存在」→ 條件 4 必須嚴格 `<`。另加 lineset 收緊：揮發 meta 行不得當 genuine leaf。"""
    return [
        umsg("u1", None, "user", 1),
        umsg("u2", "u1", "assistant", 2),
        umsg("u3", "u2", "user", 3),
        {"type": "last-prompt", "uuid": "u4", "parentUuid": "u3",     # meta 行帶 uuid（畸形但可解析）
         "leafUuid": "u3", "sessionId": "s", "lastPrompt": "p"},
    ]


def cursor_line_is_the_leaf_continued() -> list[dict]:
    return cursor_line_is_the_leaf() + [
        umsg("u5", "u4", "assistant", 5),
        lastprompt("u5"),
    ]


def mid_turn_cursor_on_tool_result() -> list[dict]:
    """**釘住實機真實形狀**（2026-07-14 a9f7f783 的 hub 快照）：`last-prompt.leafUuid` 指向的是一條
    `type=user` **且帶 `toolUseResult`** 的**工具結果行**（`is_tool_fanout` 為真），而非普通 prompt 行。
    它是真實的對話節點、就在唯一真葉的祖先鏈上 → **必須仍可 ff**。
    （codex g8 曾建議游標須「非 meta、非 sidechain、非 fan-out」；照做會把這個真實案例擋死 → 守衛 2 只
    排除簿記行，離枝風險交給守衛 3。此測試是那條界線的護欄。）"""
    return [
        umsg("u1", None, "user", 1),
        umsg("u2", "u1", "assistant", 2),
        lastprompt("tr1"),                                              # 游標指向工具結果行
        umsg("tr1", "u2", "user", 3, toolUseResult={"stdout": "x"}),    # fan-out：真節點、非葉候選
        umsg("u4", "tr1", "assistant", 4),                              # 唯一 genuine leaf（tr1 的後裔）
    ]


def mid_turn_cursor_on_tool_result_continued() -> list[dict]:
    return mid_turn_cursor_on_tool_result() + [
        umsg("u5", "u4", "user", 5),
        umsg("u6", "u5", "assistant", 6),
        lastprompt("u6"),
    ]


def rewind_marker_without_leafuuid() -> list[dict]:
    """**codex g6 的線索**：葉子 u4 出現**之後**又寫了一條 last-prompt，但該行**缺 `leafUuid`** →
    `active_tip`/`active_tip_index` 只認有 leafUuid 的行 → 對守衛 4 隱形（指標仍是 idx 3 的 u3，早於葉子）
    → 舊條件會誤放行。但「葉子已存在後游標仍被碰過」就無法再證明只是落後 → 須 fail-closed（條件 4b）。"""
    return [
        umsg("u1", None, "user", 1),
        umsg("u2", "u1", "assistant", 2),
        umsg("u3", "u2", "user", 3),
        lastprompt("u3"),
        umsg("u4", "u3", "assistant", 4),
        {"type": "last-prompt", "sessionId": "s", "lastPrompt": "p"},   # 無 leafUuid → 守衛 4 看不見
    ]


def mid_turn_snapshot_with_title() -> list[dict]:
    """mid_turn_snapshot + custom-title（驗「ff 覆蓋 hub 會丟標題」守衛未被新路徑繞過）。"""
    return mid_turn_snapshot() + [
        {"type": "custom-title", "title": "我的對話", "sessionId": "s"},
    ]


def mid_turn_two_leaves() -> list[dict]:
    """落後指標（行序正確）+ **兩個** genuine leaf（u4、u9）→ 哪一枝是活的有歧義 → 不可回退（條件 1）。"""
    return [
        umsg("u1", None, "user", 1),
        umsg("u2", "u1", "assistant", 2),
        umsg("u3", "u2", "user", 3),
        lastprompt("u3"),
        umsg("u4", "u3", "assistant", 4),
        umsg("u9", "u2", "user", 9),      # 第二枝
    ]


def mid_turn_two_leaves_continued() -> list[dict]:
    """mid_turn_two_leaves + 掛在 u9 下的 sidechain s1（使 u9 不再是 genuine leaf）
    → **bigger 恰一個 genuine leaf u4**、active_tip 指它 → bigger 側嚴格條件全過。
    此時擋下 ff 的只剩「smaller 兩葉有歧義」（條件 1）→ 可隔離驗證該條件。"""
    return mid_turn_two_leaves() + [
        umsg("s1", "u9", "assistant", 10, sidechain=True),
        lastprompt("u4"),
    ]


def mid_turn_ghost_root_parent() -> list[dict]:
    """落後指標指向**檔外**的 uuid（根 u1 的缺席父親 ghost）→ dangling，不可信（條件 2）。
    注意：`is_ancestor` 會沿 parent_map 走到「根的檔外父親」而回 True → 光靠祖先檢查擋不掉，
    必須另有「active_tip 在本檔 uuid 集內」的明檢。"""
    return [
        umsg("u1", "ghost", "user", 1),   # parent 不在檔內 → u1 仍是根
        umsg("u2", "u1", "assistant", 2),
        umsg("u3", "u2", "user", 3),
        lastprompt("ghost"),
        umsg("u4", "u3", "assistant", 4),
    ]


def mid_turn_ghost_root_parent_continued() -> list[dict]:
    return mid_turn_ghost_root_parent() + [
        umsg("u5", "u4", "user", 5),
        lastprompt("u5"),
    ]


def mid_turn_pointer_off_branch() -> list[dict]:
    """落後指標指向 sidechain s1（存在於檔內、非 genuine leaf、但**不在**唯一葉 u4 的祖先鏈上）
    → 指標不是單純落後、指到別的地方 → 不可信（條件 3）。行序合法（指標在葉之前）以隔離條件 3。"""
    return [
        umsg("u1", None, "user", 1),
        umsg("u2", "u1", "assistant", 2),
        umsg("u3", "u2", "user", 3),
        umsg("s1", "u2", "assistant", 5, sidechain=True),   # 非 genuine leaf、離枝
        lastprompt("s1"),
        umsg("u4", "u3", "assistant", 4),
    ]


def mid_turn_pointer_off_branch_continued() -> list[dict]:
    return mid_turn_pointer_off_branch() + [
        umsg("u5", "u4", "user", 6),
        lastprompt("u5"),
    ]


def mid_turn_pointer_to_meta_ancestor() -> list[dict]:
    """**codex g8 反例**：游標指向一條**帶 uuid 的揮發 meta 行 m1**，而 m1 恰好是唯一真葉 u2 的祖先
    （DAG 上），且行序早於 u2 → 守衛 1/3/4 全過，僅靠守衛 2 的「主鏈真行」判定才擋得住。
    既已宣告 meta 行不是對話節點，就不能讓它當游標把 ff 放行。"""
    return [
        umsg("u1", None, "user", 1),
        {"type": "last-prompt", "uuid": "m1", "parentUuid": "u1", "leafUuid": "m1",
         "sessionId": "s", "lastPrompt": "x"},
        umsg("u2", "m1", "assistant", 2),      # 唯一 genuine leaf；其祖先鏈含 m1
    ]


def mid_turn_pointer_to_meta_ancestor_continued() -> list[dict]:
    return mid_turn_pointer_to_meta_ancestor() + [
        umsg("u3", "u2", "user", 3),
        lastprompt("u3"),
    ]


def mid_turn_pointer_off_real_branch() -> list[dict]:
    """**守衛 3 的隔離反例**：游標指向 u9——一條**真對話行**（過得了守衛 2），但 u9 在另一條枝上、
    **不在**唯一真葉 u4 的祖先鏈上（u9 那枝的末端是 fan-out 行 tr1，故 u9 那枝沒有 genuine leaf）。
    唯有守衛 3（祖先檢查）擋得住。"""
    return [
        umsg("u1", None, "user", 1),
        umsg("u2", "u1", "assistant", 2),
        umsg("u9", "u1", "user", 3),                                   # 另一條真枝
        umsg("tr1", "u9", "user", 4, toolUseResult={"stdout": "x"}),   # 該枝末端＝fan-out（非 genuine leaf）
        lastprompt("u9"),                                              # 游標指向真行 u9（但離枝）
        umsg("u4", "u2", "assistant", 5),                              # 唯一 genuine leaf
    ]


def mid_turn_pointer_off_real_branch_continued() -> list[dict]:
    return mid_turn_pointer_off_real_branch() + [
        umsg("u5", "u4", "user", 6),
        lastprompt("u5"),
    ]


def mid_turn_pointer_to_fanout() -> list[dict]:
    """條件 3 的 **fan-out 口味**（codex R1 指出的涵蓋缺口）：指標指向工具 fan-out 行 tr1
    （type=user + toolUseResult → 非 genuine leaf），tr1 不在唯一葉 u4 的祖先鏈上 → 不可回退。
    行序合法（指標在葉之前）以隔離條件 3。"""
    return [
        umsg("u1", None, "user", 1),
        umsg("u2", "u1", "assistant", 2),
        umsg("u3", "u2", "user", 3),
        umsg("tr1", "u3", "user", 4, toolUseResult={"stdout": "x"}),   # fan-out（非真 tip）
        lastprompt("tr1"),
        umsg("u4", "u3", "assistant", 5),                              # 唯一 genuine leaf
    ]


def mid_turn_pointer_to_fanout_continued() -> list[dict]:
    return mid_turn_pointer_to_fanout() + [
        umsg("u5", "u4", "user", 6),
        lastprompt("u5"),
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
