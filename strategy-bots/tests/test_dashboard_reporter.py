"""
Tests for shared/dashboard_reporter.py's side of closing positions from the
dashboard: commands arriving in a report's reply, being carried out once
on the bot's thread, and the answers going back. Nothing is sent anywhere -
the HTTP post is replaced.

    python -m unittest discover -s strategy-bots/tests
"""

import itertools
import os
import sys
import unittest
from unittest import mock

os.environ["DASHBOARD_URL"] = ""  # never report test runs to the real dashboard
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "shared"))

import dashboard_reporter  # noqa: E402
from dashboard_reporter import CommandError, DashboardReporter  # noqa: E402

CLOSE = {"id": 7, "action": "close", "symbol": "EUR_USD", "ref": 1234, "direction": "long", "size": None}


class CommandTests(unittest.TestCase):
    def reporter(self, commands_on=True, replies=()):
        """A reporter whose posts are recorded, answered with `replies` in turn."""
        with mock.patch.object(dashboard_reporter, "URL", ""):
            reporter = DashboardReporter("test-bot", "Test bot", broker="Fake", strategy="Test")
        reporter.enabled = True  # as if DASHBOARD_URL were set, but with no thread and no real posts
        self.sent = []
        replies = list(replies)

        def post(report):
            self.sent.append(report)
            return replies.pop(0) if replies else {"ok": True, "commands": []}
        reporter._post = post
        patcher = mock.patch.object(dashboard_reporter, "COMMANDS_ON", commands_on)
        patcher.start()
        self.addCleanup(patcher.stop)
        return reporter

    def test_a_close_is_carried_out_once_and_answered(self):
        calls = []
        reporter = self.reporter(replies=[{"ok": True, "commands": [CLOSE]}, {"ok": True, "commands": [CLOSE]}])
        reporter.accept_closes(lambda *args: calls.append(args) or "Closed EUR_USD.")
        reporter._send_once()
        self.assertEqual(self.sent[0]["bot"]["acceptsCommands"], ["close"])
        self.assertEqual(calls, [], "nothing runs on the reporting thread")

        self.assertEqual(reporter.run_commands(), 1)
        self.assertEqual(calls, [("EUR_USD", "1234", "long", None)])
        self.assertFalse(reporter._wake.is_set(), "the answer waits for the fresh positions")
        reporter.update(positions=[])
        self.assertTrue(reporter._wake.is_set(), "...and then goes straight out")

        reporter._send_once()  # the dashboard (wrongly) sends the same command again
        self.assertEqual(self.sent[1]["commandResults"], [{"id": 7, "ok": True, "message": "Closed EUR_USD."}])
        self.assertEqual(self.sent[1]["positions"], [])
        self.assertEqual(reporter.run_commands(), 0, "a command never runs twice")
        reporter._send_once()
        self.assertNotIn("commandResults", self.sent[2], "an answer is sent once it's been taken")

    def test_refusals_and_errors_are_answered_not_raised(self):
        def close(symbol, ref, direction, size):
            if size:
                raise CommandError("Capital.com's API only closes whole positions.")
            raise KeyError("dealId")
        reporter = self.reporter(replies=[{"commands": [CLOSE, dict(CLOSE, id=8, size=0.5), dict(CLOSE, id=9, action="open")]}])
        reporter.accept_closes(close)
        reporter._send_once()
        self.assertEqual(reporter.run_commands(), 3)
        answers = {a["id"]: a for a in reporter._answers}
        self.assertEqual(answers[7], {"id": 7, "ok": False, "message": "KeyError: 'dealId'"})
        self.assertEqual(answers[8]["message"], "Capital.com's API only closes whole positions.")
        self.assertIn("doesn't know the command 'open'", answers[9]["message"])

    def test_off_unless_dashboard_commands_is_set(self):
        calls = []
        reporter = self.reporter(commands_on=False, replies=[{"commands": [CLOSE]}])
        reporter.accept_closes(lambda *args: calls.append(args))
        reporter._send_once()
        self.assertEqual(self.sent[0]["bot"]["acceptsCommands"], [])
        reporter.run_commands()
        self.assertEqual(calls, [])
        self.assertIn("switched off", reporter._answers[0]["message"])

    def test_an_answer_that_fails_to_send_is_sent_again(self):
        reporter = self.reporter(replies=[{"commands": [CLOSE]}, None])
        reporter.accept_closes(lambda *args: "Closed.")
        reporter._send_once()
        reporter.run_commands()
        reporter._send_once()  # the dashboard is down
        reporter._send_once()
        self.assertEqual(self.sent[2]["commandResults"][0]["id"], 7)

    def test_a_bot_shutting_down_takes_no_more_commands(self):
        calls = []
        reporter = self.reporter(replies=[{"commands": [CLOSE]}])
        reporter.accept_closes(lambda *args: calls.append(args))
        reporter._status = "stopped"
        reporter._send_once()
        reporter.run_commands()
        self.assertEqual(calls, [])

    def test_sleep_runs_commands_as_they_arrive(self):
        calls = []
        reporter = self.reporter(replies=[{"commands": [CLOSE]}])
        reporter.accept_closes(lambda *args: calls.append(args) or "Closed.")
        reporter._send_once()
        with mock.patch("time.sleep"), mock.patch("time.monotonic", side_effect=itertools.count(0, 0.4)):
            reporter.sleep(1.0, report=lambda: None)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
