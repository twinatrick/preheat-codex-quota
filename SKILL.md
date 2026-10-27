---
name: preheat-codex-quota
description: Check and automatically preheat a ChatGPT subscription's Codex five-hour usage window using ONLY the currently active CLI interface (OpenCode or Codex). Mounts automatically on OpenCode Server (via plugin) and Codex CLI (via MCP server), schedules checks at HH:01:00 every hour and within 1 minute after quota reset (+15s/+45s), and enforces in Python code that the 5-hour quota is at 100% remaining (0% used) before any ping can be sent.
---

# Preheat Codex Quota

Use the bundled standard-library Python utility at `scripts/codex_preheater.py` and the OpenCode Server plugin at `plugins/codex-preheater.ts`.

## Architecture & Automatic Server Mounting

1. **OpenCode Server Auto-Mount (`plugins/codex-preheater.ts`)**
   - Installed at `~/.config/opencode/plugins/codex-preheater.ts`.
   - Runs automatically inside the OpenCode background server whenever OpenCode is active, without requiring manual user invocation.
   - Reads live quota for ONLY the currently active OpenCode account (`isActive == true`) via `oc-codex-multi-auth` and invokes `scripts/codex_preheater.py once --opencode-limits-json <json>`.
   - Logs schedule and check outcomes to `%LOCALAPPDATA%\codex-preheater\server.log` (or `~/.local/state/codex-preheater/server.log`).
2. **Codex CLI Auto-Mount (`scripts/codex_preheater.py mcp-server`)**
   - Configured in `~/.codex/config.toml` under `[mcp_servers.codex_preheater]`.
   - Starts automatically only when `codex` CLI is running and exits cleanly when `codex` closes.
3. **Precision Schedule (No 5-Minute Polling)**
   - **Fixed Hourly Check (`HH:01:00`)**: Runs at 1 minute past every hour (`:01:00`).
   - **Post-Reset 1-Minute Capture (`resetAt + 15s` and `+ 45s`)**: Whenever the 5-hour quota has `usedPercent > 0` and a future `resetAt` timestamp, schedules one-shot wakeups at `resetAt + 15s` and `resetAt + 45s` so the refreshed 100% window is preheated within 1 minute of reset.

## Mandatory Code-Level Guards

1. **Active CLI Isolation (`detect_active_cli`)**
   - In **OpenCode** (`OPENCODE*` env): only checks OpenCode's active account and only pings via `opencode run`. Never invokes `codex` CLI.
   - In **Codex** (`CODEX_*` env): only checks Codex via `codex app-server` and only pings via `codex exec`. Never invokes `opencode` CLI.
   - Refuses to trigger if neither CLI is active or if cross-CLI execution is requested.
2. **Mandatory 5-Hour 100% Quota Verification (`_assert_quota_eligible`)**
   - Enforced in both `check_once()` and `ping_opencode()` / `ping_codex()`:
     - `duration_minutes == 300` (5-hour subscription bucket only; Free plans and weekly/30d buckets are rejected)
     - `used_percent == 0` and `remaining_percent == 100`
     - Valid `resets_at` or `not_started == True`
3. **PATH Resolution & Cooldown Lock**
   - Resolves `opencode` and `codex` directly from system `PATH` via `shutil.which`.
   - Enforces a 280-minute anti-repeat cooldown and per-window (`observed_resets_at`) deduplication lock in `%LOCALAPPDATA%\codex-preheater\state.json`.

## Commands

```bash
# Run unit tests (18 tests):
python -m unittest -v scripts/test_preheater.py

# Manual check inside OpenCode (pass JSON from codex-limits tool):
python scripts/codex_preheater.py status --opencode-limits-file <limits.json>
python scripts/codex_preheater.py once --dry-run --opencode-limits-file <limits.json>
python scripts/codex_preheater.py once --model openai/gpt-6-luna --opencode-limits-file <limits.json>

# Manual check inside Codex CLI:
python scripts/codex_preheater.py status
python scripts/codex_preheater.py once --dry-run
python scripts/codex_preheater.py once

# Codex MCP server mode (mounted automatically by Codex CLI):
python scripts/codex_preheater.py mcp-server
```
