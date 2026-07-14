# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `--map` now **asserts** a folder binding: `bootstrap --map` / `doctor --rebuild-state --map`
  bind a project folder by name even when its sessions record several different `cwd`
  values (previously refused as an ambiguous multi-`cwd` folder). Auto re-binding revokes
  the assertion; a state file predating this field fails closed.
- Restore mode: pointing `--map` at a local folder that does not exist yet, while the
  project exists in the hub, creates the folder plus an empty local baseline — so a fresh
  machine restores with `bootstrap --map` followed by `sync --apply`.

### Fixed
- **A conversation that was backed up mid-turn and then continued is now fast-forwarded
  instead of being reported as a fork forever.** When a session file is captured after the
  assistant has replied but before the next prompt is submitted, its last `last-prompt`
  cursor points at the message that was just answered — which by then has a child, so it is
  no longer a leaf. The classifier treated that as "cannot determine the tip" and refused to
  fast-forward, so a purely linear extension was reported as `superset-branch` on every run
  and had to be resolved by hand. Cursor staleness is now proven from append-only file order
  (a cursor written before the leaf existed cannot express intent about it), so only a
  deliberate rewind still blocks the fast-forward. The strict side of the check — the branch
  that gets adopted and may overwrite the other side — is unchanged.
- Auto fast-forward now fails closed on structurally broken transcripts: a `parentUuid`
  cycle (including self-parenting), a conversation line parented to a bookkeeping row, and
  an "extension" that adds nothing but bookkeeping rows.
- `session_merge` no longer carries uuid-bearing volatile-metadata rows into a union (its
  own contract said it did not) and can no longer select one as the merged tip; conflicting
  same-uuid rows are detected before those rows are filtered out.
- A lock that cannot be acquired because the volume is gone, read-only, or permission-denied
  (plain `OSError`, not `LockError`) is now reported per file instead of aborting the whole
  run with a traceback — relevant because a hub is often a removable drive.
- Cycle detection is now O(n) rather than O(n²) (390 ms → 2.5 ms on a 2,026-node transcript).
- The interactive decider no longer tracebacks when stdin is closed (e.g. the process was
  backgrounded); it skips safely.

### Docs
- Documented that `union` writes a **new** file and never overwrites the two original
  diverged files — so the fork keeps being reported afterwards (by design, not a failure),
  and re-running `union` only produces another duplicate merge file.

## [0.1.0] — 2026-07-05

First public release.

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

[0.1.0]: https://github.com/weilung/claude-session-sync/releases/tag/v0.1.0
