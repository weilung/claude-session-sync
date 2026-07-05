# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — 2026-07-05

First public release. _(0.1.0 and 0.1.1 could not be published — their
filenames were already reserved on PyPI; 0.2.0 is the first release on PyPI.)_

### Added
- Offline cross-machine sync for Claude Code sessions (JSONL) and memory (.md)
  over an external / network drive — no forced cloud or git.
- Same-group two-way sync via a shared hub; cross-group explicit, selectable
  `pull` / `push` of specific sessions.
- Safe writes throughout: read-verify-write + file lock + tombstone + keep-both.
  Never silently loses data (mechanical work to Python, semantic calls to the human).
- Commands: `bootstrap`, `status`, `sync` (`--apply` / `--interactive`),
  `pull` / `push`, `remote`, `doctor` (`--rebuild-state` / `--break-lock` /
  `--ack-all` / `--unack-all` / `--show-acked`), `memory-merge`
  (incl. cross-group `--from` and advisory `--fuzzy` / `--stage` / `--interactive`),
  and a read-only SessionEnd `nudge` hook.
- Memory union + tombstone + `MEMORY.md` index rebuild; AI-assisted memory merge
  that always preserves both versions and never auto-merges.
- Cross-platform: Linux + Windows CI on Python 3.11 / 3.13; zero third-party
  dependencies (standard library only).

[0.2.0]: https://github.com/weilung/claude-session-sync/releases/tag/v0.2.0
