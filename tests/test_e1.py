"""Tests for the Audio batch E1 composer (ltxq add-batch-e1, Idea 8).

Pins the sidecar role vocabulary (.i/.iN/.f/.m/.l/.kNN), the per-dialect
compiler (args + capability sets + tiers), the continuity rules (explicit
first-frame wins; continuity+ordered refuses), the planner validation paths
(auto vs pinned host) and the queueing tail (assets, keyframe tokens, chain).

Run:
  ./venv/bin/python -m unittest discover -s tests -t . -v
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import ltxq  # noqa: E402

CFG = {"width": 128, "height": 128, "fps": 25, "numFrames": 121}
HOSTS_BOTH = ("cli_path: ~/tod-dt-cli\ncli_dialect: dtcustom\nhosts:\n"
              "  - alias: dt-main\n"
              "  - alias: dt-community\n    cli_dialect: dtofficial\n"
              "    cli_path: ~/draw-things-cli\n")


class E1Base(unittest.TestCase):
    def setUp(self):
        self._scratch = tempfile.TemporaryDirectory(prefix="ltxq_e1_")
        self.tmp = Path(self._scratch.name)
        self._saved = (ltxq.DB_PATH, ltxq.HERE, ltxq.CONF_PATH, ltxq._DB_INITED,
                       ltxq._wav_duration)
        ltxq.DB_PATH = self.tmp / "ltxq.db"
        ltxq.HERE = self.tmp
        ltxq.CONF_PATH = self.tmp / "hosts.yaml"
        ltxq._DB_INITED = False
        ltxq._wav_duration = lambda p: 4.84          # 121 frames @ 25 fps
        (self.tmp / "jobs" / "_tmp").mkdir(parents=True)
        (self.tmp / "hosts.yaml").write_text(HOSTS_BOTH)
        self.c = ltxq.conf()
        self.con = ltxq.db()
        ltxq.sync_hosts(self.con)
        self.cfg = self.tmp / "cfg.json"
        self.cfg.write_text(json.dumps(CFG))

    def tearDown(self):
        self.con.close()
        (ltxq.DB_PATH, ltxq.HERE, ltxq.CONF_PATH, ltxq._DB_INITED,
         ltxq._wav_duration) = self._saved
        self._scratch.cleanup()

    # -- fixtures -------------------------------------------------------------
    def segdir(self, name="segs", wavs=(1, 2), images=None, prompts=True):
        d = self.tmp / name
        d.mkdir(exist_ok=True)
        for n in wavs:
            (d / f"f-{n:04d}.wav").write_bytes(b"RIFF")
            if prompts:
                (d / f"f-{n:04d}.txt").write_text(f"prompt {n}")
        if images:
            if isinstance(images, (list, tuple, set)):
                images = {n: b"x" for n in images}
            for fn, blob in images.items():
                (d / fn).write_bytes(blob)
        return d

    def ns(self, d, **over):
        opts = dict(dir=str(d), model="ltx-2-dev", host=None, ext="mov",
                    backend=None, seed=None, batch=None,
                    config_file=str(self.cfg), config_json=None,
                    prompt_file=None, manifest=None,
                    on_non_grid="round-up", chain=False, dry_run=True)
        opts.update(over)
        return Namespace(**opts)

    def plan(self, d, **over):
        return ltxq.e1_plan(str(d), self.ns(d, **over), self.c, self.con)

    def seg(self, attachments, name="f-0001", d=None):
        return {"name": name, "dir": str(d or self.tmp),
                "attachments": attachments}


class SuffixTests(E1Base):
    def test_vocabulary_and_legacy(self):
        d = self.segdir(images=["f-0001.png", "f-0001.i2.png", "f-0001.f.png",
                                "f-0001.m.png", "f-0001.l.png",
                                "f-0001.k12.png", "f-0001.x.png"])
        atts, problems = ltxq._e1_attachments("f-0001", d)
        got = sorted((a["file"], a["target"], a.get("order"), a.get("index"))
                     for a in atts)
        self.assertIn(("f-0001.png", "ordered", 1, None), got)
        self.assertIn(("f-0001.i2.png", "ordered", 2, None), got)
        self.assertIn(("f-0001.f.png", "first", None, None), got)
        self.assertIn(("f-0001.m.png", "middle", None, None), got)
        self.assertIn(("f-0001.l.png", "last", None, None), got)
        self.assertIn(("f-0001.k12.png", "keyframe", None, 12), got)
        self.assertEqual(len(atts), 6)
        self.assertTrue(any("f-0001.x.png" in p for p in problems))

    def test_layout_orders_and_duplicates(self):
        d = self.segdir(images=["f-0001.i2.png", "f-0001.i.png",
                                "f-0001.l.png", "f-0001.f.png"])
        atts, _ = ltxq._e1_attachments("f-0001", d)
        problems = []
        layout = ltxq._e1_layout(atts, problems)
        self.assertEqual([a["file"] for a in layout],
                         ["f-0001.i.png", "f-0001.i2.png",
                          "f-0001.f.png", "f-0001.l.png"])
        self.assertEqual(problems, [])
        dup = [{"file": "a.png", "target": "ordered", "order": 1},
               {"file": "b.png", "target": "ordered", "order": 1},
               {"file": "c.png", "target": "first"},
               {"file": "d.png", "target": "first"}]
        problems = []
        ltxq._e1_layout(dup, problems)
        self.assertEqual(len(problems), 2)

    def test_lowercase_only(self):
        self.assertEqual(ltxq._e1_target("i"), ("ordered", 1))
        self.assertEqual(ltxq._e1_target("i2"), ("ordered", 2))
        self.assertEqual(ltxq._e1_target("L"), ("last", None))
        self.assertIsNone(ltxq._e1_target("k"))       # keyframe needs an index
        self.assertIsNone(ltxq._e1_target("f2"))


class CompileTests(E1Base):
    def test_dtcustom_full_slots(self):
        d = self.segdir(images=["f-0001.i.png", "f-0001.f.png",
                                "f-0001.l.png", "f-0001.m.png",
                                "f-0001.k12.png"])
        seg = self.seg([{"file": "f-0001.i.png", "target": "ordered",
                         "order": 1},
                        {"file": "f-0001.f.png", "target": "first"},
                        {"file": "f-0001.m.png", "target": "middle"},
                        {"file": "f-0001.l.png", "target": "last"},
                        {"file": "f-0001.k12.png", "target": "keyframe",
                         "index": 12}], d=d)
        v = ltxq._e1_compile(seg, "dtcustom")
        self.assertEqual(v["errors"], [])
        self.assertEqual([f for f, _ in v["assets"]],
                         ["--image", "--first-frame", "--middle-frame",
                          "--last-frame", None])
        self.assertEqual(v["extra"], ["--keyframe", "@f-0001.k12.png@:12"])
        self.assertEqual(v["caps"],
                         {"image", "first_frame", "middle_frame", "last_frame",
                          "keyframe", "fflf_preflight"})
        self.assertTrue(all(t["tier"] == "exact" for t in v["tiers"]))

    def test_dtofficial_ordered_exact(self):
        d = self.segdir(images=["f-0001.f.png", "f-0001.i.png",
                                "f-0001.i2.png"])
        seg = self.seg([{"file": "f-0001.f.png", "target": "first"},
                        {"file": "f-0001.i.png", "target": "ordered",
                         "order": 1},
                        {"file": "f-0001.i2.png", "target": "ordered",
                         "order": 2}], d=d)
        v = ltxq._e1_compile(seg, "dtofficial")
        self.assertEqual(v["errors"], [])
        # the first-frame target becomes image #1, ordered images follow
        self.assertEqual([Path(p).name for _, p in v["assets"]],
                         ["f-0001.f.png", "f-0001.i.png", "f-0001.i2.png"])
        self.assertTrue(all(t["tier"] == "exact" for t in v["tiers"]))
        self.assertEqual(v["caps"], {"image", "multi_image"})

    def test_dtofficial_named_roles_ride_ordered_refs(self):
        d = self.segdir(images=["f-0001.i.png", "f-0001.m.png",
                                "f-0001.l.png", "f-0001.k12.png"])
        seg = self.seg([{"file": "f-0001.i.png", "target": "ordered",
                         "order": 1},
                        {"file": "f-0001.m.png", "target": "middle"},
                        {"file": "f-0001.l.png", "target": "last"},
                        {"file": "f-0001.k12.png", "target": "keyframe",
                         "index": 12}], d=d)
        v = ltxq._e1_compile(seg, "dtofficial")
        self.assertEqual(v["errors"], [])
        self.assertEqual([Path(p).name for _, p in v["assets"]],
                         ["f-0001.i.png", "f-0001.m.png", "f-0001.l.png",
                          "f-0001.k12.png"])
        tiers = {t["file"]: t["tier"] for t in v["tiers"]}
        self.assertEqual(tiers["f-0001.i.png"], "exact")
        for f in ("f-0001.m.png", "f-0001.l.png", "f-0001.k12.png"):
            self.assertEqual(tiers[f], "steered")
        self.assertIn("multi_image", v["caps"])

    def test_dtofficial_lone_reference_refused(self):
        # a lone last/middle/keyframe has no anchor: a single --image would be
        # read as the start, so v1 refuses instead of sending it.
        d = self.segdir(images=["f-0001.l.png"])
        seg = self.seg([{"file": "f-0001.l.png", "target": "last"}], d=d)
        v = ltxq._e1_compile(seg, "dtofficial")
        self.assertEqual(len(v["errors"]), 1)
        self.assertIn("needs an anchor", v["errors"][0])
        self.assertEqual(v["tiers"][0]["tier"], "unsupported")
        self.assertEqual(v["assets"], [])

    def test_first_alone_is_image_on_both(self):
        d = self.segdir(images=["f-0001.f.png"])
        seg = self.seg([{"file": "f-0001.f.png", "target": "first"}], d=d)
        dt = ltxq._e1_compile(seg, "dtcustom")
        do = ltxq._e1_compile(seg, "dtofficial")
        self.assertEqual([f for f, _ in dt["assets"]], ["--image"])
        self.assertEqual([f for f, _ in do["assets"]], ["--image"])
        self.assertEqual(dt["caps"], {"image"})
        self.assertEqual(do["caps"], {"image"})

    def test_dtcustom_multi_ordered_errors(self):
        d = self.segdir(images=["f-0001.i.png", "f-0001.i2.png"])
        seg = self.seg([{"file": "f-0001.i.png", "target": "ordered",
                         "order": 1},
                        {"file": "f-0001.i2.png", "target": "ordered",
                         "order": 2}], d=d)
        v = ltxq._e1_compile(seg, "dtcustom")
        self.assertTrue(any("at most one --image" in e for e in v["errors"]))
        v2 = ltxq._e1_compile(seg, "dtofficial")
        self.assertEqual(v2["errors"], [])
        self.assertEqual(v2["caps"], {"image", "multi_image"})


class PlannerTests(E1Base):
    def test_plan_ok_and_compile_views(self):
        d = self.segdir(images=["f-0001.i.png", "f-0002.f.png",
                                "f-0002.l.png"])
        plan = self.plan(d)
        self.assertEqual(plan["errors"], [])
        self.assertEqual(len(plan["segments"]), 2)
        self.assertEqual(plan["fps"], 25)
        self.assertEqual(plan["grid"], [8, 1, 1])
        for seg in plan["segments"]:
            self.assertIn("dtcustom", seg["compile"])
            self.assertIn("dtofficial", seg["compile"])
            self.assertIn(seg["queued"], ("dtcustom", "dtofficial"))

    def test_chain_ff_wins_note(self):
        d = self.segdir(images=["f-0002.f.png"])
        plan = self.plan(d, chain=True)
        self.assertEqual(plan["errors"], [])
        seg = plan["segments"][1]
        self.assertTrue(seg["continuity"])
        self.assertEqual(seg["continuity_omitted"],
                         "explicit first-frame wins")

    def test_chain_plus_ordered_routes_dtofficial(self):
        # continuity + a numbered image: dtofficial leads with the chain frame
        # (image #1) and keeps the numbered order after it.
        d = self.segdir(images=["f-0002.i.png"])
        plan = self.plan(d, chain=True)
        self.assertEqual(plan["errors"], [])
        seg1, seg2 = plan["segments"]
        self.assertFalse(seg1["continuity"])     # segment 1 has no predecessor
        self.assertTrue(seg2["continuity"])
        self.assertEqual(seg2["queued"], "dtofficial")
        self.assertTrue(any("routes to dtofficial" in w
                            for w in plan["warnings"]))
        self.assertTrue(any("continuity + an ordered image" in e
                            for e in seg2["compile"]["dtcustom"]["errors"]))

    def test_pinned_dtofficial_cannot_run_last(self):
        d = self.segdir(images=["f-0001.l.png"])
        plan = self.plan(d, host="dt-community")
        self.assertTrue(any("last-frame needs an anchor" in e
                            for e in plan["errors"]), plan["errors"])

    def test_pinned_dtcustom_cannot_run_multi_image(self):
        d = self.segdir(images=["f-0001.i.png", "f-0001.i2.png"])
        plan = self.plan(d, host="dt-main")
        self.assertTrue(any("at most one --image" in e
                            for e in plan["errors"]))

    def test_auto_multi_image_routes_dtofficial(self):
        d = self.segdir(images=["f-0001.i.png", "f-0001.i2.png"])
        plan = self.plan(d)
        self.assertEqual(plan["errors"], [])
        self.assertEqual(plan["segments"][0]["queued"], "dtofficial")
        self.assertTrue(any("routes to dtofficial" in w
                            for w in plan["warnings"]))

    def test_error_plan_still_carries_compile_views(self):
        # a plan refused BEFORE compilation (duplicate first-frame slots via
        # an edited manifest) must still carry a stable compile object — the
        # editor renders rows regardless of errors.
        d = self.segdir(images=["f-0001.a.png", "f-0001.b.png"])
        edits = {"f-0001": {"attachments": [
            {"file": "f-0001.a.png", "target": "first"},
            {"file": "f-0001.b.png", "target": "first"}]}}
        plan = ltxq.e1_plan(str(d), self.ns(d), self.c, self.con, edits=edits)
        self.assertTrue(plan["errors"])
        seg = plan["segments"][0]
        self.assertIn("compile", seg)
        self.assertEqual(seg["compile"]["dtcustom"]["assets"], [])
        self.assertEqual(seg["compile"]["dtofficial"]["caps"], [])

    def test_continuity_default_skips_first_segment(self):
        d = self.segdir(wavs=(1, 2, 3))
        plan = self.plan(d, chain=True)
        self.assertEqual([s["continuity"] for s in plan["segments"]],
                         [False, True, True])

    def test_queue_chain_slots(self):
        d = self.segdir(wavs=(1, 2, 3),
                        images=["f-0002.i.png", "f-0003.l.png"])
        plan = self.plan(d, chain=True)
        self.assertEqual(plan["errors"], [])
        ns = self.ns(d, chain=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ltxq.e1_queue(plan, ns, self.c, self.con)
        jobs = self.con.execute("SELECT * FROM jobs ORDER BY created_at, "
                                "rowid").fetchall()
        self.assertEqual(len(jobs), 3)
        self.assertIsNone(jobs[0]["chain"])
        # ordered image + continuity => the chain frame leads as image #1
        self.assertEqual(jobs[1]["chain"], "image_first")
        # a lone last-frame target keeps the legacy single image slot
        self.assertEqual(jobs[2]["chain"], "image")

    def test_job_caps_image_first_needs_multi_image(self):
        job = {"backend": None,
               "assets": json.dumps([{"flag": "--image", "file": "a.png"}]),
               "extra_args": "[]", "chain": "image_first"}
        self.assertIn("multi_image", ltxq.job_caps(job))

    def test_edits_override_and_validate(self):
        d = self.segdir(images=["f-0001.i.png", "f-0001.k24.png"])
        edits = {"f-0001": {"attachments": [
            {"file": "f-0001.k24.png", "target": "keyframe", "index": 24}],
            "continuity": True}}
        plan = ltxq.e1_plan(str(d), self.ns(d), self.c, self.con, edits=edits)
        self.assertEqual(plan["errors"], [])
        seg = plan["segments"][0]
        self.assertEqual([a["file"] for a in seg["attachments"]],
                         ["f-0001.k24.png"])
        self.assertTrue(seg["continuity"])
        bad = {"f-0001": {"attachments": [
            {"file": "nope.png", "target": "ordered"}]}}
        plan2 = ltxq.e1_plan(str(d), self.ns(d), self.c, self.con, edits=bad)
        self.assertTrue(any("attachment image not found" in e
                            for e in plan2["errors"]))

    def test_queue_tail_assets_and_chain(self):
        d = self.segdir(images=["f-0001.k12.png", "f-0002.f.png",
                                "f-0002.l.png"])
        plan = self.plan(d, chain=True)
        self.assertEqual(plan["errors"], [])
        ns = self.ns(d, chain=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            jids = ltxq.e1_queue(plan, ns, self.c, self.con)
        self.assertEqual(len(jids), 2)
        jobs = self.con.execute("SELECT * FROM jobs ORDER BY created_at, "
                                "rowid").fetchall()
        self.assertEqual(len(jobs), 2)
        a1 = json.loads(jobs[0]["assets"])
        self.assertEqual(a1[0]["flag"], "--audio")
        self.assertEqual([a["flag"] for a in a1[1:]], [None])  # keyframe upload
        extra1 = json.loads(jobs[0]["extra_args"])
        self.assertIn("--keyframe", extra1)
        self.assertIn("@f-0001.k12.png@:12", extra1)
        # continuity defaults apply from segment 2; seg1 must not carry it
        self.assertIsNone(jobs[0]["chain"])
        # segment 2 targets .f => continuity omitted, no chain on the job
        self.assertIsNone(jobs[1]["chain"])
        self.assertEqual(jobs[1]["batch"], plan["batch"])
        self.assertEqual(jobs[1]["num_frames"], 121)


class CliPathTests(E1Base):
    def test_cli_cmd_resolves_home_before_quoting(self):
        h = {"cli_path": "~/tod-dt-cli", "home": "/Users/x"}
        self.assertEqual(ltxq.cli_cmd(h, self.c), "/Users/x/tod-dt-cli")
        h2 = {"cli_path": "~/tod-dt-cli", "home": ""}
        self.assertEqual(ltxq.cli_cmd(h2, self.c), "'$HOME/tod-dt-cli'")
        h3 = {"cli_path": "/opt/dt/draw-things-cli", "home": "/Users/x"}
        self.assertEqual(ltxq.cli_cmd(h3, self.c),
                         "/opt/dt/draw-things-cli")


if __name__ == "__main__":
    unittest.main()
