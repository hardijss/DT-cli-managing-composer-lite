"""Tests for the keyframe-pair batch composer (ltxq add-pairs / pairs_*).

Pins the model-independent planner (ordering rules, companion numbering,
prompt coverage), the model-dependent resolver (frame-length precedence,
grid snapping, composition strategy per dialect), the queueing tail (assets,
argv spellings, regen round-trips) and the CLI's all-or-nothing refusal.

Run:
  ./venv/bin/python -m unittest discover -s tests -t . -v
"""

import json
import shutil
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import ltxq  # noqa: E402

CFG = {"width": 128, "height": 128, "fps": 25, "numFrames": 121}   # 121 = 8n+1
HOSTS_BOTH = ("cli_path: ~/tod-dt-cli\ncli_dialect: dtcustom\nhosts:\n"
              "  - alias: dt-main\n"
              "  - alias: dt-community\n    cli_dialect: dtofficial\n"
              "    cli_path: ~/draw-things-cli\n")


class PairsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ltxq_pairs_"))
        self._saved = (ltxq.DB_PATH, ltxq.HERE, ltxq.CONF_PATH, ltxq._DB_INITED,
                       ltxq._img_dims, ltxq._wav_duration)
        ltxq.DB_PATH = self.tmp / "ltxq.db"
        ltxq.HERE = self.tmp
        ltxq.CONF_PATH = self.tmp / "hosts.yaml"
        ltxq._DB_INITED = False
        ltxq._img_dims = lambda p: (128, 128)
        ltxq._wav_duration = lambda p: 4.84          # 121 frames @ 25 fps
        (self.tmp / "jobs" / "_tmp").mkdir(parents=True)
        (self.tmp / "hosts.yaml").write_text("cli_path: ~/tod-dt-cli\n"
                                             "cli_dialect: dtcustom\nhosts: []\n")
        self.c = ltxq.conf()
        self.con = ltxq.db()
        ltxq.sync_hosts(self.con)

    def tearDown(self):
        (ltxq.DB_PATH, ltxq.HERE, ltxq.CONF_PATH, ltxq._DB_INITED,
         ltxq._img_dims, ltxq._wav_duration) = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- fixtures -------------------------------------------------------------

    def stilldir(self, name="pairsdir", numbers=(1, 2, 3), prefix="image",
                 ext=".png", unnumbered=()):
        d = self.tmp / name
        d.mkdir()
        for n in numbers:
            (d / f"{prefix}{n:04d}{ext}").write_bytes(b"x")
        for u in unnumbered:
            (d / u).write_bytes(b"x")
        return d

    def sidecar(self, d, name, text="go"):
        p = d / name
        p.write_text(text)
        return p

    def cfg_file(self, name="cfg.json", **over):
        cfg = dict(CFG)
        cfg.update(over)
        cfg = {k: v for k, v in cfg.items() if v is not None}
        p = self.tmp / name
        p.write_text(json.dumps(cfg))
        return str(p)

    def manifest(self, d, **kw):
        kw.setdefault("prompt_text", "go")
        m, errs = ltxq.pairs_plan_from_dir(d, **kw)
        self.assertEqual(errs, [])
        return m

    def resolve(self, m, d, **kw):
        kw.setdefault("model", "ltx-2-dev")
        if "config_file" not in kw:            # not setdefault: cfg_file() would
            kw["config_file"] = self.cfg_file()  # clobber an explicitly passed one
        return ltxq.pairs_resolve(self.c, self.con, m, base_dir=d, **kw)

    def queue(self, m, res, d, **kw):
        return ltxq.pairs_queue(self.c, self.con, m, res, base_dir=d, **kw)

    def jobs(self):
        return [dict(r) for r in self.con.execute(
            "SELECT * FROM jobs ORDER BY created_at, rowid")]

    def assets_of(self, job):
        return [(a["flag"], a["file"]) for a in json.loads(job["assets"])]


class PlanTests(PairsBase):
    def test_number_order_tolerates_gaps_and_disk_order(self):
        d = self.stilldir(numbers=(1, 7, 3))          # created out of order
        m = self.manifest(d)
        self.assertEqual(m["stills"], ["image0001.png", "image0003.png",
                                       "image0007.png"])
        self.assertEqual([(p["first"], p["last"]) for p in m["pairs"]],
                         [("image0001.png", "image0003.png"),
                          ("image0003.png", "image0007.png")])

    def test_non_padded_numbers_sort_numerically(self):
        d = self.stilldir(name="np", numbers=(), prefix="img")
        for n in ("img2.png", "img10.png", "img1.png"):
            (d / n).write_bytes(b"x")
        m = self.manifest(d)
        self.assertEqual(m["stills"], ["img1.png", "img2.png", "img10.png"])

    def test_duplicate_number_refused(self):
        d = self.stilldir(numbers=(1, 2))
        (d / "image0001.jpg").write_bytes(b"x")
        m, errs = ltxq.pairs_plan_from_dir(d)
        self.assertTrue(any("claim number 1" in e for e in errs), errs)

    def test_mixed_prefixes_refused(self):
        d = self.stilldir(numbers=(1, 2))
        (d / "key0003.png").write_bytes(b"x")
        m, errs = ltxq.pairs_plan_from_dir(d)
        self.assertTrue(any("mixed still prefixes" in e for e in errs), errs)

    def test_unnumbered_images_ignored_with_warning(self):
        d = self.stilldir(numbers=(1, 2), unnumbered=("cover.png",))
        m, errs = ltxq.pairs_plan_from_dir(d, prompt_text="go")
        self.assertEqual(errs, [])
        self.assertIn("ignored unnumbered image: cover.png", m["warnings"])

    def test_all_unnumbered_refused_with_rename_hint(self):
        d = self.stilldir(numbers=(), unnumbered=("a.png", "b.png"))
        m, errs = ltxq.pairs_plan_from_dir(d)
        self.assertEqual(m["pairs"], [])
        self.assertTrue(any("no numbered stills" in e and "rename" in e
                            for e in errs), errs)

    def test_order_name_is_alphabetical_over_all_images(self):
        d = self.stilldir(name="byname", numbers=(),
                          unnumbered=("b.png", "a.png", "c.png"))
        m = self.manifest(d, order="name")
        self.assertEqual(m["stills"], ["a.png", "b.png", "c.png"])
        self.assertEqual(len(m["pairs"]), 2)

    def test_fewer_than_two_stills_refused(self):
        d = self.stilldir(numbers=(1,))
        m, errs = ltxq.pairs_plan_from_dir(d, prompt_text="go")
        self.assertTrue(any("need at least 2 stills" in e for e in errs), errs)

    def test_prompt_sidecar_beats_shared_fallback(self):
        d = self.stilldir(numbers=(1, 2))
        self.sidecar(d, "prompt0001.txt", "one")
        m = self.manifest(d, prompt_text="shared")
        self.assertEqual([(p["prompt"], p["prompt_src"]) for p in m["pairs"]],
                         [("one", "prompt0001.txt")])

    def test_empty_prompts_warn_and_queue(self):
        d = self.stilldir(numbers=(1, 2))
        m, errs = ltxq.pairs_plan_from_dir(d)      # an unfinished session is fine
        self.assertEqual(errs, [])
        self.assertEqual(m["pairs"][0]["prompt"], "")
        res, rerrs, rwarns = self.resolve(m, d)
        self.assertEqual(rerrs, [])
        self.assertTrue(any("has no prompt" in w for w in rwarns), rwarns)
        jids = self.queue(m, res, d, model="ltx-2-dev")
        self.assertEqual(len(jids), 1)
        self.assertEqual((Path(self.jobs()[0]["local_dir"]) / "prompt.txt")
                         .read_text(), "")
        self.sidecar(d, "prompt0001.txt", "  ")
        m, errs = ltxq.pairs_plan_from_dir(d)
        res, rerrs, rwarns = self.resolve(m, d)
        self.assertTrue(any("empty prompt (prompt0001.txt)" in w for w in rwarns),
                        rwarns)
        self.assertEqual(rerrs, [])
        d2 = self.stilldir(name="noshared", numbers=(1, 2))
        m, errs = ltxq.pairs_plan_from_dir(d2, prompt_text=" ")
        res, rerrs, rwarns = self.resolve(m, d2)
        self.assertTrue(any("empty prompt (shared)" in w for w in rwarns), rwarns)
        self.assertEqual(rerrs, [])

    def test_companion_numbers_match_as_integers(self):
        d = self.stilldir(numbers=(1, 2))             # one pair
        self.sidecar(d, "prompt1.txt")
        m = self.manifest(d)
        self.assertEqual(m["pairs"][0]["prompt_src"], "prompt1.txt")

    def test_orphan_and_duplicate_companions(self):
        d = self.stilldir(numbers=(1, 2))             # one pair
        self.sidecar(d, "prompt0001.txt", "first")
        self.sidecar(d, "prompt5.txt")
        self.sidecar(d, "readme.txt", "not a prompt")
        m, errs = ltxq.pairs_plan_from_dir(d, prompt_text="go")
        self.assertEqual(errs, [])
        self.assertTrue(any("orphan prompt5.txt: no pair 5" in w
                            for w in m["warnings"]), m["warnings"])
        self.assertIn("ignored .txt file without a number: readme.txt",
                      m["warnings"])
        self.sidecar(d, "alt1.txt", "second")
        m, errs = ltxq.pairs_plan_from_dir(d, prompt_text="go")
        self.assertTrue(any("two prompt sidecars claim pair 1" in e for e in errs),
                        errs)

    def test_config_and_audio_companions_attached(self):
        d = self.stilldir(numbers=(1, 2, 3, 4))       # three pairs
        self.sidecar(d, "cfg0002.json", '{"steps": 9}')
        (d / "audio0003.wav").write_bytes(b"x")
        m = self.manifest(d)
        self.assertIsNone(m["pairs"][0]["config"])
        self.assertEqual(m["pairs"][1]["config"], "cfg0002.json")
        self.assertEqual(m["pairs"][2]["audio"], "audio0003.wav")


class ResolveTests(PairsBase):
    def test_template_numframes_uniform(self):
        d = self.stilldir()
        res, errs, warns = self.resolve(self.manifest(d), d)
        self.assertEqual(errs, [])
        self.assertEqual([p["frames"] for p in res["pairs"]], [121, 121])
        self.assertEqual(res["grid"], "8n+1")
        self.assertEqual(res["fps"], 25.0)

    def test_config_sidecar_numframes_wins_over_template(self):
        d = self.stilldir()
        self.sidecar(d, "config0001.json", '{"numFrames": 89}')     # 8n+1
        res, errs, warns = self.resolve(self.manifest(d), d)
        self.assertEqual([p["frames"] for p in res["pairs"]], [89, 121])
        self.assertFalse([w for w in warns if "89" in w])

    def test_off_grid_sidecar_numframes_warns_but_sticks(self):
        d = self.stilldir(numbers=(1, 2))
        self.sidecar(d, "config0001.json", '{"numFrames": 60}')
        res, errs, warns = self.resolve(self.manifest(d), d)
        self.assertEqual(res["pairs"][0]["frames"], 60)
        self.assertTrue(any("numFrames 60" in w for w in warns), warns)

    def test_frames_per_pair_loses_to_config_sidecar(self):
        d = self.stilldir()
        self.sidecar(d, "config0001.json", '{"numFrames": 89}')
        res, errs, warns = self.resolve(self.manifest(d), d, frames_per_pair=113)
        self.assertEqual([p["frames"] for p in res["pairs"]], [89, 113])

    def test_audio_frames_win_and_fit(self):
        d = self.stilldir(numbers=(1, 2))
        (d / "audio0001.wav").write_bytes(b"x")
        res, errs, warns = self.resolve(self.manifest(d), d)
        self.assertEqual(res["pairs"][0]["frames"], 121)
        self.assertIsNone(res["pairs"][0]["fit"])
        ltxq._wav_duration = lambda p: 7.3              # 182.5 → 182 → snap up
        res, errs, warns = self.resolve(self.manifest(d), d)
        self.assertEqual(res["pairs"][0]["frames"], 185)
        self.assertEqual(res["pairs"][0]["fit"], "pad")
        self.assertTrue(any("padded" in n for n in res["pairs"][0]["notes"]))

    def test_audio_under_grid_floor_refused(self):
        d = self.stilldir(numbers=(1, 2))
        (d / "audio0001.wav").write_bytes(b"x")
        ltxq._wav_duration = lambda p: 0.1              # 2.5 frames < floor 9
        res, errs, warns = self.resolve(self.manifest(d), d)
        self.assertTrue(any("under the 9-frame minimum" in e for e in errs), errs)

    def test_audio_without_fps_refused(self):
        d = self.stilldir(numbers=(1, 2))
        (d / "audio0001.wav").write_bytes(b"x")
        res, errs, warns = self.resolve(self.manifest(d), d,
                                        config_file=self.cfg_file(fps=None))
        self.assertTrue(any("needs a template fps" in e for e in errs), errs)

    def test_config_numframes_conflicting_audio_refused(self):
        d = self.stilldir(numbers=(1, 2))
        (d / "audio0001.wav").write_bytes(b"x")
        self.sidecar(d, "config0001.json", '{"numFrames": 89}')     # audio says 121
        res, errs, warns = self.resolve(self.manifest(d), d)
        self.assertTrue(any("numFrames 89 conflicts with audio-derived 121" in e
                            for e in errs), errs)

    def test_frames_override_snaps_nearest_ties_up(self):
        d = self.stilldir()                            # two pairs
        m = self.manifest(d)
        m["pairs"][0]["frames_override"] = 120          # → 121 (up is nearer)
        m["pairs"][1]["frames_override"] = 114          # → 113 (down is nearer)
        res, errs, warns = self.resolve(m, d)
        self.assertEqual([p["frames"] for p in res["pairs"]], [121, 113])
        self.assertEqual(len([n for p in res["pairs"] for n in p["notes"]
                              if "snapped" in n]), 2)

    def test_override_tie_rounds_up(self):
        d = self.stilldir(numbers=(1, 2))
        m = self.manifest(d)
        m["pairs"][0]["frames_override"] = 117          # 113 and 121 both 4 away
        res, errs, warns = self.resolve(m, d)
        self.assertEqual(res["pairs"][0]["frames"], 121)

    def test_override_beats_audio_and_refits_wav(self):
        d = self.stilldir(numbers=(1, 2))
        (d / "audio0001.wav").write_bytes(b"x")
        m = self.manifest(d)
        m["pairs"][0]["frames_override"] = 89
        res, errs, warns = self.resolve(m, d)
        self.assertEqual(res["pairs"][0]["frames"], 89)
        self.assertEqual(res["pairs"][0]["fit"], "trim")
        self.assertTrue(any("overriding audio-derived length" in n
                            for n in res["pairs"][0]["notes"]))

    def test_no_frame_length_source_refused(self):
        d = self.stilldir()
        res, errs, warns = self.resolve(self.manifest(d), d,
                                        config_file=self.cfg_file(numFrames=None))
        self.assertTrue(any("no frame length source" in e for e in errs), errs)

    def test_resolution_mismatch_warns(self):
        d = self.stilldir(numbers=(1, 2))
        ltxq._img_dims = lambda p: (256, 256)
        res, errs, warns = self.resolve(self.manifest(d), d)
        self.assertTrue(any("engine will resize" in w for w in warns), warns)
        self.assertEqual(errs, [])


class StrategyTests(PairsBase):
    def setUp(self):
        super().setUp()
        (self.tmp / "hosts.yaml").write_text(HOSTS_BOTH)
        self.c = ltxq.conf()
        self.con = ltxq.db()
        ltxq.sync_hosts(self.con)

    def test_unpinned_and_dtcustom_compose_frame_slots(self):
        self.assertEqual(ltxq.pairs_strategy(self.c, self.con, None), "frame_slots")
        self.assertEqual(ltxq.pairs_strategy(self.c, self.con, "dt-main"),
                         "frame_slots")

    def test_dtofficial_composes_canvas_ref(self):
        self.assertEqual(ltxq.pairs_strategy(self.c, self.con, "dt-community"),
                         "canvas_ref")

    def test_unknown_host_refused(self):
        with self.assertRaises(SystemExit):
            ltxq.pairs_strategy(self.c, self.con, "ghost")

    def test_pair_assets_spellings(self):
        self.assertEqual(
            [f for f, _ in ltxq._pair_assets("frame_slots", "a.png", "b.png", "w.wav")],
            ["--first-frame", "--last-frame", "--audio"])
        self.assertEqual(
            [f for f, _ in ltxq._pair_assets("canvas_ref", "a.png", "b.png", None)],
            ["--image", "--image"])


class QueueTests(PairsBase):
    def setUp(self):
        super().setUp()
        (self.tmp / "hosts.yaml").write_text(HOSTS_BOTH)
        self.c = ltxq.conf()
        self.con = ltxq.db()
        ltxq.sync_hosts(self.con)

    def test_frame_slots_jobs_and_runner(self):
        d = self.stilldir()
        m = self.manifest(d)
        res, errs, warns = self.resolve(m, d)
        jids = self.queue(m, res, d, model="ltx-2-dev", batch="story", seed=7)
        self.assertEqual(len(jids), 2)
        jobs = self.jobs()
        self.assertEqual([j["name"] for j in jobs], ["pair-0001", "pair-0002"])
        self.assertTrue(all(j["batch"] == "story" for j in jobs))
        self.assertEqual(self.assets_of(jobs[0]),
                         [("--first-frame", "image0001.png"),
                          ("--last-frame", "image0002.png")])
        self.assertEqual(json.loads(jobs[0]["extra_args"]), ["--seed", "7"])
        self.assertEqual(jobs[0]["num_frames"], 121)
        self.assertEqual(jobs[0]["fps"], 25.0)
        cfg = json.loads(jobs[0]["config_text"])
        self.assertEqual(cfg["numFrames"], 121)
        runner = (Path(jobs[0]["local_dir"]) / "runner.sh").read_text()
        self.assertIn("--first-frame image0001.png", runner)
        self.assertIn("--last-frame image0002.png", runner)
        caps = ltxq.job_caps(jobs[0])
        self.assertIn("fflf_preflight", caps)

    def test_canvas_ref_jobs_on_dtofficial_host(self):
        d = self.stilldir()
        m = self.manifest(d)
        res, errs, warns = self.resolve(m, d, host="dt-community")
        self.assertEqual(res["strategy"], "canvas_ref")
        jids = self.queue(m, res, d, model="ltx-2-dev", host="dt-community")
        jobs = self.jobs()
        self.assertEqual(self.assets_of(jobs[0]),
                         [("--image", "image0001.png"), ("--image", "image0002.png")])
        caps = ltxq.job_caps(jobs[0])
        self.assertNotIn("fflf_preflight", caps)
        self.assertEqual(caps, {"image"})
        runner = (Path(jobs[0]["local_dir"]) / "runner.sh").read_text()
        self.assertIn("--image image0001.png --image image0002.png", runner)

    def test_edited_manifest_reorder_delete_and_inline_prompt(self):
        d = self.stilldir(numbers=(1, 2, 3, 4))
        m = self.manifest(d)
        m["pairs"] = [m["pairs"][2], m["pairs"][1]]      # drop pair 1, reorder
        m["pairs"][1]["prompt"] = "hand fixed"
        m["pairs"][1]["prompt_src"] = "inline"
        res, errs, warns = self.resolve(m, d)
        self.assertEqual(errs, [])
        self.assertTrue(any("seam" in w for w in warns), warns)
        self.assertTrue(any("unused still" in w for w in warns), warns)
        self.queue(m, res, d, model="ltx-2-dev")
        jobs = self.jobs()
        self.assertEqual([j["name"] for j in jobs], ["pair-0001", "pair-0002"])
        self.assertEqual(self.assets_of(jobs[0])[0],
                         ("--first-frame", "image0003.png"))
        self.assertEqual((Path(jobs[1]["local_dir"]) / "prompt.txt").read_text(),
                         "hand fixed")

    def test_fitted_wav_replaces_sidecar(self):
        def fake_fit(src, frames, fps, dst, mode):
            shutil.copyfile(src, dst)
            return dst
        ltxq._fit_wav = fake_fit
        d = self.stilldir(numbers=(1, 2))
        (d / "audio0001.wav").write_bytes(b"x")
        ltxq._wav_duration = lambda p: 7.3              # 182 → snap up 185 → pad
        m = self.manifest(d)
        res, errs, warns = self.resolve(m, d)
        self.queue(m, res, d, model="ltx-2-dev")
        flags = [f for f, _ in self.assets_of(self.jobs()[0])]
        self.assertEqual(flags, ["--first-frame", "--last-frame", "--audio"])
        self.assertTrue(self.assets_of(self.jobs()[0])[2][1].endswith(".wav"))

    def test_half_pair_composition_frame_slots(self):
        d = self.stilldir(numbers=(1, 2))              # one pair (0001 → 0002)
        m = self.manifest(d)
        m["pairs"][0]["first"] = ""                    # drop the first still
        res, errs, warns = self.resolve(m, d)
        self.assertEqual(errs, [])
        self.assertTrue(any("lone --last-frame" in w for w in warns), warns)
        self.queue(m, res, d, model="ltx-2-dev")
        self.assertEqual(self.assets_of(self.jobs()[0]),
                         [("--last-frame", "image0002.png")])
        m["pairs"][0] = {"first": "image0001.png", "last": "", "prompt": "p",
                         "prompt_src": "inline", "config": None, "audio": None,
                         "frames_override": None}      # drop the last instead
        res, errs, warns = self.resolve(m, d)
        self.assertEqual(errs, [])
        self.assertTrue(any("composed as --image" in w for w in warns), warns)
        self.queue(m, res, d, model="ltx-2-dev")
        self.assertEqual(self.assets_of(self.jobs()[1]),
                         [("--image", "image0001.png")])

    def test_pair_with_no_stills_at_all_refused(self):
        d = self.stilldir(numbers=(1, 2))
        m = self.manifest(d)
        m["pairs"][0].update({"first": "", "last": ""})
        res, errs, warns = self.resolve(m, d)
        self.assertTrue(any("has no stills" in e for e in errs), errs)

    def test_half_pair_composition_canvas_ref(self):
        d = self.stilldir(numbers=(1, 2))
        m = self.manifest(d)
        m["pairs"][0]["first"] = ""                    # keep the last still only
        res, errs, warns = self.resolve(m, d, host="dt-community")
        self.assertEqual(errs, [])
        self.assertTrue(any("canvas image" in w for w in warns), warns)
        self.queue(m, res, d, model="ltx-2-dev", host="dt-community")
        self.assertEqual(self.assets_of(self.jobs()[0]),
                         [("--image", "image0002.png")])

    def test_negative_seed_means_random_per_pair(self):
        d = self.stilldir(numbers=(1, 2))
        m = self.manifest(d)
        res, errs, warns = self.resolve(m, d)
        self.queue(m, res, d, model="ltx-2-dev", seed=-1)
        extra = json.loads(self.jobs()[0]["extra_args"])
        self.assertNotIn("--seed", extra)              # random per pair
        self.queue(m, res, d, model="ltx-2-dev", seed=7)
        extra = json.loads(self.jobs()[1]["extra_args"])
        self.assertEqual(extra, ["--seed", "7"])

    def test_regen_roundtrip_frame_slots(self):
        d = self.stilldir(numbers=(1, 2))
        m = self.manifest(d)
        res, errs, warns = self.resolve(m, d)
        jid = self.queue(m, res, d, model="ltx-2-dev", batch="story")[0]
        ltxq.cmd_regen(Namespace(id=jid, model=None, config_json=None, host=None,
                                 name=None, seed=None, new_seed=False, frames=None,
                                 ext=None, backend=None, batch=None))
        jobs = self.jobs()
        self.assertEqual(len(jobs), 2)
        self.assertEqual(self.assets_of(jobs[1]),
                         [("--first-frame", "image0001.png"),
                          ("--last-frame", "image0002.png")])
        self.assertEqual(jobs[1]["batch"], "story")

    def test_regen_roundtrip_canvas_ref(self):
        d = self.stilldir(numbers=(1, 2))
        m = self.manifest(d)
        res, errs, warns = self.resolve(m, d, host="dt-community")
        jid = self.queue(m, res, d, model="ltx-2-dev", host="dt-community")[0]
        ltxq.cmd_regen(Namespace(id=jid, model=None, config_json=None, host=None,
                                 name=None, seed=None, new_seed=False, frames=None,
                                 ext=None, backend=None, batch=None))
        jobs = self.jobs()
        self.assertEqual(self.assets_of(jobs[1]),
                         [("--image", "image0001.png"), ("--image", "image0002.png")])


class CliTests(PairsBase):
    def ns(self, d, **kw):
        kw.setdefault("dir", str(d))
        kw.setdefault("model", "ltx-2-dev")
        kw.setdefault("prompt_file", None)
        if "config_file" not in kw:            # not setdefault: cfg_file() would
            kw["config_file"] = self.cfg_file()  # clobber an explicitly passed one
        kw.setdefault("config_json", None)
        kw.setdefault("host", None)
        kw.setdefault("seed", None)
        kw.setdefault("ext", "mov")
        kw.setdefault("backend", None)
        kw.setdefault("batch", None)
        kw.setdefault("order", "number")
        kw.setdefault("frames_per_pair", None)
        kw.setdefault("on_non_grid", "round-up")
        return Namespace(**kw)

    def test_all_or_nothing_refusal(self):
        d = self.stilldir()                              # no prompt sidecars
        with self.assertRaises(SystemExit) as cm:
            ltxq._cmd_add_pairs(self.c, self.con, self.ns(
                d, config_file=self.cfg_file(numFrames=None)))
        self.assertIn("batch refused: 2 problem(s) across 2 pairs — nothing queued",
                      str(cm.exception))
        self.assertEqual(self.jobs(), [])

    def test_add_negative_seed_becomes_recorded_random(self):
        prompt = self.tmp / "p.txt"
        prompt.write_text("go")
        ns = Namespace(model="ltx-2-dev", prompt_file=str(prompt),
                       config_file=self.cfg_file(), config_json=None, host=None,
                       name=None, parent=None, seed=-1, new_seed=False,
                       frames=None, ext="mov", backend=None, chain=None,
                       batch=None, upload=[], extra_arg=[])
        ltxq._cmd_add(self.c, self.con, ns)
        extra = json.loads(self.jobs()[0]["extra_args"])
        self.assertIn("--seed", extra)                 # generated, not passed as -1
        self.assertGreater(int(extra[extra.index("--seed") + 1]), 0)

    def test_happy_path_queues_pairs(self):
        d = self.stilldir()
        for k in range(1, 3):
            self.sidecar(d, f"prompt{k:04d}.txt", f"move {k}")
        ltxq._cmd_add_pairs(self.c, self.con, self.ns(d))
        jobs = self.jobs()
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["batch"], "pairsdir")
        self.assertEqual((Path(jobs[0]["local_dir"]) / "prompt.txt").read_text(),
                         "move 1")


if __name__ == "__main__":
    unittest.main()
