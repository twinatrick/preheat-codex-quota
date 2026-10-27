import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import codex_preheater as m


def codex_payload(used=0, reset=2000000000, duration=300):
    return {"rateLimitsByLimitId": {"codex": {
        "limitId": "codex",
        "primary": {"usedPercent": used, "windowDurationMins": duration, "resetsAt": reset},
        "secondary": {"usedPercent": 0, "windowDurationMins": 10080, "resetsAt": 2000000100},
    }}}


def opencode_payload(used=0, reset_ms=2000000000000, duration=300, plan="plus", not_started=False):
    return {
        "totalAccounts": 2,
        "activeIndex": 1,
        "accounts": [
            {
                "index": 1,
                "isActive": True,
                "planType": plan,
                "limits": [
                    {
                        "name": "5h limit",
                        "windowMinutes": duration,
                        "usedPercent": used,
                        "leftPercent": 100 - used,
                        "resetAtMs": reset_ms,
                        "notStarted": not_started,
                    },
                    {
                        "name": "Weekly limit",
                        "windowMinutes": 10080,
                        "usedPercent": 30,
                        "leftPercent": 70,
                        "resetAtMs": 2000100000000,
                        "notStarted": False,
                    },
                ],
            },
            {
                "index": 2,
                "isActive": False,
                "planType": "plus",
                "limits": [
                    {
                        "name": "5h limit",
                        "windowMinutes": 300,
                        "usedPercent": 0,
                        "leftPercent": 100,
                        "resetAtMs": 2000000000000,
                        "notStarted": True,
                    }
                ],
            },
        ],
    }


class ActiveCliDetectionTests(unittest.TestCase):
    def test_detects_opencode_only_when_opencode_env_present(self):
        self.assertEqual(m.detect_active_cli({"OPENCODE": "1"}), "opencode")

    def test_detects_codex_only_when_codex_env_present(self):
        self.assertEqual(m.detect_active_cli({"CODEX_SANDBOX": "read-only"}), "codex")

    def test_refuses_when_no_cli_is_active(self):
        with self.assertRaises(m.PreheaterError):
            m.detect_active_cli({})

    def test_refuses_cross_cli_invocation(self):
        with self.assertRaises(m.PreheaterError):
            m.detect_active_cli({"OPENCODE": "1"}, requested_cli="codex")
        with self.assertRaises(m.PreheaterError):
            m.detect_active_cli({"CODEX_SANDBOX": "1"}, requested_cli="opencode")

    def test_seconds_until_next_hour_plus_one_minute(self):
        from datetime import datetime
        dt_before = datetime(2026, 10, 27, 13, 0, 30).astimezone()
        self.assertAlmostEqual(m.seconds_until_next_hour_plus_one_minute(dt_before.timestamp()), 30.0, places=1)
        dt_after = datetime(2026, 10, 27, 13, 1, 0).astimezone()
        self.assertAlmostEqual(m.seconds_until_next_hour_plus_one_minute(dt_after.timestamp()), 3600.0, places=1)
        dt_mid = datetime(2026, 10, 27, 13, 21, 0).astimezone()
        self.assertAlmostEqual(m.seconds_until_next_hour_plus_one_minute(dt_mid.timestamp()), 2400.0, places=1)


class QuotaTests(unittest.TestCase):
    def test_remaining_100_means_used_zero(self):
        q = m.find_five_hour_quota(codex_payload(0))
        self.assertEqual(q.remaining_percent, 100)
        self.assertEqual(q.cli, "codex")

    def test_already_99_percent(self):
        q = m.find_five_hour_quota(codex_payload(1))
        self.assertEqual(q.remaining_percent, 99)

    def test_weekly_only_never_triggers(self):
        self.assertIsNone(m.find_five_hour_quota(codex_payload(duration=10080)))

    def test_opencode_active_account_only(self):
        q = m.find_opencode_five_hour_quota(opencode_payload(used=100))
        self.assertIsNotNone(q)
        self.assertEqual(q.used_percent, 100)
        self.assertEqual(q.remaining_percent, 0)
        self.assertEqual(q.cli, "opencode")

    def test_opencode_free_plan_ignored(self):
        self.assertIsNone(m.find_opencode_five_hour_quota(opencode_payload(plan="free")))

    def test_hard_code_guard_blocks_non_100_or_wrong_cli(self):
        bad_used = m.Quota(used_percent=1, duration_minutes=300, resets_at=2000000000, limit_id="codex", cli="opencode")
        with self.assertRaises(m.PreheaterError):
            m.ping_opencode("opencode", "openai/gpt-6-luna", 5, quota=bad_used)
        wrong_cli = m.Quota(used_percent=0, duration_minutes=300, resets_at=2000000000, limit_id="codex", cli="codex")
        with self.assertRaises(m.PreheaterError):
            m.ping_opencode("opencode", "openai/gpt-6-luna", 5, quota=wrong_cli)
        with self.assertRaises(m.PreheaterError):
            m.ping_codex("codex", None, 5, quota=bad_used)


class TriggerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.opencode_env = {"OPENCODE": "1"}
        self.codex_env = {"CODEX_SANDBOX": "read-only"}

    def make_args(self, **overrides):
        base = dict(
            cli="auto",
            codex_bin="codex",
            opencode_bin="opencode",
            model="openai/gpt-6-luna",
            codex_model=None,
            opencode_limits_json=json.dumps(opencode_payload(0)),
            opencode_limits_file=None,
            rpc_timeout=5,
            ping_timeout=5,
            min_gap_minutes=280,
            state_file=str(self.path),
            dry_run=False,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_opencode_mode_only_triggers_opencode_and_cooldown(self):
        args = self.make_args()
        with patch.object(m, "ping_opencode") as ping_oc, \
             patch.object(m, "ping_codex") as ping_cx, \
             patch.object(m, "read_codex_limits") as read_cx:
            self.assertEqual(m.check_once(args, now=1000000, env=self.opencode_env), "sent")
            self.assertEqual(m.check_once(args, now=1000060, env=self.opencode_env), "cooldown")
            ping_oc.assert_called_once()
            ping_cx.assert_not_called()
            read_cx.assert_not_called()
        self.assertEqual(m.load_state(self.path)["last_outcome"], "cli-success")
        self.assertEqual(m.load_state(self.path)["cli"], "opencode")

    def test_codex_mode_only_triggers_codex(self):
        args = self.make_args(opencode_limits_json=None)
        with patch.object(m, "read_codex_limits", return_value=codex_payload(0)), \
             patch.object(m, "ping_codex") as ping_cx, \
             patch.object(m, "ping_opencode") as ping_oc:
            self.assertEqual(m.check_once(args, now=1000000, env=self.codex_env), "sent")
            ping_cx.assert_called_once()
            ping_oc.assert_not_called()
        self.assertEqual(m.load_state(self.path)["cli"], "codex")

    def test_no_send_when_usage_above_zero_in_opencode(self):
        args = self.make_args(opencode_limits_json=json.dumps(opencode_payload(used=100)))
        with patch.object(m, "ping_opencode") as ping_oc:
            self.assertEqual(m.check_once(args, now=1000000, env=self.opencode_env), "not-full")
            ping_oc.assert_not_called()

    def test_dry_run_does_not_record_or_send(self):
        args = self.make_args(dry_run=True)
        with patch.object(m, "ping_opencode") as ping_oc:
            self.assertEqual(m.check_once(args, now=1000000, env=self.opencode_env), "dry-run")
            ping_oc.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_failed_call_is_recorded_and_not_retried(self):
        args = self.make_args()
        with patch.object(m, "ping_opencode", side_effect=m.PreheaterError("timeout")) as ping_oc:
            with self.assertRaises(m.PreheaterError):
                m.check_once(args, now=1000000, env=self.opencode_env)
            self.assertEqual(m.check_once(args, now=1000060, env=self.opencode_env), "cooldown")
            ping_oc.assert_called_once()
        self.assertEqual(m.load_state(self.path)["last_outcome"], "uncertain")

    def test_same_window_does_not_retry_after_min_gap(self):
        args = self.make_args()
        with patch.object(m, "ping_opencode") as ping_oc:
            self.assertEqual(m.check_once(args, now=1000000, env=self.opencode_env), "sent")
            self.assertEqual(m.check_once(args, now=1020000, env=self.opencode_env), "duplicate-window")
            ping_oc.assert_called_once()

    def test_unknown_reset_time_does_not_send(self):
        args = self.make_args(opencode_limits_json=json.dumps(opencode_payload(used=0, reset_ms=None, not_started=False)))
        with patch.object(m, "ping_opencode") as ping_oc:
            self.assertEqual(m.check_once(args, now=1000000, env=self.opencode_env), "unknown-window")
            ping_oc.assert_not_called()

    def test_opencode_auto_fallback_to_codex_limits_when_no_oauth_file(self):
        args = self.make_args(opencode_limits_json=None)
        with patch.object(m, "_discover_opencode_oauth_credentials", return_value=[]), \
             patch.object(m, "read_codex_limits", return_value=codex_payload(0)) as read_cx, \
             patch.object(m, "ping_opencode") as ping_oc:
            self.assertEqual(m.check_once(args, now=1000000, env=self.opencode_env), "sent")
            read_cx.assert_called_once()
            ping_oc.assert_called_once()

    def test_build_reset_check_schedule_returns_15s_and_45s(self):
        schedule = m.build_reset_check_schedule(1000, 990)
        self.assertEqual(schedule, [1015.0, 1045.0])
        schedule_after_15 = m.build_reset_check_schedule(1000, 1020)
        self.assertEqual(schedule_after_15, [1045.0])
        self.assertEqual(m.build_reset_check_schedule(1000, 1050), [])

    def test_mcp_server_isolates_stdout_for_jsonrpc_only(self):
        import io
        args = self.make_args()
        fake_in = io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n"
        )
        fake_out = io.StringIO()
        rc = m.run_mcp_server(args, stdin=fake_in, stdout=fake_out, start_scheduler=False)
        self.assertEqual(rc, 0)
        lines = [line for line in fake_out.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)
        msg1 = json.loads(lines[0])
        msg2 = json.loads(lines[1])
        self.assertEqual(msg1["id"], 1)
        self.assertEqual(msg1["result"]["serverInfo"]["name"], "codex-preheater")
        self.assertEqual(msg2["id"], 2)
        self.assertEqual(msg2["result"]["tools"], [])

    def test_map_wham_window_converts_5h_window(self):
        mapped = m._map_wham_window(
            {"limit_window_seconds": 18000, "used_percent": 0, "reset_at": 2000000000},
            now_sec=1999990000,
        )
        self.assertIsNotNone(mapped)
        self.assertEqual(mapped["windowMinutes"], 300)
        self.assertEqual(mapped["usedPercent"], 0.0)
        self.assertEqual(mapped["leftPercent"], 100)
        self.assertEqual(mapped["resetAtMs"], 2000000000000)


if __name__ == "__main__":
    unittest.main()
