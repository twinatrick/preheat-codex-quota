#!/usr/bin/env python3
"""Codex quota preheater: inspect subscription usage and optionally ping using ONLY the active CLI.

- When invoked from OpenCode CLI / Server: checks OpenCode active account 5h quota and pings via `opencode run`.
- When invoked from Codex CLI / MCP Server: checks Codex 5h quota via `codex app-server` and pings via `codex exec`.
- When neither CLI is active: refuses to run.
- Enforces 5-hour 100% remaining (0% used) check in code before any preheat request can be sent.
- Supports `mcp-server` mode for Codex CLI: mounts when Codex starts, checks at HH:01:00 and within 1 minute after reset.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Iterator, Mapping


PING = "Reply with exactly OK. Do not use tools, read files, or perform searches."
FIVE_HOURS_MINUTES = 300
VERSION = "1.2.0"

OPENCODE_ENV_KEYS = (
    "OPENCODE",
    "OPENCODE_SESSION_ID",
    "OPENCODE_TERMINAL",
)
CODEX_ENV_KEYS = (
    "CODEX_SANDBOX",
    "CODEX_THREAD_ID",
    "CODEX_CI",
    "CODEX_MANAGED_BY_NPM",
    "CODEX_MCP_SERVER",
)


class PreheaterError(Exception):
    pass


@dataclass(frozen=True)
class Quota:
    used_percent: float
    duration_minutes: int
    resets_at: int
    limit_id: str
    cli: str = "codex"
    not_started: bool = False

    @property
    def remaining_percent(self) -> float:
        return 100.0 - self.used_percent


def seconds_until_next_hour_plus_one_minute(now_ts: float | None = None) -> float:
    """Return seconds from `now_ts` until the next local clock HH:01:00."""
    current_dt = datetime.fromtimestamp(time.time() if now_ts is None else now_ts).astimezone()
    target = current_dt.replace(minute=1, second=0, microsecond=0)
    if target <= current_dt:
        target = (current_dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).replace(minute=1)
    return max(1.0, (target - current_dt).total_seconds())


def detect_active_cli(
    env: Mapping[str, str] | None = None,
    requested_cli: str = "auto",
) -> str:
    """Detect which CLI interface is currently in use; refuse if none or mismatched."""
    environ = os.environ if env is None else env
    in_opencode = any(bool(environ.get(k)) for k in OPENCODE_ENV_KEYS)
    in_codex = any(bool(environ.get(k)) for k in CODEX_ENV_KEYS)

    detected: str | None = None
    if in_opencode and not in_codex:
        detected = "opencode"
    elif in_codex and not in_opencode:
        detected = "codex"
    elif in_opencode and in_codex:
        if requested_cli in ("opencode", "codex"):
            detected = requested_cli
        else:
            detected = "opencode"

    if detected is None:
        raise PreheaterError(
            "未偵測到當前使用中的 CLI 介面（OpenCode 或 Codex）；沒在使用該 CLI 時不觸發預熱。"
        )
    if requested_cli not in ("auto", detected):
        raise PreheaterError(
            f"強制攔截：當前使用的介面為 {detected}，禁止跨介面觸發 {requested_cli}。"
        )
    return detected


def _assert_quota_eligible(quota: Quota, *, expected_cli: str) -> None:
    """Hard code-level guard: block any ping unless 5h quota is verified at 100% remaining."""
    if (
        not isinstance(quota, Quota)
        or quota.cli != expected_cli
        or quota.duration_minutes != FIVE_HOURS_MINUTES
        or quota.used_percent != 0
        or quota.remaining_percent != 100
        or (not quota.resets_at and not quota.not_started)
    ):
        raise PreheaterError(
            "強制攔截：未通過五小時額度 100% 程式碼檢查，禁止發送預熱請求。"
        )


def local_time(timestamp: int, *, not_started: bool = False) -> str:
    if not timestamp:
        return "尚未啟動視窗（首次請求後開始計時 5 小時）" if not_started else "未知"
    return datetime.fromtimestamp(timestamp).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _rpc_read_loop(stdout: Any, responses: queue.Queue) -> None:
    try:
        for line in stdout:
            try:
                responses.put(json.loads(line))
            except json.JSONDecodeError:
                continue
    finally:
        responses.put(None)


def read_codex_limits(binary: str, timeout: int = 30) -> dict[str, Any]:
    """Use official, documented Codex app-server JSON-RPC; never inspect auth files."""
    resolved_binary = shutil.which(binary)
    if resolved_binary is None:
        raise PreheaterError(f"找不到 Codex CLI：{binary}。請先安裝並使用 ChatGPT 帳號登入。")

    try:
        proc = subprocess.Popen(
            [resolved_binary, "app-server"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1,
        )
    except OSError as exc:
        raise PreheaterError(f"無法啟動 Codex app-server：{exc}") from exc

    responses: queue.Queue = queue.Queue()
    threading.Thread(target=_rpc_read_loop, args=(proc.stdout, responses), daemon=True).start()
    deadline = time.monotonic() + timeout

    def call(method: str, request_id: int, params: dict | None = None) -> dict:
        request = {"method": method, "id": request_id}
        if params is not None:
            request["params"] = params
        send(request)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PreheaterError("Codex app-server 逾時；請確認已登入並更新 CLI。")
            try:
                message = responses.get(timeout=remaining)
            except queue.Empty as exc:
                raise PreheaterError("等待 Codex 額度資料逾時。") from exc
            if message is None:
                raise PreheaterError("Codex app-server 意外結束。")
            if not isinstance(message, dict) or message.get("id") != request_id:
                continue
            if "error" in message:
                raise PreheaterError(f"Codex RPC {method} 失敗：{message['error']}")
            return message.get("result", {})

    def send(request: dict) -> None:
        try:
            assert proc.stdin is not None
            proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except (OSError, BrokenPipeError, ValueError) as exc:
            raise PreheaterError("無法向 Codex app-server 傳送指令。") from exc

    try:
        call("initialize", 1, {"clientInfo": {
            "name": "codex_preheater", "title": "Codex Quota Preheater", "version": VERSION,
        }})
        send({"method": "initialized", "params": {}})
        account = call("account/read", 2, {"refreshToken": False}).get("account")
        if not isinstance(account, dict) or account.get("type") not in {
            "chatgpt", "chatgptAuthTokens"
        }:
            raise PreheaterError("Codex CLI 未以 ChatGPT 訂閱登入。請先執行 codex login。")
        if str(account.get("planType") or "").lower() == "free":
            raise PreheaterError("當前 Codex CLI 帳號為 Free 免費方案（無 5 小時額度視窗），不執行預熱。")
        limits = call("account/rateLimits/read", 3)
        if not isinstance(limits, dict):
            raise PreheaterError("Codex 回傳的額度格式不正確。")
        return limits
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if proc.stdin:
            proc.stdin.close()
        if proc.stdout:
            proc.stdout.close()


def find_five_hour_quota(payload: dict[str, Any]) -> Quota | None:
    """Choose only the main Codex 300-minute bucket, not weekly/other quotas."""
    buckets = payload.get("rateLimitsByLimitId")
    main = buckets.get("codex") if isinstance(buckets, dict) else None
    if not isinstance(main, dict):
        main = payload.get("rateLimits")
    if not isinstance(main, dict) or main.get("limitId") not in (None, "codex"):
        return None
    for side in ("primary", "secondary"):
        window = main.get(side)
        if not isinstance(window, dict):
            continue
        try:
            duration = int(window["windowDurationMins"])
            used = float(window["usedPercent"])
            reset = int(window.get("resetsAt") or 0)
        except (ValueError, TypeError, KeyError):
            continue
        if duration == FIVE_HOURS_MINUTES and 0 <= used <= 100:
            return Quota(
                used_percent=used,
                duration_minutes=duration,
                resets_at=reset,
                limit_id="codex",
                cli="codex",
            )
    return None


def read_opencode_limits(
    limits_json: str | None = None,
    limits_file: str | None = None,
) -> dict[str, Any]:
    """Parse OpenCode `codex-limits` JSON payload without touching credential files."""
    raw = limits_json
    if not raw and limits_file:
        try:
            raw = Path(limits_file).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            raise PreheaterError(f"無法讀取 OpenCode 額度檔案 {limits_file}：{exc}") from exc
    if not raw:
        raise PreheaterError(
            "OpenCode 介面下必須先提供當前額度資料（請由 OpenCode 呼叫 codex-limits 取得 JSON 後透過 --opencode-limits-json 傳入），未檢查額度前禁止預熱。"
        )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PreheaterError(f"OpenCode 額度 JSON 格式不正確：{exc}") from exc
    if not isinstance(data, dict):
        raise PreheaterError("OpenCode 額度資料必須為 JSON 物件。")
    return data


def find_opencode_five_hour_quota(payload: dict[str, Any]) -> Quota | None:
    """Inspect ONLY the currently active OpenCode account (`isActive == True`) for a 300-min bucket."""
    accounts = payload.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        return None
    active_index = payload.get("activeIndex")
    active_account: dict[str, Any] | None = None
    for acc in accounts:
        if isinstance(acc, dict) and acc.get("isActive") is True:
            active_account = acc
            break
    if active_account is None and isinstance(active_index, int):
        for acc in accounts:
            if isinstance(acc, dict) and acc.get("index") == active_index:
                active_account = acc
                break
    if active_account is None:
        raise PreheaterError("OpenCode 額度資料中找不到當前啟用（isActive）的帳號。")

    if str(active_account.get("planType") or "").lower() == "free":
        return None

    limits = active_account.get("limits")
    if not isinstance(limits, list):
        return None
    for item in limits:
        if not isinstance(item, dict):
            continue
        try:
            duration = int(item["windowMinutes"])
            used = float(item["usedPercent"])
        except (ValueError, TypeError, KeyError):
            continue
        if duration != FIVE_HOURS_MINUTES or not (0 <= used <= 100):
            continue
        left = item.get("leftPercent")
        if left is not None:
            try:
                if float(left) != 100.0 - used:
                    used = 100.0 - float(left)
            except (ValueError, TypeError):
                pass
        reset_ms = item.get("resetAtMs")
        resets_at = int(float(reset_ms) // 1000) if isinstance(reset_ms, (int, float)) and reset_ms > 0 else 0
        not_started = bool(item.get("notStarted"))
        return Quota(
            used_percent=used,
            duration_minutes=duration,
            resets_at=resets_at,
            limit_id="codex",
            cli="opencode",
            not_started=not_started,
        )
    return None


def default_state_file() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    return base / "codex-preheater" / "state.json"


@contextmanager
def state_lock(state_path: Path) -> Iterator[None]:
    """Cross-platform advisory lock to prevent duplicate pings from two instances."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(".lock")
    with lock_path.open("a+b") as handle:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_state(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            state = json.load(stream)
        return state if isinstance(state, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise PreheaterError(f"無法讀取狀態檔 {path}：{exc}") from exc


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".state-",
                                         suffix=".json", encoding="utf-8", delete=False) as stream:
            temp_path = Path(stream.name)
            json.dump(state, stream, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()


def print_quota(quota: Quota) -> None:
    print(f"當前介面：{quota.cli}")
    print(f"五小時額度：剩餘 {quota.remaining_percent:.2f}%（已用 {quota.used_percent:.2f}%）")
    print(f"顯示的下次刷新：{local_time(quota.resets_at, not_started=quota.not_started)}")


def ping_opencode(binary: str, model: str, timeout: int, *, quota: Quota) -> None:
    _assert_quota_eligible(quota, expected_cli="opencode")
    resolved_binary = shutil.which(binary)
    if resolved_binary is None:
        raise PreheaterError(f"找不到 OpenCode：{binary}。請先安裝並登入 ChatGPT。")
    with tempfile.TemporaryDirectory(prefix="codex-preheater-") as directory:
        env = os.environ.copy()
        env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
        env["OPENCODE_DISABLE_EXTERNAL_SKILLS"] = "1"
        try:
            result = subprocess.run(
                [resolved_binary, "run", "--model", model, "--format", "json", PING],
                cwd=directory, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise PreheaterError("OpenCode 呼叫逾時；無法確認請求是否被計費，本次不會自動重試。") from exc
        except OSError as exc:
            raise PreheaterError(f"無法執行 OpenCode：{exc}") from exc
        if result.returncode != 0:
            raise PreheaterError(f"OpenCode 返回失敗（exit code {result.returncode}）；請檢查 ChatGPT 登入與模型名稱。")
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") in ("error", "session.error"):
                raise PreheaterError("OpenCode 回傳錯誤事件；請自行確認額度，程式不會重試。")


def ping_codex(binary: str, model: str | None, timeout: int, *, quota: Quota) -> None:
    _assert_quota_eligible(quota, expected_cli="codex")
    resolved_binary = shutil.which(binary)
    if resolved_binary is None:
        raise PreheaterError(f"找不到 Codex CLI：{binary}。請先安裝並登入 ChatGPT。")
    with tempfile.TemporaryDirectory(prefix="codex-preheater-") as directory:
        cmd = [
            resolved_binary,
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--json",
            "-C",
            directory,
        ]
        if model:
            cmd.extend(["-m", model])
        cmd.append(PING)
        try:
            result = subprocess.run(
                cmd,
                cwd=directory,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise PreheaterError("Codex CLI 呼叫逾時；無法確認請求是否被計費，本次不會自動重試。") from exc
        except OSError as exc:
            raise PreheaterError(f"無法執行 Codex CLI：{exc}") from exc
        if result.returncode != 0:
            raise PreheaterError(f"Codex CLI 返回失敗（exit code {result.returncode}）；請檢查登入與模型設定。")


def _read_active_quota(args: argparse.Namespace, active_cli: str) -> Quota | None:
    if active_cli == "opencode":
        payload = read_opencode_limits(
            getattr(args, "opencode_limits_json", None),
            getattr(args, "opencode_limits_file", None),
        )
        return find_opencode_five_hour_quota(payload)
    payload = read_codex_limits(args.codex_bin, args.rpc_timeout)
    return find_five_hour_quota(payload)


def check_once(
    args: argparse.Namespace,
    *,
    now: float | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    active_cli = detect_active_cli(env, getattr(args, "cli", "auto"))
    quota = _read_active_quota(args, active_cli)
    if quota is None:
        print(f"目前在 {active_cli} 讀不到當前帳號的五小時額度（可能為 Free 方案或缺少 5 小時視窗），不發送預熱請求。")
        return "not-applicable"
    print_quota(quota)
    if quota.used_percent != 0 or quota.remaining_percent != 100:
        print("已有使用量（剩餘不等於 100%），強制攔截，不發送預熱請求。")
        return "not-full"
    if not quota.resets_at and not quota.not_started:
        print("沒有可辨識的刷新時間，為避免重複消耗，本次不發送。")
        return "unknown-window"

    state_file = Path(args.state_file).expanduser()
    with state_lock(state_file):
        current = time.time() if now is None else now
        state = load_state(state_file)
        last_attempt = float(state.get("last_attempt_at", 0) or 0)
        last_reset = int(state.get("observed_resets_at", 0) or 0)
        gap_seconds = max(0, int(args.min_gap_minutes)) * 60
        if last_attempt and current - last_attempt < gap_seconds:
            left = int((gap_seconds - (current - last_attempt)) / 60) + 1
            print(f"防重複保護：距上次嘗試不足 {args.min_gap_minutes} 分鐘，約 {left} 分鐘後才會再次允許。")
            return "cooldown"
        if quota.resets_at and last_reset == quota.resets_at and last_attempt:
            print("這個刷新視窗已經嘗試預熱過，不再重複發送。")
            return "duplicate-window"

        target_desc = f"OpenCode ({args.model})" if active_cli == "opencode" else "Codex CLI"
        print(f"符合條件：剩餘 100%。準備僅針對當前介面 {target_desc} 發送極小請求。")
        if args.dry_run:
            print("演練模式：未發出請求，未修改防重複紀錄。")
            return "dry-run"

        observed_reset = quota.resets_at or int(current) + FIVE_HOURS_MINUTES * 60
        save_state(state_file, {
            "cli": active_cli,
            "last_attempt_at": current,
            "observed_resets_at": observed_reset,
            "model": args.model if active_cli == "opencode" else (getattr(args, "codex_model", None) or "default"),
            "last_outcome": "unknown",
        })
        try:
            if active_cli == "opencode":
                ping_opencode(args.opencode_bin, args.model, args.ping_timeout, quota=quota)
            else:
                ping_codex(args.codex_bin, getattr(args, "codex_model", None), args.ping_timeout, quota=quota)
        except PreheaterError:
            save_state(state_file, {
                "cli": active_cli,
                "last_attempt_at": current,
                "observed_resets_at": observed_reset,
                "model": args.model if active_cli == "opencode" else (getattr(args, "codex_model", None) or "default"),
                "last_outcome": "uncertain",
            })
            raise
        save_state(state_file, {
            "cli": active_cli,
            "last_attempt_at": current,
            "observed_resets_at": observed_reset,
            "model": args.model if active_cli == "opencode" else (getattr(args, "codex_model", None) or "default"),
            "last_outcome": "cli-success",
        })

    print(f"{active_cli} 預熱請求完成。")
    if active_cli == "codex":
        try:
            after = find_five_hour_quota(read_codex_limits(args.codex_bin, args.rpc_timeout))
            if after:
                print_quota(after)
                if after.used_percent == 0:
                    print("仍顯示 100% 剩餘：可能是計量延遲或四捨五入；不會自動重送。")
                else:
                    print("已看到使用量增加。刷新時刻請以帳號的最新顯示為準。")
        except PreheaterError as exc:
            print(f"事後查詢額度失敗：{exc}。已禁止自動重送。")
    return "sent"


def _run_codex_background_scheduler(args: argparse.Namespace) -> None:
    """Background scheduler for Codex MCP server: checks at startup, HH:01:00, and resetAt + 15s/+45s."""
    env = dict(os.environ)
    env["CODEX_MCP_SERVER"] = "1"
    next_reset_check_ts: float | None = None
    retry_45_for_reset: int | None = None

    while True:
        now = time.time()
        try:
            quota = find_five_hour_quota(read_codex_limits(args.codex_bin, args.rpc_timeout))
            if quota is not None:
                if quota.used_percent == 0 and quota.remaining_percent == 100:
                    check_once(args, now=now, env=env)
                    next_reset_check_ts = None
                    retry_45_for_reset = None
                elif quota.resets_at > now:
                    if retry_45_for_reset == quota.resets_at:
                        next_reset_check_ts = quota.resets_at + 45.0
                        retry_45_for_reset = None
                    else:
                        next_reset_check_ts = quota.resets_at + 15.0
                        retry_45_for_reset = quota.resets_at
        except Exception:
            pass

        now = time.time()
        sleep_secs = seconds_until_next_hour_plus_one_minute(now)
        if next_reset_check_ts is not None and next_reset_check_ts > now:
            sleep_secs = min(sleep_secs, max(1.0, next_reset_check_ts - now))
        time.sleep(sleep_secs)


def run_mcp_server(args: argparse.Namespace) -> int:
    """Minimal stdio MCP server mounted by Codex CLI on startup; stops when Codex exits."""
    threading.Thread(target=_run_codex_background_scheduler, args=(args,), daemon=True).start()
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict) or "id" not in msg:
            continue
        req_id = msg["id"]
        method = msg.get("method", "")
        if method == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "codex-preheater", "version": VERSION},
            }
        elif method == "tools/list":
            result = {"tools": []}
        else:
            result = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}) + "\n")
        sys.stdout.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    parser = argparse.ArgumentParser(
        description="Codex 額度預熱器：僅針對當前使用中的 CLI（OpenCode 或 Codex），在五小時額度剩餘 100% 時發一次小請求。"
    )
    parser.add_argument(
        "command",
        choices=("status", "once", "mcp-server"),
        help="查詢當前介面額度 / 檢查並預熱一次 / 作為 Codex MCP Server 掛載",
    )
    parser.add_argument("--cli", choices=("auto", "opencode", "codex"), default="auto", help="限定當前介面（預設 auto 自動偵測）")
    parser.add_argument("--model", default="openai/gpt-6-luna", help="OpenCode 介面使用的 model ID")
    parser.add_argument("--codex-model", default=None, help="Codex CLI 介面使用的 model（選填）")
    parser.add_argument("--codex-bin", default="codex", help="Codex CLI 執行檔名稱（由 PATH 解析）")
    parser.add_argument("--opencode-bin", default="opencode", help="OpenCode CLI 執行檔名稱（由 PATH 解析）")
    parser.add_argument("--opencode-limits-json", default=None, help="OpenCode codex-limits 工具輸出的 JSON 字串")
    parser.add_argument("--opencode-limits-file", default=None, help="OpenCode codex-limits 工具輸出的 JSON 檔案路徑")
    parser.add_argument("--state-file", default=str(default_state_file()), help="防重複紀錄位置")
    parser.add_argument("--min-gap-minutes", type=int, default=280, help="兩次預熱嘗試至少間隔幾分鐘（預設 280）")
    parser.add_argument("--rpc-timeout", type=int, default=30, help="額度查詢逾時秒數")
    parser.add_argument("--ping-timeout", type=int, default=120, help="預熱請求逾時秒數")
    parser.add_argument("--dry-run", action="store_true", help="只檢查是否符合條件，不實際發送")
    args = parser.parse_args(argv)

    if args.min_gap_minutes < 0 or args.rpc_timeout < 1 or args.ping_timeout < 1:
        parser.error("min-gap 不可為負；timeout 必須大於 0。")
    if not args.model.startswith("openai/"):
        parser.error("為避免消耗錯誤服務商額度，--model 必須以 openai/ 開頭。")

    if args.command == "mcp-server":
        return run_mcp_server(args)

    try:
        active_cli = detect_active_cli(os.environ, args.cli)
        if args.command == "status":
            quota = _read_active_quota(args, active_cli)
            if quota is None:
                print(f"當前介面 ({active_cli}) 的啟用帳號沒有可辨識的 5 小時 Codex 額度視窗。")
                return 0
            print_quota(quota)
            return 0
        check_once(args)
        return 0
    except KeyboardInterrupt:
        print("\n已停止。")
        return 0
    except PreheaterError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
