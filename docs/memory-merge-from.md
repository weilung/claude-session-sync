# `memory-merge --from <remote>`：跨群 memory 衝突

`memory-merge`（不帶 `--from`）偵測**本機 ↔ 自己群組 hub** 的 memory 衝突；`--from <remote>` 是**跨群**版：偵測
**本機 memory ↔ 另一個群組的 remote hub memory** 的衝突，保留兩版到本機快取供你手動合併。與 `pull --from` 對稱
（那是搬 session；這是比對 memory 衝突）。

```
claude-session-sync remote add office /media/will/OfficeDrive/OfficeJSONL   # 先登記 remote（若尚未）
claude-session-sync memory-merge --from office --map 本機夾名=office夾名     # 預覽衝突
claude-session-sync memory-merge --from office --map 本機夾名=office夾名 --apply   # 保留兩版到本機快取
```

## 偵測什麼

**stateless**（無 per-remote 基線，與 `pull`/`push` 一致）——衝突偵測不需基線：

| 衝突 | 條件 |
|------|------|
| `conflict-content` | 同檔名，本機與 remote 兩側**都在**且正規化內容不同 |
| `conflict-delete-vs-update` | remote 有此 memory 的刪除標記（tombstone），但本機還留著且已改過（A3：跨群不復活已刪） |

**不偵測**（此版限制，留 P2）：

- **跨檔改名同一事實**（`conflict-cross-file-identity`，如 `old.md`→`new.md` 同 `name`）——需要基線語意，stateless
  跨群下單邊檔一律 `blocked-no-baseline`（非衝突），故不升級成跨檔衝突。
- **本機已刪、remote 還在**——本機的刪除標記在自己 hub、不在 remote，故此方向不視為 delete-vs-update。

## 配對（`--map`）

跨群靠**夾名配對**：`--map 本機夾名=remote夾名`（可重複）。工具目前不寫 `_project.json` sidecar，所以 git 指紋
多半判不出 → 需要 `--map`（同 `pull`/`push`）。沒配對到的本機專案會提示你補 `--map`。

## 安全

- **只讀**正式 memory，**絕不寫回** `memory/`（A3）。
- 保留兩版到 `$XDG_CACHE_HOME/claude-session-sync/merge/`（memory/ 與**兩側 hub** 之外）→ 不會被當新 memory
  同步擴散。暫存根若落在本機/remote/自己 hub **之內** → fail-closed 拒絕（不外洩）。
- ⚠ **明文外洩警告**：memory 是明文。把兩版貼進 Claude 對話合併，會讓該 prompt 進 session JSONL → 下次 `sync`
  擴散到 hub。故只保留到本機快取 / 印到 stdout，**絕不自動餵 Claude**；合併前請自行刪減敏感段。
