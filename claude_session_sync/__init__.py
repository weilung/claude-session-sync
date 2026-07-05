"""claude-session-sync — 跨機同步 Claude Code 的 session(JSONL) 與 memory(.md)。

P1a 唯讀核心（已實作）：
  - canonical：編碼吸收 + canonical hash + 行解析（三態：ok / zero-byte / blank / decode-error）
  - lineset：行身分、root-set、genuine leaf、active-tip
  - classify：§4.1 分類表 + 安全閘 + main-root 相容性 + active-tip 交叉驗

依據 DESIGN.md v0.4 附錄 B（P0 spike 定案）與 PLAN-P1.md v0.3。
"""

__version__ = "0.2.0"
