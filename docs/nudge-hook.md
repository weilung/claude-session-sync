# `nudge`：SessionEnd / SessionStart hook 提示（DESIGN §7.5）

`claude-session-sync nudge` 是給 Claude Code hook 用的**極簡建議指令**：唯讀掃一遍，如果 memory 有待同步的
分歧就印**一行**提示，否則安靜。它**不做重活**——不寫檔、不取鎖、不讀 stdin、不碰 session 分類（`build_plan`
的 `memory_only` 快路徑），而且**任何錯誤/未設定/掛載點不在/halt 一律靜默 `exit 0`**，絕不干擾或中斷 session。

## 它會提示什麼

只看 memory（session 由例行 `sync` 處理）：

| 情況 | 提示 | 建議動作 |
|------|------|----------|
| 更新（`copy-to-hub` / `copy-to-local` / `local-deleted`） | 「N 個記憶更新待同步」 | `sync --apply` |
| 衝突（`conflict-*`） | 「N 個記憶衝突待處理」 | `memory-merge` |
| 已同步 / 已定案刪除 / 無法自動解的 blocked | （無輸出） | — |

`blocked-*` 不提示：它們工具無法自動解，靜音出口是 `doctor --ack-all`（A15），不該每次 session 結束重吵。

## 輸出格式

- **預設**：印一行 JSON `{"systemMessage": "…"}`。Claude Code 會把 `systemMessage` 對使用者顯示（SessionEnd
  的純 stdout 不顯示，但 `systemMessage` 是通用顯示欄位；SessionStart 亦適用）。JSON 用純 ASCII 的 `\uXXXX`
  跳脫（`ensure_ascii=True`）→ 不管 hook 子程序的 stdout 是什麼編碼都印得出、提示不被默默吞掉；Claude Code
  的 JSON parser 會把它還原成中文顯示。
- `--text`：改印純文字一行，供手動執行/除錯。

## 設定（`~/.claude/settings.json` 或專案 `.claude/settings.json`）

換成你環境的執行檔路徑（`claude-session-sync` 或 `python -m claude_session_sync.cli`）。

SessionEnd（設計原本的觸發點）：

```json
{
  "hooks": {
    "SessionEnd": [
      { "hooks": [ { "type": "command", "command": "claude-session-sync nudge" } ] }
    ]
  }
}
```

SessionStart（想「開新 session 時看到上次沒同步的記憶」可改掛這裡；stdout 也會進 Claude 脈絡）：

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "claude-session-sync nudge" } ] }
    ]
  }
}
```

指令是 hook 事件無關的——同一行掛在 SessionEnd / SessionStart / Stop 都可以，差別只在何時顯示。

## 為什麼安全

- 唯讀：只 `build_plan`（等同 `status` 的比對），從不寫入/取鎖/搬檔。
- fail-silent：hook 不可讓 session 結束失敗，所以任何例外都吞掉、`exit 0`、不印 traceback。
- 掛載點不在（外接/網路碟沒掛）→ 安靜跳過（G5：載體可有可無）。

## 已知有界殘留

身分解析仍需讀 session 的 `cwd` 來配對 local↔hub 專案夾（`memory_only` 只跳過較重的 session 分類段）。
要完全免 session-parse 需要一條夾名身分快路徑，留待有延遲需求時再做。
