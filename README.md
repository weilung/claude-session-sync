# claude-session-sync

讓 Claude Code 的**對話 session（JSONL）與 memory（.md）**透過**外接 / 網路硬碟**在多台機器間離線同步的 CLI 工具。

- 同群組多台共用一個 hub 目錄**雙向同步**；跨群組**明確、可挑選**地引入/送回特定 session。
- 核心原則：**機械的事交給 Python，語意的事交給 AI；永不靜默丟資料。**
- 與現成工具的差異：離線（不強制上雲/git）＋可挑選＋AI 輔助 memory 合併＋多寫入者安全。

## 現況

**核心功能已實作並收斂**（跨模型 code review + 逐塊 fresh-gate；跨平台，Windows 綠）。

- 已完成：唯讀掃描/分類 → `sync` 雙向同步（安全寫入：read-verify-write + lock + tombstone + keep-both）、
  跨群 `pull`/`push`、`bootstrap` 基線、`doctor` 診斷/rebuild-state/break-lock/ack、
  memory union + tombstone + `MEMORY.md` 索引重建、`memory-merge`（含跨群 `--from` 與模糊近似 `--fuzzy`）、SessionEnd `nudge` hook。
- memory「同事實不同檔名」的**模糊近似比對**（P2 最後一項、最高風險）已完成——刻意只做**唯讀建議**（`memory-merge --fuzzy` 列候選、由你逐對放行才保留兩版，**絕不自動合併**）。
- 尚未釋出為 1.0；仍在自用/收斂階段。

## 快速開始

```bash
# 1) 設定自己群組的 hub（編輯設定檔；Windows 路徑用單引號）
#    Windows: %APPDATA%\claude-session-sync\config.toml
#    POSIX:   ~/.config/claude-session-sync/config.toml
#    own_hub = 'D:\SyncDrive\HomeJSONL'

# 2) 第一次先建基線
claude-session-sync bootstrap --map "本機專案夾=hub專案夾" --yes

# 3) 日常同步（先預覽，再 --apply 落地）
claude-session-sync status          # 看差異（純唯讀）
claude-session-sync sync            # 預覽
claude-session-sync sync --apply    # 落地
```

（開發環境未安裝時，等價寫法為 `python -m claude_session_sync.cli <子指令>`。）

## 文件

- **[`docs/`](docs/README.md) — 使用者指南（白話版）**：不需要懂程式，講清楚「做什麼、怎麼安全、怎麼用」。
  - [概念與運作](docs/01-概念與運作.md) ｜ [安全機制白話](docs/02-安全機制白話.md) ｜ [指令手冊](docs/03-指令手冊.md)
  - [情境劇本](docs/04-情境劇本.md) ｜ [Windows 與已知限制](docs/05-windows-與已知限制.md) ｜ [名詞對照](docs/06-名詞對照.md)

## 開發

```bash
python -m unittest discover -t . -s tests
```

純 Python、零第三方相依（標準庫）。跨 OS CI 於 Ubuntu + Windows 跑 py3.11/3.13。
