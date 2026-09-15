"""Phase 0 spec-lock tests for ltxq frame-grid math.

Purpose: pin the CURRENT behavior of `_snap_frames` and the 8n+1 grid
invariants the audio batch composer relies on, BEFORE the model-generalization
work (Phase 1: LTX-2/WAN 8n+1 vs MiniMax H3 17n+5, parameterized grids).
When the generalized grid lands, these tests are expected to show explicit
red/green diffs — surfacing every semantic decision (floor, rounding, grid
membership) instead of silent drift.

Under test:
  ltxq._snap_frames(raw, up=True)          # ltxq.py:984-988 (current line numbers)
  the composer's on-grid rule              # `src_frames % 8 != 1` guard + <9 floor
  the pad/trim direction decision          # fit = 'pad' if frames > src else 'trim'

Run:
  ./venv/bin/python -m unittest discover -s tests -t . -v
  ./venv/bin/python -m unittest tests.test_frames -v

Import note: `import ltxq` has only no-op side effects when hosts.yaml exists
(CONF_PATH resolution stats the file; MUX_DIR.mkdir(exist_ok=True) is a no-op);
no database is created or opened at import time.
"""

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ltxq import _snap_frames, frame_grid  # noqa: E402

STEP, BASE = 8, 1      # the LTX-2 8n+1 grid
MIN_EMITTED = 9        # 8 * max(n, 1) + 1: _snap_frames never returns < 9


class SnapFramesTableTests(unittest.TestCase):
    """Exact-value table over grid boundaries (hand-computed from the
    current implementation: n = ceil/floor((raw-1)/8), f = 8*max(n,1)+1)."""

    TABLE = [
        # sub-floor inputs: everything below the first grid point snaps to 9
        (1, True, 9), (1, False, 9),
        (2, True, 9), (2, False, 9),
        (8, True, 9),
        (8, False, 9),    # QUIRK (documented): "down" on a sub-floor input
                          # returns 9 > raw. The composer masks this via its
                          # own src_frames < 9 guard before snapping.
        # exact grid members pass through unchanged
        (9, True, 9), (9, False, 9),
        (17, True, 17), (17, False, 17),
        (121, True, 121), (121, False, 121),   # 121 = 8*15 + 1
        (249, True, 249), (249, False, 249),   # 249 = 8*31 + 1
        # off-grid neighbors round away from / toward the grid
        (10, True, 17), (10, False, 9),
        (16, True, 17), (16, False, 9),
        (18, True, 25), (18, False, 17),
        (122, True, 129), (122, False, 121),
        (250, True, 257), (250, False, 249),
    ]

    def test_table(self):
        for raw, up, expected in self.TABLE:
            with self.subTest(raw=raw, up=up):
                self.assertEqual(_snap_frames(raw, up=up), expected)

    def test_degenerate_inputs_clamp_to_floor(self):
        # The max(n, 1) clamp catches even non-positive raws (floor division
        # of negatives stays <= 0). The composer never sends these (it guards
        # raw < 9 upstream), but the pure function's behavior is part of the
        # spec Phase 1 must reproduce or consciously change.
        for raw in (0, -1, -7, -100):
            with self.subTest(raw=raw):
                self.assertEqual(_snap_frames(raw, up=True), MIN_EMITTED)
                self.assertEqual(_snap_frames(raw, up=False), MIN_EMITTED)


class SnapFramesInvariants(unittest.TestCase):
    """Properties the batch composer depends on, swept across a range."""

    RANGE = range(1, 501)

    def test_output_is_always_on_grid(self):
        for raw in self.RANGE:
            for up in (True, False):
                with self.subTest(raw=raw, up=up):
                    self.assertEqual(_snap_frames(raw, up=up) % STEP, BASE)

    def test_round_up_never_shrinks(self):
        for raw in self.RANGE:
            with self.subTest(raw=raw):
                self.assertGreaterEqual(_snap_frames(raw, up=True), raw)

    def test_round_down_never_expands_at_or_above_floor(self):
        # Holds for raw >= 9. Below the floor, the documented quirk applies
        # (down returns 9 > raw) — asserted explicitly, kept visible on
        # purpose: Phase 1 must decide whether the generalized grid keeps it.
        for raw in range(MIN_EMITTED, 501):
            with self.subTest(raw=raw):
                self.assertLessEqual(_snap_frames(raw, up=False), raw)
        self.assertEqual(_snap_frames(8, up=False), MIN_EMITTED)
        self.assertEqual(_snap_frames(1, up=False), MIN_EMITTED)

    def test_minimum_emitted_is_nine(self):
        self.assertEqual(_snap_frames(1, up=True), MIN_EMITTED)
        self.assertEqual(_snap_frames(8, up=True), MIN_EMITTED)

    def test_randomized_property(self):
        rng = random.Random(42)  # fixed seed: deterministic run
        for _ in range(500):
            raw = rng.randint(1, 1000)
            up = rng.random() < 0.5
            f = _snap_frames(raw, up=up)
            self.assertEqual(f % STEP, BASE, f"off-grid output for raw={raw}")
            if up:
                self.assertGreaterEqual(f, raw)
            elif raw >= MIN_EMITTED:
                self.assertLessEqual(f, raw)


class GridMembershipRule(unittest.TestCase):
    """The composer accepts a source length iff `src_frames % 8 == 1`
    (after its own <9 floor guard), else applies the on_non_grid policy
    (round-up / round-down / refuse) via _snap_frames. Locked as the rule
    Phase 1 generalizes to (step, base) = (17, 5) for MiniMax H3."""

    def test_grid_members_below_200(self):
        on_grid = [f for f in range(1, 200) if f % 8 == 1 and f >= 9]
        self.assertEqual(
            on_grid,
            [9, 17, 25, 33, 41, 49, 57, 65, 73, 81, 89, 97, 105, 113,
             121, 129, 137, 145, 153, 161, 169, 177, 185, 193],
        )

    def test_every_snap_output_passes_the_composer_guard(self):
        # Whatever the policy, the composer's snapped target must satisfy
        # its own on-grid check — otherwise fit/trim logic would desync.
        for raw in range(MIN_EMITTED, 501):
            with self.subTest(raw=raw):
                self.assertEqual(_snap_frames(raw) % 8, 1)


class PadTrimSemantics(unittest.TestCase):
    """The composer fits each wav to the snapped length:
    fit = 'pad' if frames > src_frames else 'trim'; on-grid sources pass
    through with no fit operation at all."""

    def test_up_means_pad_or_passthrough(self):
        for raw in range(MIN_EMITTED, 200):
            f = _snap_frames(raw, up=True)
            self.assertTrue(f > raw or f == raw)  # never shorter => never trims

    def test_down_means_trim_or_passthrough(self):
        for raw in range(MIN_EMITTED, 200):
            f = _snap_frames(raw, up=False)
            self.assertTrue(f < raw or f == raw)  # never longer => never pads

    def test_on_grid_passthrough_is_identity(self):
        for f in range(MIN_EMITTED, 194, 8):
            with self.subTest(frames=f):
                self.assertEqual(_snap_frames(f, up=True), f)
                self.assertEqual(_snap_frames(f, up=False), f)


H3_GRID = (17, 5, 0)  # MiniMax H3: 17n+5 with n >= 0 (spec confirmed 2026-09-15)


class SnapFramesH3Tests(unittest.TestCase):
    """MiniMax H3 grid (17n+5, floor 5) through the generalized _snap_frames."""

    TABLE = [
        # sub-floor inputs clamp to the first grid point 5 (n=0 is valid)
        (1, True, 5), (1, False, 5),
        (4, True, 5), (4, False, 5),
        (5, True, 5), (5, False, 5),      # on-grid passthrough
        (6, True, 22), (6, False, 5),
        (21, True, 22), (21, False, 5),
        (22, True, 22), (22, False, 22),
        (23, True, 39), (23, False, 22),
        (38, True, 39), (38, False, 22),
        (39, True, 39), (39, False, 39),
        (40, True, 56), (40, False, 39),
        (56, True, 56), (56, False, 56),
        (57, True, 73), (57, False, 56),
    ]

    def test_table(self):
        for raw, up, expected in self.TABLE:
            with self.subTest(raw=raw, up=up):
                self.assertEqual(_snap_frames(raw, up=up, grid=H3_GRID), expected)

    def test_output_is_always_on_h3_grid(self):
        for raw in range(1, 301):
            for up in (True, False):
                f = _snap_frames(raw, up=up, grid=H3_GRID)
                self.assertEqual(f % 17, 5, f"off-grid output for raw={raw}")

    def test_round_up_never_shrinks(self):
        for raw in range(1, 301):
            self.assertGreaterEqual(_snap_frames(raw, up=True, grid=H3_GRID), raw)

    def test_round_down_never_expands_at_or_above_floor(self):
        for raw in range(5, 301):
            self.assertLessEqual(_snap_frames(raw, up=False, grid=H3_GRID), raw)
        # below the 5-frame floor, down returns 5 — same quirk family as the
        # LTX grid, but spec-aligned here because n=0 is a valid H3 length
        self.assertEqual(_snap_frames(4, up=False, grid=H3_GRID), 5)
        self.assertEqual(_snap_frames(1, up=False, grid=H3_GRID), 5)


class FrameGridMappingTests(unittest.TestCase):
    """frame_grid(model) → (step, base, min_n); default is LTX 8n+1."""

    def test_ltx_and_wan(self):
        for model in ("ltx-2.3-dev", "LTX-2.3", "wan2.2-t2v", "WAN-2.2"):
            with self.subTest(model=model):
                self.assertEqual(frame_grid(model), (8, 1, 1))

    def test_minimax_h3_variants(self):
        for model in ("MiniMax-H3", "minimax-h3-7b", "minimax_h3_7b",
                      "H3-video", "MiniMax H3"):
            with self.subTest(model=model):
                self.assertEqual(frame_grid(model), (17, 5, 0))

    def test_unknown_defaults_to_ltx(self):
        for model in ("mochi-1", "wanh3x", "", None):
            with self.subTest(model=model):
                self.assertEqual(frame_grid(model), (8, 1, 1))

    def test_h3_token_boundary(self):
        # 'h3' inside a larger alnum token must not match; _ and - boundaries do
        self.assertEqual(frame_grid("wanh3x"), (8, 1, 1))
        self.assertEqual(frame_grid("h3"), (17, 5, 0))


class DefaultGridEquivalence(unittest.TestCase):
    """Phase 1 must not change LTX behavior: the (8,1,1) default reproduces
    the Phase 0 spec-lock exactly."""

    def test_default_matches_explicit_ltx_grid(self):
        for raw in range(1, 501):
            for up in (True, False):
                with self.subTest(raw=raw, up=up):
                    self.assertEqual(_snap_frames(raw, up=up),
                                     _snap_frames(raw, up=up, grid=(8, 1, 1)))


if __name__ == "__main__":
    unittest.main()
