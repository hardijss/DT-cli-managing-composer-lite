"""Regression tests for the oneshot poll reply parser (`parse_poll`).

The engine's log can end WITHOUT a trailing newline while a render is running
(progress lines use \r). POLL_CMD used a bare `echo ---A` after the log tail, so
the marker glued itself onto the log's last line: the marker was never
recognised, `alive` defaulted to False, and `poll_job` failed a job that was
still working on the host ("process gone, no exit_code").

Run:
  ./venv/bin/python -m unittest discover -s tests -t . -v
"""

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import ltxq  # noqa: E402

# Real captured shape (trimmed): the log's final line has no trailing newline,
# so the `---A` marker ended up appended to it.
CRASH_TAIL = ("^D\x08\x08Models directory: /volumes/SSD/Models\n\n"
              "Starting... [ ] 0  %\n"
              "[Metal] Quitting now.\n\n"
              "\U0001F4A3 Program crashed: Signal 5\x1b[0m")
GLUED = "---E\n\n---P\n4338\n---L\n" + CRASH_TAIL + "---A\nalive\n"
WELL_FORMED = "---E\n1\n---P\n4338\n---L\nhello\nworld\n---A\ndead\n"


class ParsePollTests(unittest.TestCase):
    def test_glued_marker_still_yields_alive(self):
        # the marker really has no line of its own ...
        self.assertNotIn("\n---A\n", GLUED)
        # ... but the parser must still see the verdict
        exit_s, log, alive = ltxq.parse_poll(GLUED)
        self.assertTrue(alive, "a live engine was reported as gone")
        self.assertEqual(exit_s, "")
        self.assertIn("Program crashed", log)

    def test_well_formed_reply(self):
        exit_s, log, alive = ltxq.parse_poll(WELL_FORMED)
        self.assertEqual(exit_s, "1")
        self.assertEqual(log, "hello\nworld")
        self.assertFalse(alive)

    def test_dead_engine_is_not_alive(self):
        _, _, alive = ltxq.parse_poll("---E\n\n---P\n\n---L\nx---A\ndead\n")
        self.assertFalse(alive)

    def test_degenerate_replies_do_not_raise(self):
        self.assertEqual(ltxq.parse_poll(""), ("", "", False))
        self.assertTrue(ltxq.parse_poll("---A\nalive\n")[2])


if __name__ == "__main__":
    unittest.main()
