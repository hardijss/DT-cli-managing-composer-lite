"""API tests for the keyframe-pair batch endpoints (/api/pairs/*).

Boots the real Flask app against a throwaway HOME/DB/hosts.yaml (patched
before `import server`, which snapshots HERE) and drives the full two-phase
flow a dashboard session would take: preview → images/folder append → PUT
edits → queue → staging cleanup.

Run:
  ./venv/bin/python -m unittest discover -s tests -t . -v
"""

import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import ltxq  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="ltxq_pairs_api_"))
ltxq.DB_PATH = TMP / "ltxq.db"
ltxq.HERE = TMP
ltxq.CONF_PATH = TMP / "hosts.yaml"
ltxq._DB_INITED = False
ltxq._img_dims = lambda p: (128, 128)
ltxq._wav_duration = lambda p: 4.84            # 121 frames @ 25 fps
(TMP / "hosts.yaml").write_text("cli_path: ~/tod-dt-cli\ncli_dialect: dtcustom\nhosts: []\n")
(TMP / "templates").mkdir()
(TMP / "templates" / "ltx-2-dev.json").write_text(json.dumps(
    {"width": 128, "height": 128, "fps": 25, "numFrames": 121}))
(TMP / "templates" / "minimax-h3.json").write_text(json.dumps(
    {"width": 128, "height": 128, "fps": 25, "numFrames": 107}))
(TMP / "jobs" / "_tmp").mkdir(parents=True)

import server  # noqa: E402  (after the patches: HERE/STAGE snapshot the tmp)

server.HERE = TMP
server.STAGE = TMP / "jobs" / "_tmp"


def stills_dir(name, numbers=(1, 2, 3), prefix="image", prompts=True):
    d = TMP / name
    d.mkdir(exist_ok=True)
    for n in numbers:
        (d / f"{prefix}{n:04d}.png").write_bytes(b"x")
        if prompts:
            (d / f"prompt{n:04d}.txt").write_text(f"move {n}")
    return d


class PairsApiBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = server.app.test_client()
        cls.con = ltxq.db()

    @classmethod
    def tearDownClass(cls):
        cls.con.close()

    def upload(self, d, field="files"):
        return [(io.BytesIO((d / p.name).read_bytes()), p.name)
                for p in sorted(d.iterdir())]

    def preview(self, d, **extra):
        form = {"model": "ltx-2-dev", "dir": str(d)}
        form.update(extra)
        r = self.client.post("/api/pairs/preview", data=form,
                             content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    def put(self, sid, body):
        r = self.client.put(f"/api/pairs/{sid}/manifest", json=body)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()


class PairsApiTests(PairsApiBase):
    def test_preview_plans_and_derives_view(self):
        payload = self.preview(stills_dir("ap1"), dir=str(TMP / "ap1"))
        self.assertEqual(payload["errors"], [])
        sid = payload["sid"]
        self.assertEqual(len(payload["manifest"]["pairs"]), 2)
        self.assertEqual(payload["view"]["strategy"], "frame_slots")
        self.assertEqual(payload["view"]["grid"], "8n+1")
        self.assertEqual(payload["view"]["fps"], 25.0)
        self.assertEqual([p["frames"] for p in payload["view"]["pairs"]],
                         [121, 121])
        self.assertTrue((_pairs_dir_exists(sid)), "staging session missing")
        self.client.post(f"/api/pairs/{sid}/discard")
        self.assertFalse(_pairs_dir_exists(sid))

    def test_preview_folder_upload(self):
        d = stills_dir("ap2", numbers=(1, 2))
        payload = self.preview(d)                 # no dir: uploaded files
        self.assertEqual(payload["errors"], [])
        self.assertEqual(len(payload["manifest"]["pairs"]), 1)
        self.client.post(f"/api/pairs/{payload['sid']}/discard")

    def test_images_and_folder_append(self):
        payload = self.preview(stills_dir("ap3"), prompt="go")
        sid = payload["sid"]
        d2 = TMP / "ap3b"
        d2.mkdir()
        for n in (8, 9):
            (d2 / f"kf{n:04d}.png").write_bytes(b"x")
        r = self.client.post(f"/api/pairs/{sid}/folder", data={
            "files": [(io.BytesIO(b"x"), f"kf{n:04d}.png") for n in (8, 9)]},
            content_type="multipart/form-data")
        payload = r.get_json()
        self.assertEqual(payload["errors"], [], payload)
        self.assertEqual(len(payload["manifest"]["pairs"]), 3)   # 2 + 1 new pair
        self.assertEqual(payload["manifest"]["pairs"][2]["first"], "kf0008.png")
        r = self.client.post(f"/api/pairs/{sid}/images", data={
            "files": [(io.BytesIO(b"x"), "spare.png")]},
            content_type="multipart/form-data")
        payload = r.get_json()
        self.assertIn("spare.png", payload["manifest"]["stills"])
        self.client.post(f"/api/pairs/{sid}/discard")

    def test_put_edits_and_replan_on_model_change(self):
        payload = self.preview(stills_dir("ap4"))
        sid = payload["sid"]
        pairs = payload["manifest"]["pairs"]
        pairs[0]["prompt"] = "hand fixed"                 # inline override
        pairs[0]["prompt_src"] = "inline"
        pairs[1]["frames_override"] = 118                 # → 121 (8n+1)
        payload = self.put(sid, {"pairs": pairs})
        self.assertEqual(payload["manifest"]["pairs"][0]["prompt"], "hand fixed")
        self.assertEqual(payload["view"]["pairs"][1]["frames"], 121)
        self.assertTrue(any("snapped" in n for p in payload["view"]["pairs"]
                            for n in (p["notes"] if p else [])))
        # model change re-plans the derived layer, keeps the edited truth
        payload = self.put(sid, {"opts": {"model": "minimax-h3", "order": "number",
                                          "on_non_grid": "round-up"}})
        self.assertEqual(payload["view"]["grid"], "17n+5")
        self.assertEqual(payload["manifest"]["pairs"][0]["prompt"], "hand fixed")
        self.client.post(f"/api/pairs/{sid}/discard")

    def test_queue_happy_path_and_staging_cleanup(self):
        payload = self.preview(stills_dir("ap5"), batch="ap5batch")
        sid = payload["sid"]
        r = self.client.post(f"/api/pairs/{sid}/queue",
                             data={"ext": "mov", "seed": "7"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual(len(body["jids"]), 2)
        jobs = [dict(x) for x in self.con.execute(
            "SELECT * FROM jobs WHERE id IN (?,?)", body["jids"])]
        self.assertEqual({j["batch"] for j in jobs}, {"ap5batch"})
        assets = json.loads(jobs[0]["assets"])
        self.assertEqual([a["flag"] for a in assets],
                         ["--first-frame", "--last-frame"])
        self.assertFalse(_pairs_dir_exists(sid), "staging should be gone")

    def test_empty_prompts_warn_but_queue(self):
        d = stills_dir("ap6", prompts=False)
        payload = self.preview(d)
        self.assertEqual(payload["errors"], [])
        self.assertTrue(any("has no prompt" in w for w in payload["warnings"]))
        sid = payload["sid"]
        r = self.client.post(f"/api/pairs/{sid}/queue", data={"ext": "mov"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(len(r.get_json()["jids"]), 2)

    def test_config_fallback_from_last_done_job(self):
        # a model without a template stages the last done job's config — and
        # the staged file must actually be WRITTEN (regression: the path was
        # returned but never written, so _load_cfg died on open)
        with self.con:
            self.con.execute(
                "INSERT INTO jobs(id,created_at,name,parent_id,host,model,prompt,"
                "config_text,status,local_dir,pct_ts,assets,extra_args) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("deadbeef00", 1, "x", None, None, "notpl", "p",
                 json.dumps({"width": 128, "height": 128, "fps": 25,
                             "numFrames": 121}),
                 "done", "/tmp", 1, "[]", "[]"))
        d = stills_dir("ap8", numbers=(1, 2), prompts=False)
        payload = self.preview(d, model="notpl", prompt="go")
        self.assertEqual(payload["errors"], [], payload)
        self.assertEqual(payload["view"]["fps"], 25.0)
        self.assertEqual([p["frames"] for p in payload["view"]["pairs"]], [121])
        self.client.post(f"/api/pairs/{payload['sid']}/discard")

    def test_staged_file_route_and_sid_guard(self):
        payload = self.preview(stills_dir("ap7", numbers=(1, 2)))
        sid = payload["sid"]
        name = payload["manifest"]["pairs"][0]["first"]
        r = self.client.get(f"/api/pairs/{sid}/file/{name}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get(f"/api/pairs/zzzzzzzz/file/{name}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/pairs/{sid}/file/../../hosts.yaml").status_code, 404)
        self.assertEqual(self.client.get("/api/pairs/zzzzzzzz").status_code, 404)
        self.client.post(f"/api/pairs/{sid}/discard")


def _pairs_dir_exists(sid):
    return (server.STAGE / f"pairs_{sid}").is_dir()


if __name__ == "__main__":
    unittest.main()
