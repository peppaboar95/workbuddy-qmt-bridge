# Mmap Transport Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans. Track tasks below.

**Goal:** Add an optional mmap backend for the restricted QMT runtime while preserving existing file deployments and fail-closed execution.

**Architecture:** A Worker gateway bridges durable external queues/control files to authenticated shared-memory mailboxes. The QMT script reuses existing trading validation with memory-backed state and an asynchronous durable execution-grant handshake.

**Tech Stack:** Python stdlib, Windows named mmap, injected interprocess lock, SQLite.

**Spec:** ../specs/2026-11-12-mmap-transport-design.md

## Global Constraints

- QMT Python 3.6 minimum, 3.12 target; external Worker Python 3.10+.
- No QMT os/files/network/database operations; no real broker calls in verification.
- File remains the default backend; preserve existing configuration/Profile semantics.
- Durable PRE_SUBMIT precedes any grant; uncertain commands are never automatically regranted.
- Exact target synchronization interface is pending user input; keep it injectable.

## Review Focus

- Duplicate/restarted sessions cannot replay a valid old execution grant.
- Lost ACK, malformed frames and full mailboxes cannot lose durable order evidence.
- Missing control/authorization, revoked generation and expired leases block new submissions.
- Generated QMT source does not accidentally reintroduce forbidden imports through uuid or helpers.
- File-mode upgrades and signed Profile preservation continue working.

## Tasks

- [x] Transport: write failing mailbox tests; implement Python 3.6 framing with direction-bound HMAC, injected lock, backpressure and explicit ACK; run focused tests.
- [x] Journal: write failing SQLite persistence/replay/restart tests; implement durable grants and conservative recovery; run focused tests.
- [x] Deployment: test new strict config and account CLI selection first; generate standalone mmap scripts with embedded trust material; retain file mode; document deployment.
- [x] QMT runtime: test restricted imports and simulated QMT before implementation; replace only storage/transport boundaries, add asynchronous grant execution and session/lease checks.
- [x] Worker gateway: test control/command/event bridging, restart/uncertain state and durable-before-ACK; connect gateway lifecycle to RuntimeLoop.
- [x] Integration/review: run all tests, generated-source 3.6 parsing and real named-mapping process tests where available; independently review and resolve findings; report unverified target versions/interfaces.
- [ ] Bind the user's verified QMT synchronization API to MMAP_LOCK_FACTORY; exact module/signatures still requested from user. Run target Python 3.6/3.12 and broker acceptance after that integration.

## Verification evidence

2026-11-12: 178 tests passed on Windows/Python 3.13.9. Wheel built offline with `python -B -m build --wheel --no-isolation --outdir dist/mmap-check`; isolated extracted-wheel deployment generation passed, including Python 3.6 syntax parsing. No broker connected or actual order submitted.

Workspace has no Git metadata; work in place without commits/worktrees and preserve unrelated files.
