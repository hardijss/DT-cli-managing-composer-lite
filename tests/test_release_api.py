"""API tests for the interactive release endpoints (/api/e1/release/*).

Boots the real Flask app on a throwaway tree and drives the Idea 10 flow:
E1 session → release(interactive) → variants (serial) → rate/pick → next →
next stem carries chain_src; plus auto mode parity and validation errors.

Run:
  ./venv/bin/python -m unittest discover -s tests -t . -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import ltxq  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="ltxq_rel_api_"))
ltxq.DB_PATH = TMP / "ltxq.db"
ltxq.HERE = TMP
ltxq.CONF_PATH = TMP / "hosts.yaml"
ltxq._DB_INITED = False
ltxq._img_dims = lambda p: (128, 128)
ltxq._wav_duration = lambda p: 4.84            # 121 frames @ 25 fps
(TMP / "hosts.yaml").write_text("cli_path: ~/tod-dt-cli\ncli_dialect: dtcustom\n"
                                "hosts:\n"
                                "  - alias: dt-main\n"
                                "  - alias: dt-community\n"
                                "    cli_dialect: dtofficial\n")
(TMP / "templates").mkdir()
(TMP / "templates" / "ltx-2-dev.json").write_text(json.dumps(
    {"width": 128, "height": 128, "fps": 25, "numFrames": 121}))
(TMP / "jobs" / "_tmp").mkdir(parents=True)

import server  # noqa: E402  (after the patches)

server.HERE = TMP
server.STAGE = TMP / "jobs" / "_tmp"


def segdir(name, wavs=(1, 2), images=(), prompts=True):
    d = TMP / name
    d.mkdir(exist_ok=True)
    for n in wavs:
        (d / f"f-{n:04d}.wav").write_bytes(b"RIFF")
        if prompts:
            (d / f"f-{n:04d}.txt").write_text(f"prompt {n}")
    for fn in images:
        (d / fn).write_bytes(b"x")
    return d


class ReleaseApiBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ltxq.DB_PATH = TMP / "ltxq.db"
        ltxq.HERE = TMP
        ltxq.CONF_PATH = TMP / "hosts.yaml"
        ltxq._DB_INITED = False
        ltxq._img_dims = lambda p: (128, 128)
        ltxq._wav_duration = lambda p: 4.84
        server.HERE = TMP
        server.STAGE = TMP / "jobs" / "_tmp"
        cls.client = server.app.test_client()
        cls.con = ltxq.db()

    @classmethod
    def tearDownClass(cls):
        cls.con.close()

    def preview(self, d, extra=None):
        form = {"model": "ltx-2-dev", "dir": str(d)}
        form.update(extra or {})
        r = self.client.post("/api/add-batch-e1/preview", data=form,
                             content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    def release(self, sid, mode="interactive"):
        r = self.client.post("/api/e1/release", json={"sid": sid, "mode": mode})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    def post(self, url, body=None, status=200):
        r = self.client.post(url, json=body or {})
        self.assertEqual(r.status_code, status, r.get_data(as_text=True))
        return r.get_json()

    def mark_done(self, jid):
        self.con.execute("UPDATE jobs SET status='done', local_out=? WHERE id=?",
                         (str(TMP / "fake.mov"), jid))
        self.con.commit()

    def jobs_for(self, rid):
        return self.con.execute(
            "SELECT * FROM jobs WHERE release=? ORDER BY created_at, rowid",
            (rid,)).fetchall()


class ReleaseApiTests(ReleaseApiBase):
    def test_full_interactive_flow(self):
        payload = self.preview(segdir("r1"), {"chain": "1"})
        rel = self.release(payload["sid"])
        rid = rel["rid"]
        self.assertEqual(rel["mode"], "interactive")
        self.assertEqual([s["status"] for s in rel["stems"]],
                         ["setup", "setup"])
        # variants: serial — exactly one job is created
        data = self.post(f"/api/e1/release/{rid}/segments/0/variants",
                         {"seeds": [11, 22]})
        self.assertEqual(data["stems"][0]["status"], "queued")
        jobs = self.jobs_for(rid)
        self.assertEqual(len(jobs), 1)
        self.assertIn("11", json.loads(jobs[0]["extra_args"]))
        self.assertEqual(jobs[0]["rseg"], 0)
        # live variant blocks the next one
        self.post(f"/api/e1/release/{rid}/tick")
        self.assertEqual(len(self.jobs_for(rid)), 1)
        # finish it -> second variant appears on the next tick
        self.mark_done(jobs[0]["id"])
        self.post(f"/api/e1/release/{rid}/tick")
        jobs = self.jobs_for(rid)
        self.assertEqual(len(jobs), 2)
        self.assertIn("22", json.loads(jobs[1]["extra_args"]))
        self.mark_done(jobs[1]["id"])
        data = self.post(f"/api/e1/release/{rid}/tick")
        self.assertEqual(data["stems"][0]["status"], "review")
        # rate + pick
        data = self.post(f"/api/e1/release/{rid}/segments/0/rate",
                         {"jid": jobs[0]["id"], "rating": 4})
        rated = [v for s in data["stems"] for v in s["variants"]
                 if v["jid"] == jobs[0]["id"]][0]
        self.assertEqual(rated["rating"], 4)
        data = self.post(f"/api/e1/release/{rid}/segments/0/pick",
                         {"jid": jobs[0]["id"]})
        self.assertEqual(data["stems"][0]["pick"], jobs[0]["id"])
        # next stem requires variants; its job must chain from the pick
        data = self.post(f"/api/e1/release/{rid}/next")
        self.assertEqual(data["cursor"], 1)
        self.post(f"/api/e1/release/{rid}/segments/1/variants",
                  {"seeds": [33]})
        row = self.jobs_for(rid)[-1]
        self.assertEqual(row["rseg"], 1)
        self.assertEqual(row["chain_src"], jobs[0]["id"])
        self.assertEqual(row["chain"], "image")

    def test_auto_mode_is_classic_queue(self):
        before = self.con.execute(
            "SELECT COUNT(*) FROM releases").fetchone()[0]
        payload = self.preview(segdir("r2"))
        rel = self.release(payload["sid"], "auto")
        self.assertEqual(rel["mode"], "auto")
        self.assertEqual(len(rel["jids"]), 2)
        jobs = self.con.execute(
            "SELECT * FROM jobs WHERE release IS NULL").fetchall()
        self.assertEqual(len(jobs), 2)
        self.assertTrue(all(j["vg"] is None for j in jobs))
        after = self.con.execute(
            "SELECT COUNT(*) FROM releases").fetchone()[0]
        self.assertEqual(after, before)       # auto mode creates no release

    def test_validation_errors(self):
        payload = self.preview(segdir("r3"))
        rid = self.release(payload["sid"])["rid"]
        self.post(f"/api/e1/release/{rid}/segments/0/variants",
                  {"seeds": []}, status=400)
        self.post(f"/api/e1/release/{rid}/next", status=400)   # no pick yet
        r = self.client.get("/api/e1/release/nope0000")
        self.assertEqual(r.status_code, 404)
        self.post("/api/e1/release/nope0000/next", status=400)  # unknown rid
        # variants -> one queued job for THIS release only
        self.post(f"/api/e1/release/{rid}/segments/0/variants",
                  {"seeds": [55]})
        jid = self.jobs_for(rid)[0]["id"]
        self.post(f"/api/e1/release/{rid}/segments/0/rate",
                  {"jid": jid, "rating": 9}, status=400)       # bounds
        self.post(f"/api/e1/release/{rid}/segments/0/rate",
                  {"jid": jid, "rating": 4})                   # rating is fine
        self.post(f"/api/e1/release/{rid}/segments/0/pick",
                  {"jid": jid}, status=400)                    # not done yet


if __name__ == "__main__":
    unittest.main()
