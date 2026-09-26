"""Feature-sync guard for the two dashboards.

``static/index.html`` (served at ``/``) and ``static/index-next.html``
(``/next``) are two views over the same API. ``/next`` carries a tabbed shell on
top of the same two forms; everything else must stay in lockstep, or a feature
lands in one dashboard only — which is exactly how the host ``cli_dialect``
text and the settings **dialect** column drifted apart (the MiniMax-H3 label
was already present in both; it predates the ``/next`` shell).

The check is structural: the two files must expose the same element ids apart
from ``/next``'s own shell, and carry the same set of feature markers. When a
feature is added to the UI, add its marker here — the suite then fails until
both dashboards have it.

Run:
  ./venv/bin/python -m unittest discover -s tests -t . -v
"""

import re
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"
ROOT = STATIC / "index.html"
NEXT = STATIC / "index-next.html"

# ids that belong to /next's composer shell (tabs + panels) — the only allowed
# structural difference between the two files.
NEXT_ONLY_IDS = {"panel-single", "panel-audio", "tab-single", "tab-audio",
                 "panel-pairs", "tab-pairs"}

# A string that marks a feature both dashboards must expose.
FEATURE_MARKERS = (
    "Non-grid segment (8n+1 LTX/WAN · 17n+5 H3)",   # model-aware frame grids
    "bonnongrid",                                   # ...and its control
    "cli_dialect",                                  # host dialect (card + table)
    "/api/events",                                  # SSE live updates
    "ICONS",                                        # monoline icon set
    "keyframes",                                    # keyframe rows
    '$("bhost").onchange',                          # per-form model list reload
    "pairsform",                                    # keyframe-pairs batch panel
    "/api/pairs/",                                  # ...and its staging API
    '$("phost").onchange',                          # per-form model list reload
)

ID_RE = re.compile(r'id="([^"]+)"')


def ids(html):
    return set(ID_RE.findall(html))


class FrontendSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = ROOT.read_text()
        cls.next = NEXT.read_text()

    def test_same_element_ids(self):
        r, n = ids(self.root), ids(self.next)
        self.assertEqual(r - n, set(), "ids present at / but missing at /next")
        self.assertEqual(n - r, NEXT_ONLY_IDS,
                         "unexpected extra ids at /next (new shell ids belong "
                         "in NEXT_ONLY_IDS)")

    def test_feature_markers_in_both(self):
        for m in FEATURE_MARKERS:
            self.assertIn(m, self.root, f"marker missing from index.html: {m!r}")
            self.assertIn(m, self.next, f"marker missing from index-next.html: {m!r}")

    def test_host_select_is_not_rebuilt_on_every_change(self):
        # Regression: loadModels() is the #host onchange handler and used to
        # assign .innerHTML to the host <select>s right there. That resets a
        # <select> to its first option, so picking a host snapped back to
        # "auto". The rebuild must go through the guarded syncHostOptions().
        for name, html in (("index.html", self.root),
                           ("index-next.html", self.next)):
            self.assertIn("syncHostOptions", html, name)
            self.assertNotIn('$("host").innerHTML =', html,
                             f"{name}: unguarded host <select> rebuild")


if __name__ == "__main__":
    unittest.main()
