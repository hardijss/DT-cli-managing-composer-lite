"""Tests for the LLM prompt-synthesis helper (llm.yaml endpoints, template
override, /api/llm/* and the pairs /synth flow).

Same environment pattern as test_pairs_api.py: throwaway HOME/DB/hosts.yaml
patched before `import server`, then the real Flask app via test_client.
The OpenAI-compatible HTTP layer is exercised for real against a stdlib
http.server fake; the pairs flow stubs `ltxq.llm_chat` directly.

Run:
  ./venv/bin/python -m unittest discover -s tests -t . -v
"""

import io
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import ltxq  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="ltxq_llm_"))
ltxq.DB_PATH = TMP / "ltxq.db"
ltxq.HERE = TMP
ltxq.CONF_PATH = TMP / "hosts.yaml"
ltxq.LLM_PATH = TMP / "llm.yaml"
ltxq._DB_INITED = False
ltxq._img_dims = lambda p: (128, 128)
ltxq._wav_duration = lambda p: 4.84            # 121 frames @ 25 fps
(TMP / "hosts.yaml").write_text("cli_path: ~/tod-dt-cli\ncli_dialect: dtcustom\nhosts: []\n")
(TMP / "llm.yaml").write_text(
    "active: test-llm\ntimeout_s: 15\nendpoints:\n"
    "  - name: test-llm\n    base_url: http://127.0.0.1:1/v1\n"
    "  - name: other-llm\n    base_url: http://127.0.0.1:2/v1\n"
    "    model: canned-vl\n")
(TMP / "templates").mkdir()
(TMP / "templates" / "llm_directives").mkdir()
(TMP / "templates" / "llm_directives" / "ltx-default.txt").write_text(
    "TEST DEFAULT DIRECTIVE — describe the transition.")
(TMP / "templates" / "llm_directives" / "dur-test.txt").write_text(
    "aligns with the {{DUR}}-second mark")
(TMP / "templates" / "ltx-2-dev.json").write_text(json.dumps(
    {"width": 128, "height": 128, "fps": 25, "numFrames": 121}))
(TMP / "jobs" / "_tmp").mkdir(parents=True)

import server  # noqa: E402  (after the patches: HERE/STAGE snapshot the tmp)

server.HERE = TMP
server.STAGE = TMP / "jobs" / "_tmp"


# --- a fake OpenAI-compatible server for the real-HTTP client tests ----------

class _FakeOpenAI(BaseHTTPRequestHandler):
    def log_message(self, *a):                 # keep test output clean
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/models"):
            self._send(200, {"data": [{"id": "zeta-vl"}, {"id": "alpha-vl"}]})
        else:
            self._send(404, {"error": "no such route"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        d = json.loads(self.rfile.read(n) or b"{}")
        if self.path.endswith("/chat/completions"):
            parts = d["messages"][-1]["content"]
            nimg = sum(1 for p in parts if isinstance(p, dict)
                       and p.get("type") == "image_url")
            self._send(200, {"choices": [{"message": {
                "content": f"prompt from {d['model']} with {nimg} images"}}]})
        elif self.path.endswith("/broken"):
            self._send(500, {"error": "boom"})
        else:
            self._send(404, {"error": "no such route"})


def stills_dir(name, numbers=(1, 2, 3), prefix="image", prompts=False):
    d = TMP / name
    d.mkdir(exist_ok=True)
    for n in numbers:
        (d / f"{prefix}{n:04d}.png").write_bytes(b"\x89PNG fake")
    return d


class LLMBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenAI)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}/v1"
        cls.client = server.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        # Same save/restore idiom as test_pairs.py: sibling suite modules
        # patch these globals at import time (after this module's own module
        # level patch), so every test re-asserts its world before running.
        self._saved = (ltxq.DB_PATH, ltxq.HERE, ltxq.CONF_PATH, ltxq.LLM_PATH,
                       ltxq._DB_INITED, server.HERE, server.STAGE)
        ltxq.DB_PATH = TMP / "ltxq.db"
        ltxq.HERE = TMP
        ltxq.CONF_PATH = TMP / "hosts.yaml"
        ltxq.LLM_PATH = TMP / "llm.yaml"
        ltxq._DB_INITED = False
        server.HERE = TMP
        server.STAGE = TMP / "jobs" / "_tmp"

    def tearDown(self):
        (ltxq.DB_PATH, ltxq.HERE, ltxq.CONF_PATH, ltxq.LLM_PATH,
         ltxq._DB_INITED, server.HERE, server.STAGE) = self._saved
        shutil.rmtree(TMP / "llm_directives", ignore_errors=True)  # PUT overrides
        server.SYNTH_RUN.update(running=False, sid=None, done=0, total=0,
                                current=None, errors=[], skipped=[])

    def preview(self, d):
        r = self.client.post("/api/pairs/preview",
                             data={"model": "ltx-2-dev", "dir": str(d)},
                             content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()


class LLMParsingTests(LLMBase):
    def test_conf_defaults_when_file_missing(self):
        ltxq.LLM_PATH = TMP / "does-not-exist.yaml"
        try:
            c = ltxq.llm_conf()
            self.assertEqual(c["endpoints"], ltxq._DEFAULT_LLM_ENDPOINTS)
            self.assertEqual(c["active"], "ollama-local")
            self.assertEqual(c["timeout_s"], 120)
        finally:
            ltxq.LLM_PATH = TMP / "llm.yaml"

    def test_conf_sanitizes_entries(self):
        c = ltxq.llm_conf()
        self.assertEqual([e["name"] for e in c["endpoints"]],
                         ["test-llm", "other-llm"])
        self.assertEqual(c["active"], "test-llm")
        self.assertEqual(c["timeout_s"], 15)

    def test_endpoint_lookup(self):
        ep, timeout, err = ltxq.llm_endpoint("other-llm")
        self.assertIsNone(err)
        self.assertEqual(ep["model"], "canned-vl")
        self.assertEqual(timeout, 15)
        ep, _, err = ltxq.llm_endpoint(None)          # active wins
        self.assertEqual(ep["name"], "test-llm")
        _, _, err = ltxq.llm_endpoint("nope")
        self.assertIn("no LLM endpoint named", err)


class LLMClientTests(LLMBase):
    def test_models_round_trip(self):
        # real urllib call against the fake server
        self.assertEqual(ltxq.llm_models(self.base, timeout=5),
                         ["alpha-vl", "zeta-vl"])

    def test_chat_round_trip_sends_two_images(self):
        f = TMP / "f.png"
        f.write_bytes(b"\x89PNG fake")
        msgs = ltxq.llm_pair_messages(f, f, "make it moody",
                                      "TEST TEMPLATE", TMP)
        self.assertEqual(msgs[0]["role"], "system")
        parts = msgs[1]["content"]
        self.assertIn("make it moody", parts[0]["text"])
        out = ltxq.llm_chat(self.base, "vl-model", msgs, timeout=5)
        self.assertEqual(out, "prompt from vl-model with 2 images")

    def test_unreachable_endpoint_raises_clean_error(self):
        with self.assertRaises(ltxq.LLMError) as cm:
            ltxq.llm_models("http://127.0.0.1:9/v1", timeout=2)
        self.assertIn("unreachable", str(cm.exception))

    def test_http_error_and_bad_json(self):
        with self.assertRaises(ltxq.LLMError) as cm:
            ltxq._llm_http(self.base, "/broken", payload={}, timeout=5)
        self.assertIn("HTTP 500", str(cm.exception))
        with self.assertRaises(ltxq.LLMError):
            ltxq._llm_http(self.base, "/chat/completions", payload={}, timeout=5)


class LLMDirectiveTests(LLMBase):
    def test_library_and_user_override_wins(self):
        dirs = ltxq.llm_directives()
        self.assertEqual([d["name"] for d in dirs], ["dur-test", "ltx-default"])
        txt, meta, err = ltxq.llm_directive("ltx-default")
        self.assertIsNone(err)
        self.assertEqual(txt, "TEST DEFAULT DIRECTIVE — describe the transition.")
        self.assertFalse(meta["custom"])
        udir = TMP / "llm_directives"
        udir.mkdir()
        (udir / "ltx-default.txt").write_text("MY OVERRIDE")
        dirs = ltxq.llm_directives()
        self.assertTrue(next(d for d in dirs
                             if d["name"] == "ltx-default")["custom"])
        txt, meta, err = ltxq.llm_directive("ltx-default")
        self.assertEqual(txt, "MY OVERRIDE")
        txt, meta, err = ltxq.llm_directive("nope")
        self.assertIn("no directive named", err)

    def test_default_directive_follows_model_grid(self):
        self.assertEqual(ltxq.llm_default_directive("minimax-h3-7b"),
                         "minimax-h3-fl2va")
        self.assertEqual(ltxq.llm_default_directive("MiniMax-H3"),
                         "minimax-h3-fl2va")
        self.assertEqual(ltxq.llm_default_directive("wanh3x"),   # no boundary hit
                         "ltx-default")
        self.assertEqual(ltxq.llm_default_directive("ltx-2-dev"), "ltx-default")
        self.assertEqual(ltxq.llm_default_directive(None), "ltx-default")

    def test_placeholders(self):
        self.assertEqual(ltxq.llm_fill_placeholders("a {{DUR}} b",
                                                    {"DUR": "8.00"}), "a 8.00 b")
        self.assertEqual(ltxq.llm_fill_placeholders("no tokens", {}), "no tokens")
        with self.assertRaises(ltxq.LLMError):
            ltxq.llm_fill_placeholders("{{DUR}} {{FPS}}", {"FPS": 25})

    def test_directives_route_and_template_routes(self):
        r = self.client.get("/api/llm/directives")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual([d["name"] for d in j["directives"]],
                         ["dur-test", "ltx-default"])
        self.assertEqual(j["default"], "ltx-default")
        r = self.client.get("/api/llm/directives?model=minimax-h3-7b")
        self.assertEqual(r.get_json()["default"], "minimax-h3-fl2va")
        r = self.client.get("/api/llm/template?name=dur-test")
        self.assertEqual(r.get_json()["template"],
                         "aligns with the {{DUR}}-second mark")
        r = self.client.get("/api/llm/template?name=nope")
        self.assertEqual(r.status_code, 400)
        r = self.client.get("/api/llm/template")          # nameless = default
        self.assertEqual(r.get_json()["name"], "ltx-default")

    def test_template_put_creates_and_overrides(self):
        r = self.client.put("/api/llm/template",
                            json={"template": "  BETTER  ",
                                  "name": "ltx-default"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["custom"])
        self.assertEqual((TMP / "llm_directives" / "ltx-default.txt").read_text(),
                         "BETTER\n")
        r = self.client.get("/api/llm/template?name=ltx-default")
        self.assertEqual(r.get_json()["template"], "BETTER")
        self.assertTrue(r.get_json()["custom"])
        r = self.client.put("/api/llm/template",
                            json={"template": "X", "name": "my-own.v2"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue((TMP / "llm_directives" / "my-own.v2.txt").exists())
        r = self.client.put("/api/llm/template",
                            json={"template": "X", "name": "../evil"})
        self.assertEqual(r.status_code, 400)
        r = self.client.put("/api/llm/template",
                            json={"template": "   ", "name": "x"})
        self.assertEqual(r.status_code, 400)

    def test_endpoints_and_models_routes(self):
        r = self.client.get("/api/llm/endpoints")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["active"], "test-llm")
        r = self.client.get("/api/llm/models?endpoint=test-llm")
        self.assertEqual(r.status_code, 502)      # bogus base_url → unreachable
        self.assertIn("unreachable", r.get_json()["error"])
        r = self.client.get("/api/llm/models?endpoint=nope")
        self.assertEqual(r.status_code, 400)


class PairsSynthTests(LLMBase):
    def synth(self, sid, body):
        return self.client.post(f"/api/pairs/{sid}/synth", json=body)

    def test_single_pair_updates_manifest(self):
        payload = self.preview(stills_dir("llm_single"))
        sid = payload["sid"]
        seen = {}

        def fake_chat(base_url, model, messages, timeout=120, temperature=0.3):
            seen["base_url"], seen["model"] = base_url, model
            return "  A slow push-in across the dune.  "

        orig = ltxq.llm_chat
        ltxq.llm_chat = fake_chat
        try:
            r = self.synth(sid, {"index": 0, "endpoint": "test-llm",
                                 "model": "vl-1"})
        finally:
            ltxq.llm_chat = orig
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        p = r.get_json()["manifest"]["pairs"][0]
        self.assertEqual(p["prompt"], "A slow push-in across the dune.")
        self.assertEqual(p["prompt_src"], "llm")
        self.assertEqual(seen["model"], "vl-1")
        # persisted, not just in the response
        m = json.loads((server.STAGE / f"pairs_{sid}" / "manifest.json")
                       .read_text())
        self.assertEqual(m["pairs"][0]["prompt_src"], "llm")

    def test_single_pair_model_falls_back_to_endpoint_profile(self):
        payload = self.preview(stills_dir("llm_single_fb"))
        seen = {}

        def fake_chat(base_url, model, messages, timeout=120, temperature=0.3):
            seen["model"] = model
            return "x"

        orig = ltxq.llm_chat
        ltxq.llm_chat = fake_chat
        try:
            r = self.synth(payload["sid"], {"index": 1, "endpoint": "other-llm"})
        finally:
            ltxq.llm_chat = orig
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(seen["model"], "canned-vl")

    def test_single_pair_bad_inputs(self):
        payload = self.preview(stills_dir("llm_bad"))
        sid = payload["sid"]
        r = self.synth(sid, {"index": 0, "endpoint": "nope", "model": "m"})
        self.assertEqual(r.status_code, 400)
        r = self.synth(sid, {"index": 0, "endpoint": "test-llm"})   # no model
        self.assertEqual(r.status_code, 400)
        self.assertIn("no model chosen", r.get_json()["error"])
        r = self.synth(sid, {"index": 99, "endpoint": "test-llm", "model": "m"})
        self.assertEqual(r.status_code, 400)
        r = self.synth(sid, {"index": "x", "endpoint": "test-llm", "model": "m"})
        self.assertEqual(r.status_code, 400)
        r = self.synth("00000000", {})
        self.assertEqual(r.status_code, 404)
        r = self.client.get("/api/pairs/00000000/synth")
        self.assertEqual(r.status_code, 404)

    def test_llm_failure_surfaces_as_502_and_leaves_prompt(self):
        payload = self.preview(stills_dir("llm_fail"))
        sid = payload["sid"]
        before = json.loads((server.STAGE / f"pairs_{sid}" / "manifest.json")
                            .read_text())["pairs"][0]
        orig = ltxq.llm_chat
        ltxq.llm_chat = lambda *a, **k: (_ for _ in ()).throw(
            ltxq.LLMError("LLM unreachable at http://x"))
        try:
            r = self.synth(sid, {"index": 0, "endpoint": "test-llm",
                                 "model": "m"})
        finally:
            ltxq.llm_chat = orig
        self.assertEqual(r.status_code, 502)
        self.assertIn("unreachable", r.get_json()["error"])
        after = json.loads((server.STAGE / f"pairs_{sid}" / "manifest.json")
                           .read_text())["pairs"][0]
        self.assertEqual(after, before)

    def test_bulk_fills_skips_and_continues_past_errors(self):
        payload = self.preview(stills_dir("llm_bulk", numbers=(1, 2, 3, 4)))
        sid = payload["sid"]
        # pair 2 (index 1) carries typed text → must be skipped, not overwritten
        r = self.client.put(f"/api/pairs/{sid}/manifest", json={"pairs": [
            {"first": "image0001.png", "last": "image0002.png",
             "prompt": "", "prompt_src": "", "config": None, "audio": None,
             "frames_override": None},
            {"first": "image0002.png", "last": "image0003.png",
             "prompt": "my own words", "prompt_src": "inline",
             "config": None, "audio": None, "frames_override": None},
            {"first": "image0003.png", "last": "image0004.png",
             "prompt": "", "prompt_src": "", "config": None, "audio": None,
             "frames_override": None}]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        calls, outputs = [], ["first pair text", None, "third pair text"]

        def fake_chat(base_url, model, messages, timeout=120, temperature=0.3):
            calls.append(1)
            out = outputs[len(calls) - 1]
            if out is None:
                raise ltxq.LLMError("synthetic failure")
            return out

        orig = ltxq.llm_chat
        ltxq.llm_chat = fake_chat
        try:
            r = self.synth(sid, {"endpoint": "test-llm", "model": "m"})
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            self.assertEqual(r.get_json()["total"], 2)   # inline pair excluded
            deadline = time.time() + 10
            while time.time() < deadline:
                s = self.client.get(f"/api/pairs/{sid}/synth").get_json()
                if not s["running"]:
                    break
                time.sleep(0.02)
        finally:
            ltxq.llm_chat = orig
        self.assertEqual(len(calls), 2)
        self.assertEqual(s["errors"], [{"pair": 3,
                                        "error": "synthetic failure"}])
        m = json.loads((server.STAGE / f"pairs_{sid}" / "manifest.json")
                       .read_text())
        self.assertEqual(m["pairs"][0]["prompt"], "first pair text")
        self.assertEqual(m["pairs"][0]["prompt_src"], "llm")
        self.assertEqual(m["pairs"][1]["prompt"], "my own words")   # untouched
        self.assertEqual(m["pairs"][1]["prompt_src"], "inline")
        self.assertEqual(m["pairs"][2]["prompt"], "")               # left as-is
        self.assertNotIn("llm", {p["prompt_src"] for p in m["pairs"][1:]})

    def test_bulk_refuses_when_nothing_to_do_or_already_running(self):
        payload = self.preview(stills_dir("llm_empty"))
        sid = payload["sid"]
        # mark every pair as user-typed → nothing left for the LLM to fill
        r = self.client.put(f"/api/pairs/{sid}/manifest", json={"pairs": [
            {"first": p["first"], "last": p["last"], "prompt": "mine",
             "prompt_src": "inline", "config": None, "audio": None,
             "frames_override": None}
            for p in payload["manifest"]["pairs"]]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        r = self.synth(sid, {"endpoint": "test-llm", "model": "m"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("nothing to describe", r.get_json()["error"])
        server.SYNTH_RUN.update(running=True, sid="other")
        try:
            r = self.synth(sid, {"index": 0, "endpoint": "test-llm",
                                 "model": "m"})
            self.assertEqual(r.status_code, 409)
        finally:
            server.SYNTH_RUN.update(running=False, sid=None)

    def test_bulk_reports_pair_removed_midrun(self):
        payload = self.preview(stills_dir("llm_race", numbers=(1, 2, 3)))
        sid = payload["sid"]
        release = threading.Event()

        def fake_chat(base_url, model, messages, timeout=120, temperature=0.3):
            release.wait(5)                        # hold pair 1 mid-call
            return "late text"

        orig = ltxq.llm_chat
        ltxq.llm_chat = fake_chat
        try:
            r = self.synth(sid, {"endpoint": "test-llm", "model": "m"})
            self.assertEqual(r.get_json()["total"], 2)
            # user deletes pair 1 while it is being described
            m = json.loads((server.STAGE / f"pairs_{sid}" / "manifest.json")
                           .read_text())
            m["pairs"] = m["pairs"][1:]
            (server.STAGE / f"pairs_{sid}" / "manifest.json").write_text(
                json.dumps(m))
            release.set()
            deadline = time.time() + 10
            while time.time() < deadline:
                s = self.client.get(f"/api/pairs/{sid}/synth").get_json()
                if not s["running"]:
                    break
                time.sleep(0.02)
        finally:
            ltxq.llm_chat = orig
        self.assertEqual(s["skipped"],
                         [{"pair": 1, "reason": "pair removed"}])
        m = json.loads((server.STAGE / f"pairs_{sid}" / "manifest.json")
                       .read_text())
        self.assertEqual(len(m["pairs"]), 1)
        # the surviving pair was still in the worklist and gets described;
        # the deletion itself was not rolled back by the worker's write
        self.assertEqual(m["pairs"][0]["prompt"], "late text")
        self.assertEqual(m["pairs"][0]["prompt_src"], "llm")


    def test_single_pair_substitutes_directive_placeholders(self):
        payload = self.preview(stills_dir("llm_dur"))
        sid = payload["sid"]
        seen = {}

        def fake_chat(base_url, model, messages, timeout=120, temperature=0.3):
            seen["facts"] = messages[1]["content"][0]["text"]
            return "aligns with the {{DUR}}-second mark"

        orig = ltxq.llm_chat
        ltxq.llm_chat = fake_chat
        try:
            r = self.synth(sid, {"index": 0, "endpoint": "test-llm",
                                 "model": "m", "directive": "dur-test"})
        finally:
            ltxq.llm_chat = orig
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # 121 frames @ 25 fps -> DUR filled server-side, not by the model
        self.assertEqual(r.get_json()["manifest"]["pairs"][0]["prompt"],
                         "aligns with the 4.84-second mark")
        self.assertIn("seconds = 4.84", seen["facts"])
        m = json.loads((server.STAGE / f"pairs_{sid}" / "manifest.json")
                       .read_text())
        self.assertEqual(m.get("llm_directive"), "dur-test")

    def test_single_pair_unknown_directive_refused(self):
        payload = self.preview(stills_dir("llm_diroops"))
        r = self.synth(payload["sid"], {"index": 0, "endpoint": "test-llm",
                                        "model": "m", "directive": "nope"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("no directive named", r.get_json()["error"])

    def test_bulk_substitutes_placeholders(self):
        payload = self.preview(stills_dir("llm_bdur", numbers=(1, 2, 3)))
        sid = payload["sid"]
        orig = ltxq.llm_chat
        ltxq.llm_chat = lambda *a, **k: "the {{DUR}} shot"
        try:
            r = self.synth(sid, {"endpoint": "test-llm", "model": "m",
                                 "directive": "dur-test"})
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            self.assertEqual(r.get_json()["total"], 2)
            deadline = time.time() + 10
            while time.time() < deadline:
                s = self.client.get(f"/api/pairs/{sid}/synth").get_json()
                if not s["running"]:
                    break
                time.sleep(0.02)
        finally:
            ltxq.llm_chat = orig
        self.assertEqual(s["errors"], [])
        m = json.loads((server.STAGE / f"pairs_{sid}" / "manifest.json")
                       .read_text())
        self.assertEqual([p["prompt"] for p in m["pairs"]],
                         ["the 4.84 shot", "the 4.84 shot"])
        self.assertEqual(m.get("llm_directive"), "dur-test")


if __name__ == "__main__":
    unittest.main()
