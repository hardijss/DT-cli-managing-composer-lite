"""API tests for the Audio batch E1 endpoints (/api/add-batch-e1/*).

Boots the real Flask app against a throwaway HOME/DB/hosts.yaml (patched
before `import server`) and drives the two-phase flow a dashboard session
takes: preview → PUT edits → queue → discard, plus staged-file serving.

Run:
  ./venv/bin/python -m unittest discover -s tests -t . -v
"""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import ltxq  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="ltxq_e1_api_"))
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

import server  # noqa: E402  (after the patches: HERE/STAGE snapshot the tmp)

server.HERE = TMP
server.STAGE = TMP / "jobs" / "_tmp"


def segdir(name, wavs=(1, 2), images=("f-0001.i.png",), prompts=True):
    d = TMP / name
    d.mkdir(exist_ok=True)
    for n in wavs:
        (d / f"f-{n:04d}.wav").write_bytes(b"RIFF")
        if prompts:
            (d / f"f-{n:04d}.txt").write_text(f"prompt {n}")
    for fn in images:
        (d / fn).write_bytes(b"x")
    return d


class E1ApiBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Two API suites share this process; pin this suite's tree at class
        # start so import/class order cannot leak paths between them.
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

    def put(self, sid, body):
        r = self.client.put(f"/api/add-batch-e1/{sid}/manifest", json=body)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()


class E1ApiTests(E1ApiBase):
    def test_preview_plans_segments(self):
        d = segdir("p1", images=("f-0001.i.png", "f-0002.f.png",
                                 "f-0002.l.png"))
        payload = self.preview(d)
        self.assertIn("sid", payload)
        self.assertEqual(len(payload["plan"]["segments"]), 2)
        self.assertEqual(payload["plan"]["fps"], 25)
        self.assertEqual(sorted(payload["images"]),
                         ["f-0001.i.png", "f-0002.f.png", "f-0002.l.png"])
        seg2 = payload["plan"]["segments"][1]
        self.assertEqual(seg2["continuity_omitted"], None)
        self.assertIn("dtcustom", seg2["compile"])

    def test_preview_uploads(self):
        d = segdir("p2", images=("f-0001.i.png",))
        files = [(io.BytesIO((d / p.name).read_bytes()), p.name)
                 for p in sorted(d.iterdir())]
        r = self.client.post("/api/add-batch-e1/preview",
                             data={"model": "ltx-2-dev", "files": files},
                             content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        payload = r.get_json()
        self.assertEqual(len(payload["plan"]["segments"]), 2)
        self.assertEqual(payload["plan"]["errors"], [])

    def test_preview_honours_chain_default(self):
        d = segdir("p6", images=())
        payload = self.preview(d, {"chain": "1"})
        segs = payload["plan"]["segments"]
        self.assertFalse(segs[0]["continuity"])
        self.assertTrue(segs[1]["continuity"])
        self.assertIs(payload["manifest"]["opts"]["chain"], True)

    def test_put_edits_and_validation(self):
        d = segdir("p3", images=("f-0001.i.png", "f-0001.k24.png"))
        payload = self.preview(d)
        sid = payload["sid"]
        name = payload["plan"]["segments"][0]["name"]
        good = {name: {"attachments": [
                    {"file": "f-0001.k24.png", "target": "keyframe",
                     "index": 24}],
                "continuity": True}}
        payload = self.put(sid, {"edits": good})
        seg = payload["plan"]["segments"][0]
        self.assertEqual([a["file"] for a in seg["attachments"]],
                         ["f-0001.k24.png"])
        self.assertTrue(seg["continuity"])
        self.assertEqual(payload["plan"]["errors"], [])
        bad = {name: {"attachments": [{"file": "nope.png",
                                       "target": "ordered"}]}}
        payload = self.put(sid, {"edits": bad})
        self.assertTrue(any("attachment image not found" in e
                            for e in payload["plan"]["errors"]))
        seg0 = payload["plan"]["segments"][0]
        self.assertIn("compile", seg0)
        self.assertEqual(seg0["compile"]["dtcustom"]["assets"], [])
        r = self.client.post(f"/api/add-batch-e1/{sid}/queue")
        self.assertEqual(r.status_code, 400)

    def test_queue_and_discard(self):
        d = segdir("p4", images=("f-0001.i.png",))
        payload = self.preview(d)
        sid = payload["sid"]
        before = self.con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        r = self.client.post(f"/api/add-batch-e1/{sid}/queue")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        res = r.get_json()
        self.assertEqual(len(res["jids"]), 2)
        after = self.con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        self.assertEqual(after - before, 2)
        again = self.client.get(f"/api/add-batch-e1/{sid}")
        self.assertEqual(again.status_code, 200)
        r = self.client.post(f"/api/add-batch-e1/{sid}/discard")
        self.assertEqual(r.status_code, 200)
        gone = self.client.get(f"/api/add-batch-e1/{sid}")
        self.assertEqual(gone.status_code, 404)

    def test_file_serving_and_traversal(self):
        d = segdir("p5", images=("f-0001.i.png",))
        payload = self.preview(d)
        sid = payload["sid"]
        r = self.client.get(f"/api/add-batch-e1/{sid}/file/f-0001.i.png")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, b"x")
        r = self.client.get(f"/api/add-batch-e1/{sid}/file/%2e%2e%2fhosts.yaml")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
