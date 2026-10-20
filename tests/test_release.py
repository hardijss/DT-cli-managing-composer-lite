"""Tests for the interactive release controller (Idea 10: E1_variant).

Pins the schema additions, the release snapshot, serial single-lane variant
queueing, review/pick/rate/next transitions, the chain_src carry from the
previous stem's picked variant, and the chain_src fallback note.

Run:
  ./venv/bin/python -m unittest discover -s tests -t . -v
"""

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
HOSTS = ("cli_path: ~/tod-dt-cli\ncli_dialect: dtcustom\nhosts:\n"
         "  - alias: dt-main\n"
         "  - alias: dt-community\n    cli_dialect: dtofficial\n"
         "    cli_path: ~/draw-things-cli\n")


class ReleaseBase(unittest.TestCase):
    def setUp(self):
        self._scratch = tempfile.TemporaryDirectory(prefix="ltxq_rel_")
        self.tmp = Path(self._scratch.name)
        self._saved = (ltxq.DB_PATH, ltxq.HERE, ltxq.CONF_PATH, ltxq._DB_INITED,
                       ltxq._wav_duration)
        ltxq.DB_PATH = self.tmp / "ltxq.db"
        ltxq.HERE = self.tmp
        ltxq.CONF_PATH = self.tmp / "hosts.yaml"
        ltxq._DB_INITED = False
        ltxq._wav_duration = lambda p: 4.84          # 121 frames @ 25 fps
        (self.tmp / "jobs" / "_tmp").mkdir(parents=True)
        (self.tmp / "hosts.yaml").write_text(HOSTS)
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

    def opts(self, **over):
        o = {"model": "ltx-2-dev", "host": None, "ext": "mov",
             "backend": None, "seed": None}
        o.update(over)
        return o

    def release(self, d, **over):
        plan = self.plan(d, **over)
        self.assertEqual(plan["errors"], [])
        rid = ltxq.create_release(self.con, self.c, plan, self.opts(), "interactive")
        return rid, plan

    def job_status(self, jid, status="done"):
        self.con.execute("UPDATE jobs SET status=?, local_out=? WHERE id=?",
                         (status, str(self.tmp / "fake.mov"), jid))
        self.con.commit()

    def jobs(self):
        return self.con.execute("SELECT * FROM jobs ORDER BY created_at, "
                                "rowid").fetchall()


class ReleaseTests(ReleaseBase):
    def test_schema_columns_and_table(self):
        cols = {r["name"] for r in self.con.execute("PRAGMA table_info(jobs)")}
        for c in ("vg", "rseg", "pick", "rating", "release", "chain_src"):
            self.assertIn(c, cols)
        self.con.execute("SELECT id, state, plan FROM releases").fetchall()

    def test_create_release_snapshot(self):
        rid, plan = self.release(self.segdir())
        rel = self.con.execute("SELECT * FROM releases WHERE id=?", (rid,)).fetchone()
        self.assertEqual(rel["mode"], "interactive")
        state = json.loads(rel["state"])
        self.assertEqual([s["status"] for s in state["segments"]],
                         ["setup", "setup"])
        snapshot = json.loads(rel["plan"])
        self.assertEqual(snapshot["model"], "ltx-2-dev")
        self.assertEqual(len(snapshot["segments"]), 2)
        self.assertIn("cfg", snapshot["segments"][0])
        self.assertEqual(snapshot["segments"][0]["cfg"]["fps"], 25)
        # inputs are frozen under jobs/_releases/<rid>/ (staging may vanish)
        frozen = snapshot["segments"][0]
        self.assertIn("_releases", frozen["wav"])
        self.assertTrue(Path(frozen["wav"]).exists())
        self.assertIn("_releases", frozen["dir"])

    def test_serial_tick_and_review(self):
        rid, _ = self.release(self.segdir())
        out = ltxq.release_variants(self.con, self.c, rid, 0, [11, 22])
        self.assertEqual(out["stems"][0]["status"], "queued")
        jobs = self.jobs()
        self.assertEqual(len(jobs), 1)               # serial: one live variant
        extra = json.loads(jobs[0]["extra_args"])
        self.assertIn("--seed", extra)
        self.assertIn("11", extra)
        self.assertFalse(jobs[0]["vg"] is None)
        self.assertEqual(jobs[0]["rseg"], 0)
        self.assertEqual(jobs[0]["release"], rid)
        # no duplicate while the first is still live
        ltxq.release_tick(self.con, self.c)
        self.assertEqual(len(self.jobs()), 1)
        # finish #1 -> the second variant is created
        self.job_status(jobs[0]["id"])
        ltxq.release_tick(self.con, self.c)
        jobs = self.jobs()
        self.assertEqual(len(jobs), 2)
        self.assertIn("22", json.loads(jobs[1]["extra_args"]))
        # finish #2 -> stem goes to review, nothing more is created
        self.job_status(jobs[1]["id"])
        ltxq.release_tick(self.con, self.c)
        self.assertEqual(len(self.jobs()), 2)
        summary = ltxq.release_summary(self.con, rid)
        self.assertEqual(summary["stems"][0]["status"], "review")
        self.assertTrue(all(v["done"] for v in summary["stems"][0]["variants"]))

    def test_rate_and_pick(self):
        rid, _ = self.release(self.segdir())
        ltxq.release_variants(self.con, self.c, rid, 0, [11])
        j1 = self.jobs()[0]["id"]
        self.job_status(j1)
        ltxq.release_tick(self.con, self.c)
        with self.assertRaises(ValueError):
            ltxq.release_rate(self.con, rid, j1, 9)
        ltxq.release_rate(self.con, rid, j1, 4)
        sel = ltxq.release_pick(self.con, self.c, rid, 0, j1)
        self.assertEqual(sel["stems"][0]["pick"], j1)
        row = self.con.execute("SELECT pick, rating FROM jobs WHERE id=?",
                               (j1,)).fetchone()
        self.assertEqual((row["pick"], row["rating"]), (1, 4))
        # a second variant that is not finished cannot be picked
        ltxq.release_variants(self.con, self.c, rid, 0, [77])
        pending = [d for d in ltxq.release_summary(self.con, rid)["stems"][0]
                   ["variants"] if not d["done"] and d["jid"]]
        self.assertTrue(pending)
        with self.assertRaises(ValueError):
            ltxq.release_pick(self.con, self.c, rid, 0, pending[0]["jid"])

    def test_next_requires_pick_and_carries_chain_src(self):
        d = self.segdir()
        rid, _ = self.release(d, chain=True)
        with self.assertRaises(ValueError):
            ltxq.release_next(self.con, rid)
        ltxq.release_variants(self.con, self.c, rid, 0, [11])
        j1 = self.jobs()[0]["id"]
        self.job_status(j1)
        ltxq.release_tick(self.con, self.c)
        ltxq.release_pick(self.con, self.c, rid, 0, j1)
        state = ltxq.release_next(self.con, rid)
        self.assertEqual(state["cursor"], 1)
        self.assertEqual(state["stems"][0]["status"], "done")
        ltxq.release_variants(self.con, self.c, rid, 1, [33])
        row = self.jobs()[-1]
        self.assertEqual(row["rseg"], 1)
        self.assertEqual(row["chain_src"], j1)   # picked predecessor feeds it
        self.assertEqual(row["chain"], "image")

    def test_chain_src_unavailable_note(self):
        job = {"chain": "image", "chain_src": "missing000",
               "batch": None, "assets": "[]", "id": "self0",
               "local_dir": str(self.tmp)}
        note = ltxq.resolve_chain(self.con, job)
        self.assertIn("chain_src unavailable", note)

    def test_abort_stops_scheduling(self):
        rid, _ = self.release(self.segdir())
        ltxq.release_variants(self.con, self.c, rid, 0, [11, 22])
        ltxq.release_abort(self.con, rid)
        j1 = self.jobs()[0]["id"]
        self.job_status(j1)
        ltxq.release_tick(self.con, self.c)
        self.assertEqual(len(self.jobs()), 1)      # aborted: no next variant
        self.assertEqual(ltxq.release_summary(self.con, rid)["status"], "aborted")


if __name__ == "__main__":
    unittest.main()
