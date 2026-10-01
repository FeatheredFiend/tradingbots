"""
Tests for shared/dashboard_reporter.py's relay: every bot on the PC hands
its reports to one relay (run by whichever bot got there first), which
passes them on to the dashboard over connections it keeps open. Everything
runs against a fake dashboard on 127.0.0.1 - nothing reaches the real one.

    python -m unittest discover -s strategy-bots/tests
"""

import contextlib
import http.server
import io
import json
import os
import socket
import sys
import threading
import time
import unittest
from unittest import mock

os.environ["DASHBOARD_URL"] = ""  # never report test runs to the real dashboard
os.environ["DASHBOARD_RELAY_PORT"] = "0"  # nor through a real bots' relay
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "shared"))

import dashboard_reporter  # noqa: E402
from dashboard_reporter import DashboardReporter  # noqa: E402


class _QuietServer(http.server.ThreadingHTTPServer):
    block_on_close = False

    def server_bind(self):
        http.server.socketserver.TCPServer.server_bind(self)  # skip the reverse DNS lookup
        self.server_name, self.server_port = self.server_address[:2]


class FakeDashboard:
    """Takes reports at /api/ingest and answers like the real one."""

    def __init__(self, keep_alive="timeout=5, max=100"):
        self.keep_alive = keep_alive
        self.status = 200
        self.hang_up_after_answering = False  # without saying so, like a server whose keep-alive ran out
        self.commands = {}                    # slug -> commands for that bot
        self.requests = []                    # (client port, token, path, report)
        dashboard = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                report = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                dashboard.requests.append(
                    (self.client_address[1], self.headers.get("X-Dashboard-Token"), self.path, report))
                slug = report["bot"]["slug"]
                data = json.dumps({"ok": True, "bot": slug, "commands": dashboard.commands.get(slug, [])}).encode()
                self.send_response(dashboard.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                if dashboard.keep_alive:
                    self.send_header("Keep-Alive", dashboard.keep_alive)
                self.end_headers()
                self.wfile.write(data)
                if dashboard.hang_up_after_answering:
                    self.close_connection = True

            def log_message(self, format, *args):
                pass

        self.server = _QuietServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def connections(self) -> int:
        return len({port for port, *_ in self.requests})

    def slugs(self):
        return [report["bot"]["slug"] for *_, report in self.requests]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.dashboard = self.fake_dashboard()
        self.port = free_port()
        for name, value in (("URL", self.dashboard.url), ("TOKEN", "test-token"), ("RELAY_PORT", self.port)):
            patcher = mock.patch.object(dashboard_reporter, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(dashboard_reporter._stop_relay)  # runs before the patches are undone

    def fake_dashboard(self, **kwargs):
        dashboard = FakeDashboard(**kwargs)
        self.addCleanup(dashboard.close)
        return dashboard

    def reporter(self, slug):
        """A bot's reporter with no thread of its own - the test sends its reports."""
        with mock.patch.object(dashboard_reporter, "URL", ""):
            reporter = DashboardReporter(slug, slug, broker="Fake", strategy="Test")
        reporter.enabled = True
        return reporter

    @staticmethod
    def report(slug):
        return {"bot": {"slug": slug}, "status": "running", "logs": []}

    def relaying_bot(self):
        return dashboard_reporter._relay_server and dashboard_reporter._relay_server.slug

    def test_every_bot_reports_through_one_kept_connection(self):
        bots = [self.reporter(slug) for slug in ("bot-a", "bot-b", "bot-c")]
        for _ in range(4):
            for bot in bots:
                self.assertEqual(bot._post(self.report(bot._bot["slug"]))["bot"], bot._bot["slug"])
        self.assertEqual(self.relaying_bot(), "bot-a", "the first bot to report runs the relay")
        self.assertEqual(len(self.dashboard.requests), 12)
        self.assertEqual(self.dashboard.connections(), 1)
        self.assertEqual({token for _, token, _, _ in self.dashboard.requests}, {"test-token"})
        self.assertEqual({path for _, _, path, _ in self.dashboard.requests}, {"/api/ingest"})

    def test_commands_come_back_to_the_bot_they_are_for(self):
        close = {"id": 3, "action": "close", "symbol": "EUR_USD", "direction": "long"}
        self.dashboard.commands = {"bot-b": [close]}
        a, b = self.reporter("bot-a"), self.reporter("bot-b")
        a._send_once()
        b._send_once()
        self.assertEqual(list(a._inbox), [])
        self.assertEqual(list(b._inbox), [close])

    def test_an_idle_connection_is_dropped_before_the_server_would(self):
        self.dashboard.keep_alive = "timeout=2, max=100"  # so it's reused for up to 1 s
        bot = self.reporter("bot-a")
        bot._post(self.report("bot-a"))
        bot._post(self.report("bot-a"))
        time.sleep(1.2)
        bot._post(self.report("bot-a"))
        self.assertEqual(self.dashboard.connections(), 2)

    def test_a_connection_the_server_hung_up_is_retried_once_on_a_new_one(self):
        self.dashboard.hang_up_after_answering = True
        bot = self.reporter("bot-a")
        self.assertIsNotNone(bot._post(self.report("bot-a")))
        time.sleep(0.2)
        self.assertIsNotNone(bot._post(self.report("bot-a")))
        self.assertEqual(len(self.dashboard.requests), 2, "each report reaches the dashboard once")
        self.assertEqual(self.dashboard.connections(), 2)

    def test_the_dashboards_errors_come_back_to_the_bot(self):
        bot = self.reporter("bot-a")
        bot._post(self.report("bot-a"))  # starts the relay
        self.dashboard.status = 500
        with mock.patch.object(bot, "_complain") as complain:
            self.assertIsNone(bot._post(self.report("bot-a")))
        self.assertRegex(complain.call_args.args[0], r"^HTTP 500: .*; passed on by bot-a$")

    def test_an_unreachable_dashboard_is_reported_as_such(self):
        bot = self.reporter("bot-a")
        bot._post(self.report("bot-a"))
        self.dashboard.close()
        dashboard_reporter._relay_server.relay.close()  # its kept connection went with the server
        with mock.patch.object(bot, "_complain") as complain:
            self.assertIsNone(bot._post(self.report("bot-a")))
        self.assertIn("passed on by bot-a", complain.call_args.args[0])
        self.assertEqual(bot._relay_off_until, 0.0, "the relay did its job; keep using it")

    def test_the_next_bot_runs_the_relay_when_its_bot_stops(self):
        a, b = self.reporter("bot-a"), self.reporter("bot-b")
        a._post(self.report("bot-a"))
        dashboard_reporter._stop_relay()  # bot-a stopped
        self.assertIsNotNone(b._post(self.report("bot-b")))
        self.assertEqual(self.relaying_bot(), "bot-b")
        self.assertIsNotNone(a._post(self.report("bot-a")))
        self.assertEqual(self.dashboard.slugs(), ["bot-a", "bot-b", "bot-a"])

    def test_only_one_relay_can_hold_the_port(self):
        self.assertTrue(dashboard_reporter._host_relay("bot-a"))
        with self.assertRaises(OSError):
            dashboard_reporter._RelayServer(("127.0.0.1", self.port), dashboard_reporter._RelayHandler)

    def test_something_else_on_the_port_means_reporting_straight_to_the_dashboard(self):
        squatter = _QuietServer(("127.0.0.1", self.port), http.server.SimpleHTTPRequestHandler)
        threading.Thread(target=squatter.serve_forever, daemon=True).start()
        self.addCleanup(squatter.server_close)
        self.addCleanup(squatter.shutdown)
        bot = self.reporter("bot-a")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertIsNotNone(bot._post(self.report("bot-a")))
            self.assertIsNotNone(bot._post(self.report("bot-a")))
        self.assertEqual(len(self.dashboard.requests), 2)
        self.assertEqual(stderr.getvalue().count("not using the reports relay"), 1)
        self.assertGreater(bot._relay_off_until, time.monotonic(), "it tries the relay again later")

    def test_a_relay_for_another_dashboard_is_not_used(self):
        self.reporter("bot-a")._post(self.report("bot-a"))  # the relay reports to self.dashboard
        other = self.fake_dashboard()
        bot = self.reporter("bot-b")
        with mock.patch.object(dashboard_reporter, "URL", other.url), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertIsNotNone(bot._post(self.report("bot-b")))
        self.assertEqual(other.slugs(), ["bot-b"])
        self.assertIn(f"reports to {self.dashboard.url}", stderr.getvalue())

    def test_a_relay_that_stops_answering_is_skipped_for_a_while(self):
        listener = socket.socket()  # takes connections, never answers
        listener.bind(("127.0.0.1", self.port))
        listener.listen(16)
        self.addCleanup(listener.close)
        bot = self.reporter("bot-a")
        with mock.patch.object(dashboard_reporter, "RELAY_WAIT_SECONDS", 0.3), \
                mock.patch.object(bot, "_complain") as complain:
            for _ in range(dashboard_reporter.RELAY_MISSES):
                self.assertIsNone(bot._post(self.report("bot-a")))
            self.assertIn("no answer from the reports relay", complain.call_args.args[0])
            self.assertIsNotNone(bot._post(self.report("bot-a")), "then straight to the dashboard")
        self.assertEqual(self.dashboard.slugs(), ["bot-a"])

    def test_relay_port_0_turns_it_off(self):
        bot = self.reporter("bot-a")
        with mock.patch.object(dashboard_reporter, "RELAY_PORT", 0):
            self.assertIsNotNone(bot._post(self.report("bot-a")))
        self.assertIsNone(dashboard_reporter._relay_server)
        self.assertEqual(self.dashboard.slugs(), ["bot-a"])


class SettingTests(unittest.TestCase):
    def test_relay_port(self):
        self.assertEqual(dashboard_reporter._relay_port(None), 47817)
        self.assertEqual(dashboard_reporter._relay_port(" 50500 "), 50500)
        self.assertEqual(dashboard_reporter._relay_port("0"), 0)
        self.assertEqual(dashboard_reporter._relay_port("off"), 0)
        self.assertEqual(dashboard_reporter._relay_port("nonsense"), 47817)

    def test_reusable_for(self):
        self.assertEqual(dashboard_reporter._reusable_for("timeout=5, max=100"), 4)
        self.assertEqual(dashboard_reporter._reusable_for("max=100,timeout=1"), 0)
        self.assertEqual(dashboard_reporter._reusable_for(None), 4)


if __name__ == "__main__":
    unittest.main()
