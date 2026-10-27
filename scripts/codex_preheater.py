#!/usr/bin/env python3
"""Codex quota preheater: inspect subscription usage and optionally ping using ONLY the active CLI.

- When invoked from OpenCode CLI / Server: checks OpenCode active account 5h quota and pings via `opencode run`.
- When invoked from Codex CLI / MCP Server: checks Codex 5h quota via `codex app-server` and pings via `codex exec`.
- When neither CLI is active: refuses to run.
- Enforces 5-hour 100% remaining (0% used) check in code before any preheat request can be sent.
- Supports `mcp-server` mode for Codex CLI: mounts when Codex starts, checks at HH:01:00 and within 1 minute after reset,
  and strictly isolates `sys.stdout` for MCP JSON-RPC frames while writing background logs to `server.log`.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
import io
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
from typing import Any, Iterator, Mapping, TextIO
import urllib.error
import urllib.parse
import urllib.request


PING = "Reply with exactly OK. Do not use tools, read files, or perform searches."
FIVE_HOURS_MINUTES = 300
VERSION = "1.4.0"
DEFAULT_OPENCODE_MODEL = "openai/gpt-6-luna"
PREFERRED_OPENCODE_MODELS = (
    "openai/gpt-6-luna",
    "openai/gpt-5.3-codex",
    "openai/gpt-5.2-codex",
    "openai/gpt-5.1-codex",
    "openai/gpt-5-codex",
    "openai/codex-mini-latest",
)

OPENAI_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OPENAI_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
CHATGPT_WHAM_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
JWT_AUTH_CLAIM = "https://api.openai.com/auth"

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


def default_state_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    return base / "codex-preheater"


def default_state_file() -> Path:
    return default_state_dir() / "state.json"


def default_log_file() -> Path:
    return default_state_dir() / "server.log"


def log_daemon(source: str, message: str) -> None:
    try:
        log_path = default_log_file()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{ts}] [{source}] {message}\n")
    except Exception:
        pass


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


def find_five_hour_quota(payload: dict[str, Any], *, cli: str = "codex") -> Quota | None:
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
                cli=cli,
            )
    return None


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload_b64 = parts[1].replace("-", "+").replace("_", "/")
        padded = payload_b64 + "=" * ((4 - len(payload_b64) % 4) % 4)
        decoded = base64.b64decode(padded).decode("utf-8")
        obj = json.loads(decoded)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _extract_account_id_from_token(token: str) -> str | None:
    payload = _decode_jwt_payload(token)
    auth_claim = payload.get(JWT_AUTH_CLAIM)
    if isinstance(auth_claim, dict):
        cid = auth_claim.get("chatgpt_account_id")
        if isinstance(cid, str) and cid.strip():
            return cid.strip()
    for key in ("chatgpt_account_id", "account_id", "accountId"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _refresh_openai_oauth_token(refresh_token: str, timeout: int = 20) -> dict[str, Any] | None:
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": OPENAI_OAUTH_CLIENT_ID,
    }).encode("utf-8")
    req = urllib.request.Request(
        OPENAI_OAUTH_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if not isinstance(data, dict):
                return None
            access = data.get("access_token")
            if not isinstance(access, str) or not access.strip():
                return None
            new_refresh = data.get("refresh_token")
            expires_in = data.get("expires_in")
            expires_at_ms: int | None = None
            if isinstance(expires_in, (int, float)) and expires_in > 0:
                expires_at_ms = int(time.time() * 1000 + float(expires_in) * 1000)
            return {
                "access_token": access.strip(),
                "refresh_token": (
                    new_refresh.strip()
                    if isinstance(new_refresh, str) and new_refresh.strip()
                    else refresh_token
                ),
                "expires_at_ms": expires_at_ms,
            }
    except Exception:
        return None


def _persist_refreshed_oauth_credential(
    cred: Mapping[str, Any],
    refreshed: Mapping[str, Any],
) -> None:
    """Atomically persist rotated access/refresh tokens back to the credential file."""
    source_path_raw = cred.get("source_path")
    source_kind = cred.get("source_kind")
    if not isinstance(source_path_raw, str) or not source_path_raw:
        return
    source_path = Path(source_path_raw)
    if not source_path.is_file():
        return

    new_access = refreshed.get("access_token")
    new_refresh = refreshed.get("refresh_token")
    new_expires = refreshed.get("expires_at_ms")
    if not isinstance(new_access, str) or not new_access:
        return

    try:
        data = json.loads(source_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        updated = False
        if source_kind == "multi_auth":
            accounts = data.get("accounts")
            idx = cred.get("account_index")
            if isinstance(accounts, list) and isinstance(idx, int) and 0 <= idx < len(accounts):
                acc = accounts[idx]
                if isinstance(acc, dict):
                    if "accessToken" in acc or "access" not in acc:
                        acc["accessToken"] = new_access
                    else:
                        acc["access"] = new_access
                    if isinstance(new_refresh, str) and new_refresh:
                        if "refreshToken" in acc or "refresh" not in acc:
                            acc["refreshToken"] = new_refresh
                        else:
                            acc["refresh"] = new_refresh
                    if isinstance(new_expires, int) and new_expires > 0:
                        if "expiresAt" in acc or "expires" not in acc:
                            acc["expiresAt"] = new_expires
                        else:
                            acc["expires"] = new_expires
                    updated = True
        elif source_kind == "native_auth":
            openai_entry = data.get("openai")
            if isinstance(openai_entry, dict) and openai_entry.get("type") == "oauth":
                if "access" in openai_entry or "accessToken" not in openai_entry:
                    openai_entry["access"] = new_access
                else:
                    openai_entry["accessToken"] = new_access
                if isinstance(new_refresh, str) and new_refresh:
                    if "refresh" in openai_entry or "refreshToken" not in openai_entry:
                        openai_entry["refresh"] = new_refresh
                    else:
                        openai_entry["refreshToken"] = new_refresh
                if isinstance(new_expires, int) and new_expires > 0:
                    if "expires" in openai_entry or "expiresAt" not in openai_entry:
                        openai_entry["expires"] = new_expires
                    else:
                        openai_entry["expiresAt"] = new_expires
                updated = True

        if updated:
            temp_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    "w",
                    dir=source_path.parent,
                    prefix=f".{source_path.name}.",
                    suffix=".tmp",
                    encoding="utf-8",
                    delete=False,
                ) as stream:
                    temp_path = Path(stream.name)
                    json.dump(data, stream, ensure_ascii=False, indent=2)
                os.replace(temp_path, source_path)
            finally:
                if temp_path and temp_path.exists():
                    temp_path.unlink()
    except Exception:
        pass


def _map_wham_window(window: Any, now_sec: float | None = None) -> dict[str, Any] | None:
    if not isinstance(window, dict):
        return None
    length_sec = window.get("limit_window_seconds")
    used = window.get("used_percent")
    if not isinstance(length_sec, (int, float)) or length_sec <= 0:
        return None
    if not isinstance(used, (int, float)):
        return None
    window_minutes = max(1, int((float(length_sec) + 59) // 60))
    now_val = time.time() if now_sec is None else now_sec
    reset_after = window.get("reset_after_seconds")
    reset_at = window.get("reset_at")
    remaining_sec: float | None = None
    if isinstance(reset_after, (int, float)):
        remaining_sec = float(reset_after)
    elif isinstance(reset_at, (int, float)):
        remaining_sec = float(reset_at) - now_val

    not_started = bool(
        float(used) == 0.0
        and remaining_sec is not None
        and remaining_sec >= float(length_sec) - 1.0
    )
    reset_at_ms: int | None = None
    if not not_started:
        if isinstance(reset_at, (int, float)) and reset_at > 0:
            reset_at_ms = int(float(reset_at) * 1000)
        elif isinstance(reset_after, (int, float)) and reset_after > 0:
            reset_at_ms = int((now_val + float(reset_after)) * 1000)

    return {
        "name": "5h limit" if window_minutes == 300 else f"{window_minutes}m limit",
        "windowMinutes": window_minutes,
        "usedPercent": float(used),
        "leftPercent": max(0.0, min(100.0, round(100.0 - float(used)))),
        "resetAtMs": reset_at_ms,
        "notStarted": not_started,
    }


def _fetch_wham_usage_as_opencode_payload(
    access_token: str,
    refresh_token: str | None = None,
    expires_at_ms: int | float | None = None,
    account_id: str | None = None,
    organization_id: str | None = None,
    timeout: int = 20,
    credential_meta: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    token = access_token
    current_refresh = refresh_token
    now_ms = time.time() * 1000

    def _apply_refresh(ref_tok: str) -> str | None:
        nonlocal current_refresh
        refreshed = _refresh_openai_oauth_token(ref_tok, timeout=timeout)
        if isinstance(refreshed, str) and refreshed.strip():
            refreshed = {"access_token": refreshed.strip(), "refresh_token": ref_tok, "expires_at_ms": None}
        if not isinstance(refreshed, dict):
            return None
        new_access = refreshed.get("access_token")
        if not isinstance(new_access, str) or not new_access.strip():
            return None
        new_ref = refreshed.get("refresh_token")
        if isinstance(new_ref, str) and new_ref.strip():
            current_refresh = new_ref.strip()
        if credential_meta is not None:
            _persist_refreshed_oauth_credential(credential_meta, refreshed)
        return new_access.strip()

    if (not token or (isinstance(expires_at_ms, (int, float)) and 0 < expires_at_ms <= now_ms + 30_000)) and current_refresh:
        refreshed_access = _apply_refresh(current_refresh)
        if refreshed_access:
            token = refreshed_access

    if not token:
        return None

    resolved_account_id = account_id or _extract_account_id_from_token(token)

    def _do_request(bearer: str) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/json",
            "OpenAI-Beta": "responses=experimental",
            "originator": "codex_cli_rs",
            "User-Agent": "codex_cli_rs/0.1.0",
        }
        if resolved_account_id:
            headers["chatgpt-account-id"] = resolved_account_id
        if organization_id:
            headers["openai-organization"] = organization_id
        req = urllib.request.Request(CHATGPT_WHAM_USAGE_URL, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        raw_usage = _do_request(token)
    except urllib.error.HTTPError as exc:
        if exc.code == 401 and current_refresh:
            refreshed_access = _apply_refresh(current_refresh)
            if not refreshed_access:
                return None
            resolved_account_id = account_id or _extract_account_id_from_token(refreshed_access)
            try:
                raw_usage = _do_request(refreshed_access)
            except Exception:
                return None
        else:
            return None
    except Exception:
        return None

    if not isinstance(raw_usage, dict):
        return None

    rate_limit = raw_usage.get("rate_limit")
    limits: list[dict[str, Any]] = []
    if isinstance(rate_limit, dict):
        for key in ("primary_window", "secondary_window"):
            mapped = _map_wham_window(rate_limit.get(key))
            if mapped is not None:
                limits.append(mapped)

    return {
        "totalAccounts": 1,
        "activeIndex": 1,
        "accounts": [
            {
                "index": 1,
                "isActive": True,
                "planType": raw_usage.get("plan_type"),
                "limits": limits,
            }
        ],
    }


def _discover_opencode_oauth_credentials() -> list[dict[str, Any]]:
    """Discover active OpenCode OpenAI OAuth credentials across native auth.json and multi-auth files."""
    home = Path.home()
    candidates: list[dict[str, Any]] = []

    # 1. Multi-auth account files (if user uses oc-codex-multi-auth)
    multi_auth_paths = [
        home / ".opencode" / "oc-codex-multi-auth-accounts.json",
        home / ".opencode" / "openai-codex-accounts.json",
        home / ".config" / "opencode" / "oc-codex-multi-auth-accounts.json",
    ]
    for path in multi_auth_paths:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        accounts = data.get("accounts")
        if not isinstance(accounts, list) or not accounts:
            continue
        by_family = data.get("activeIndexByFamily")
        raw_active = (
            by_family.get("codex")
            if isinstance(by_family, dict) and isinstance(by_family.get("codex"), int)
            else data.get("activeIndex", 0)
        )
        active_idx = raw_active if isinstance(raw_active, int) and 0 <= raw_active < len(accounts) else 0
        acc = accounts[active_idx]
        if isinstance(acc, dict) and acc.get("enabled") is not False:
            candidates.append({
                "access_token": str(acc.get("accessToken") or acc.get("access") or ""),
                "refresh_token": str(acc.get("refreshToken") or acc.get("refresh") or ""),
                "expires_at_ms": acc.get("expiresAt") or acc.get("expires"),
                "account_id": acc.get("accountId"),
                "organization_id": acc.get("organizationId"),
                "source_path": str(path),
                "source_kind": "multi_auth",
                "account_index": active_idx,
            })
            break

    # 2. Native OpenCode auth.json (`~/.local/share/opencode/auth.json` or XDG_DATA_HOME)
    xdg_data = os.environ.get("XDG_DATA_HOME")
    native_auth_paths = [
        Path(xdg_data) / "opencode" / "auth.json" if xdg_data else None,
        home / ".local" / "share" / "opencode" / "auth.json",
        Path(os.environ.get("LOCALAPPDATA", str(home))) / "opencode" / "auth.json",
    ]
    for path in native_auth_paths:
        if path is None or not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        openai_entry = data.get("openai")
        if isinstance(openai_entry, dict) and openai_entry.get("type") == "oauth":
            candidates.append({
                "access_token": str(openai_entry.get("access") or openai_entry.get("accessToken") or ""),
                "refresh_token": str(openai_entry.get("refresh") or openai_entry.get("refreshToken") or ""),
                "expires_at_ms": openai_entry.get("expires") or openai_entry.get("expiresAt"),
                "account_id": openai_entry.get("accountId"),
                "organization_id": openai_entry.get("organizationId"),
                "source_path": str(path),
                "source_kind": "native_auth",
            })
            break

    return candidates


def read_opencode_limits(
    limits_json: str | None = None,
    limits_file: str | None = None,
    timeout: int = 20,
) -> dict[str, Any]:
    """Read OpenCode active account limits from JSON/file or auto-discover from OpenCode OAuth."""
    raw = limits_json
    if not raw and limits_file:
        try:
            raw = Path(limits_file).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            raise PreheaterError(f"無法讀取 OpenCode 額度檔案 {limits_file}：{exc}") from exc
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PreheaterError(f"OpenCode 額度 JSON 格式不正確：{exc}") from exc
        if not isinstance(data, dict):
            raise PreheaterError("OpenCode 額度資料必須為 JSON 物件。")
        return data

    for cred in _discover_opencode_oauth_credentials():
        payload = _fetch_wham_usage_as_opencode_payload(
            access_token=cred["access_token"],
            refresh_token=cred.get("refresh_token"),
            expires_at_ms=cred.get("expires_at_ms"),
            account_id=cred.get("account_id"),
            organization_id=cred.get("organization_id"),
            timeout=timeout,
            credential_meta=cred,
        )
        if payload is not None:
            return payload

    raise PreheaterError(
        "OpenCode 介面下無法讀取當前額度資料（未找到 OpenCode OpenAI OAuth 憑證或額度 JSON），未檢查額度前禁止預熱。"
    )


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
                left_val = float(left)
            except (ValueError, TypeError) as exc:
                raise PreheaterError("OpenCode 額度資料中的 leftPercent 格式不正確，強制中止預熱。") from exc
            if not (0.0 <= left_val <= 100.0):
                raise PreheaterError("OpenCode 額度資料中的 leftPercent 超出有效範圍，強制中止預熱。")
            if (
                (used == 0.0 and left_val != 100.0)
                or (left_val == 100.0 and used != 0.0)
                or abs((100.0 - used) - left_val) > 0.51
            ):
                raise PreheaterError(
                    f"OpenCode 額度資料矛盾（usedPercent={used}, leftPercent={left_val}），強制中止預熱。"
                )
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


def print_quota(quota: Quota, *, out: TextIO | None = None) -> None:
    target = sys.stdout if out is None else out
    print(f"當前介面：{quota.cli}", file=target)
    print(f"五小時額度：剩餘 {quota.remaining_percent:.2f}%（已用 {quota.used_percent:.2f}%）", file=target)
    print(f"顯示的下次刷新：{local_time(quota.resets_at, not_started=quota.not_started)}", file=target)


def resolve_opencode_model(
    binary: str,
    requested_model: str = DEFAULT_OPENCODE_MODEL,
    timeout: int = 15,
) -> str:
    """Resolve an available openai/* model from `opencode models`, falling back safely."""
    if requested_model != "auto" and not requested_model.startswith("openai/"):
        raise PreheaterError("為避免消耗錯誤服務商額度，--model 必須為 auto 或以 openai/ 開頭。")

    fallback = DEFAULT_OPENCODE_MODEL if requested_model == "auto" else requested_model
    resolved_binary = shutil.which(binary)
    if resolved_binary is None:
        return fallback

    try:
        result = subprocess.run(
            [resolved_binary, "models"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            return fallback
        available = [
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip().startswith("openai/")
        ]
        if not available:
            return fallback
        if requested_model != "auto" and requested_model in available:
            return requested_model
        for preferred in PREFERRED_OPENCODE_MODELS:
            if preferred in available:
                return preferred
        return available[0]
    except Exception:
        return fallback


def ping_opencode(binary: str, model: str, timeout: int, *, quota: Quota) -> None:
    _assert_quota_eligible(quota, expected_cli="opencode")
    resolved_binary = shutil.which(binary)
    if resolved_binary is None:
        raise PreheaterError(f"找不到 OpenCode：{binary}。請先安裝並登入 ChatGPT。")
    effective_model = resolve_opencode_model(binary, model, timeout=min(15, max(5, timeout)))
    with tempfile.TemporaryDirectory(prefix="codex-preheater-") as directory:
        env = os.environ.copy()
        env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
        env["OPENCODE_DISABLE_EXTERNAL_SKILLS"] = "1"
        try:
            result = subprocess.run(
                [resolved_binary, "run", "--model", effective_model, "--format", "json", PING],
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
        limits_json = getattr(args, "opencode_limits_json", None)
        limits_file = getattr(args, "opencode_limits_file", None)
        payload = read_opencode_limits(
            limits_json,
            limits_file,
            timeout=getattr(args, "rpc_timeout", 20),
        )
        return find_opencode_five_hour_quota(payload)
    payload = read_codex_limits(args.codex_bin, args.rpc_timeout)
    return find_five_hour_quota(payload, cli="codex")


def check_once(
    args: argparse.Namespace,
    *,
    now: float | None = None,
    env: Mapping[str, str] | None = None,
    out: TextIO | None = None,
) -> str:
    target_out = sys.stdout if out is None else out
    active_cli = detect_active_cli(env, getattr(args, "cli", "auto"))
    quota = _read_active_quota(args, active_cli)
    if quota is None:
        print(
            f"目前在 {active_cli} 讀不到當前帳號的五小時額度（可能為 Free 方案或缺少 5 小時視窗），不發送預熱請求。",
            file=target_out,
        )
        return "not-applicable"
    print_quota(quota, out=target_out)
    if quota.used_percent != 0 or quota.remaining_percent != 100:
        print("已有使用量（剩餘不等於 100%），強制攔截，不發送預熱請求。", file=target_out)
        return "not-full"
    if not quota.resets_at and not quota.not_started:
        print("沒有可辨識的刷新時間，為避免重複消耗，本次不發送。", file=target_out)
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
            print(
                f"防重複保護：距上次嘗試不足 {args.min_gap_minutes} 分鐘，約 {left} 分鐘後才會再次允許。",
                file=target_out,
            )
            return "cooldown"
        if quota.resets_at and last_reset == quota.resets_at and last_attempt:
            print("這個刷新視窗已經嘗試預熱過，不再重複發送。", file=target_out)
            return "duplicate-window"

        target_desc = f"OpenCode ({args.model})" if active_cli == "opencode" else "Codex CLI"
        print(f"符合條件：剩餘 100%。準備僅針對當前介面 {target_desc} 發送極小請求。", file=target_out)
        if args.dry_run:
            print("演練模式：未發出請求，未修改防重複紀錄。", file=target_out)
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

    print(f"{active_cli} 預熱請求完成。", file=target_out)
    try:
        after = _read_active_quota(args, active_cli)
        if after:
            print_quota(after, out=target_out)
            if after.resets_at and after.resets_at != observed_reset:
                with state_lock(state_file):
                    latest_state = load_state(state_file)
                    if latest_state:
                        latest_state["observed_resets_at"] = after.resets_at
                        save_state(state_file, latest_state)
            if after.used_percent == 0:
                print("仍顯示 100% 剩餘：可能是計量延遲或四捨五入；不會自動重送。", file=target_out)
            else:
                print("已看到使用量增加。刷新時刻請以帳號的最新顯示為準。", file=target_out)
    except PreheaterError as exc:
        print(f"事後查詢額度失敗：{exc}。已禁止自動重送。", file=target_out)
    return "sent"


def build_reset_check_schedule(resets_at: int, now_ts: float) -> list[float]:
    """Return remaining post-reset check timestamps (+15s and +45s) strictly in the future."""
    if not resets_at or resets_at <= 0:
        return []
    return [ts for ts in (float(resets_at) + 15.0, float(resets_at) + 45.0) if ts > now_ts]


def _run_codex_background_scheduler(args: argparse.Namespace) -> None:
    """Background scheduler for Codex MCP server: never writes to stdout; logs to server.log."""
    env = dict(os.environ)
    env["CODEX_MCP_SERVER"] = "1"
    scheduled_reset_at: int | None = None
    pending_reset_checks: list[float] = []

    log_daemon("codex-mcp", "Codex MCP 預熱守衛已掛載於背景。")

    while True:
        now = time.time()
        try:
            quota = find_five_hour_quota(read_codex_limits(args.codex_bin, args.rpc_timeout))
            if quota is not None:
                if quota.used_percent == 0 and quota.remaining_percent == 100:
                    buf = io.StringIO()
                    outcome = check_once(args, now=now, env=env, out=buf)
                    summary = " | ".join(line.strip() for line in buf.getvalue().splitlines() if line.strip())
                    log_daemon("codex-mcp", f"檢查結果 ({outcome}): {summary}")
                    scheduled_reset_at = None
                    pending_reset_checks.clear()
                elif quota.resets_at > now:
                    if scheduled_reset_at != quota.resets_at:
                        scheduled_reset_at = quota.resets_at
                        pending_reset_checks = build_reset_check_schedule(quota.resets_at, now)
                        log_daemon(
                            "codex-mcp",
                            f"額度未滿（已用 {quota.used_percent:.0f}%），已排程刷新後 +15s 與 +45s 自動檢查。",
                        )
        except Exception as exc:
            log_daemon("codex-mcp", f"背景檢查異常：{exc}")

        now = time.time()
        pending_reset_checks = [ts for ts in pending_reset_checks if ts > now]
        sleep_secs = seconds_until_next_hour_plus_one_minute(now)
        if pending_reset_checks:
            sleep_secs = min(sleep_secs, max(1.0, pending_reset_checks[0] - now))
        time.sleep(sleep_secs)


def run_mcp_server(
    args: argparse.Namespace,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    start_scheduler: bool = True,
) -> int:
    """Minimal stdio MCP server mounted by Codex CLI on startup; isolates stdout strictly for JSON-RPC."""
    mcp_stdin = sys.stdin if stdin is None else stdin
    mcp_stdout = sys.stdout if stdout is None else stdout

    # Redirect global sys.stdout to sys.stderr when running over real stdio so no stray print() corrupts MCP frames
    if stdout is None:
        sys.stdout = sys.stderr

    if start_scheduler:
        threading.Thread(target=_run_codex_background_scheduler, args=(args,), daemon=True).start()

    for raw_line in mcp_stdin:
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
        mcp_stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}) + "\n")
        mcp_stdout.flush()
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
    parser.add_argument(
        "--model",
        default=DEFAULT_OPENCODE_MODEL,
        help="OpenCode 介面使用的 model ID（可設為 auto 自動挑選 openai/* 模型）",
    )
    parser.add_argument("--codex-model", default=None, help="Codex CLI 介面使用的 model（選填）")
    parser.add_argument("--codex-bin", default="codex", help="Codex CLI 執行檔名稱（由 PATH 解析）")
    parser.add_argument("--opencode-bin", default="opencode", help="OpenCode CLI 執行檔名稱（由 PATH 解析）")
    parser.add_argument("--opencode-limits-json", default=None, help="OpenCode codex-limits 工具輸出的 JSON 字串（選填，預設自動探索）")
    parser.add_argument("--opencode-limits-file", default=None, help="OpenCode codex-limits 工具輸出的 JSON 檔案路徑（選填）")
    parser.add_argument("--state-file", default=str(default_state_file()), help="防重複紀錄位置")
    parser.add_argument("--min-gap-minutes", type=int, default=280, help="兩次預熱嘗試至少間隔幾分鐘（預設 280）")
    parser.add_argument("--rpc-timeout", type=int, default=30, help="額度查詢逾時秒數")
    parser.add_argument("--ping-timeout", type=int, default=120, help="預熱請求逾時秒數")
    parser.add_argument("--dry-run", action="store_true", help="只檢查是否符合條件，不實際發送")
    args = parser.parse_args(argv)

    if args.min_gap_minutes < 0 or args.rpc_timeout < 1 or args.ping_timeout < 1:
        parser.error("min-gap 不可為負；timeout 必須大於 0。")
    if args.model != "auto" and not args.model.startswith("openai/"):
        parser.error("為避免消耗錯誤服務商額度，--model 必須為 auto 或以 openai/ 開頭。")

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
