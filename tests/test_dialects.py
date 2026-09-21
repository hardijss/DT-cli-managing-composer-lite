"""Dialect rulebook tests for ltxq.

Pins two things:

1. ``dtcustom`` CLI output is BYTE-IDENTICAL to the pre-dialect implementation.
   The goldens in ``goldens_dtcustom.json`` were generated from the pre-change
   source (git HEAD before the dialect work):

     LTXQ_REF_MODULE=/path/to/pre_change_ltxq.py \\
       ./venv/bin/python tests/test_dialects.py --dump > tests/goldens_dtcustom.json

   The committed goldens were produced exactly that way; the tests below only
   read them.

2. the per-host dialect resolver, the capability gate and the dialect-aware
   argument builder behave as specified.

Run:
  ./venv/bin/python -m unittest discover -s tests -t . -v
"""

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import ltxq  # noqa: E402

GOLDENS_PATH = Path(__file__).resolve().parent / "goldens_dtcustom.json"

C_BASE = {"download_missing": False, "disable_preview": True, "offline": False,
          "remote_root": "genwork", "cli_dialect": "dtcustom",
          "video_format": None}


def C(**over):
    c = dict(C_BASE)
    c.update(over)
    return c


def A(flag, file):
    return {"flag": flag, "file": file}


# gen_args(model, assets, extra, c, ext, models_dir, video_format)
ONESHOT_CASES = {
    "minimal": dict(model="ltx-2.3-dev", assets=[], extra=[], c=C(),
                    ext="mov", models_dir=None, video_format=None),
    "bool-flips": dict(model="ltx-2.3-dev", assets=[], extra=[],
                       c=C(download_missing=True, disable_preview=False,
                           offline=True),
                       ext="mov", models_dir=None, video_format=None),
    "models_dir+vf": dict(model="models/ltx-2.3-dev.ckpt", assets=[], extra=[],
                          c=C(), ext="mov", models_dir="~/Models DT",
                          video_format="prores4444"),
    "png-no-vf": dict(model="ltx-2.3-dev", assets=[], extra=[], c=C(),
                      ext="png", models_dir="~/Models", video_format=None),
    "vf-empty": dict(model="ltx-2.3-dev", assets=[], extra=[], c=C(),
                     ext="mp4", models_dir="~/Models", video_format=""),
    "five-assets": dict(
        model="ltx-2.3-dev",
        assets=[A("--image", "start frame.png"), A("--audio", "seg 1.wav"),
                A("--first-frame", "f0.png"), A("--last-frame", "f121.png"),
                A("--input-video", "in vid.mov"), A("--image", "quo'te.png")],
        extra=[], c=C(), ext="mov", models_dir="~/Models DT",
        video_format="hevc"),
    "extras": dict(
        model="ltx-2.3-dev", assets=[A("--image", "a.png")],
        extra=["--seed", "12345", "--frames", "121", "--keyframe",
               "kf.png:12:0.8", "--keyframe-strength", "0.5", "@~/abs.png@",
               "--prompt-relay", "x y"],
        c=C(), ext="mov", models_dir="~/Models", video_format="hevc"),
    "extras-only": dict(model="m.ckpt", assets=[], extra=["--fflf-preflight"],
                        c=C(), ext="mov", models_dir=None, video_format=None),
}


def _h(**over):
    h = {"alias": "render", "home": "/Users/render",
         "models_dir": "/Users/render/Models DT", "video_format": None}
    h.update(over)
    return h


SERVE_EXTRA = ["--seed", "7", "--keyframe", "@kf.png@:12:0.8", "~/abs.png",
               "--frames", "121"]
SERVE_ASSETS = [A("--image", "a b.png"), A("--audio", "x.wav")]

# The DB stores these as JSON strings; serve_args reads them the same way.
SERVE_EXTRA_J = json.dumps(SERVE_EXTRA)
SERVE_ASSETS_J = json.dumps(SERVE_ASSETS)
NONE_J = "[]"

# serve_args(job, h, c) -> (rd, args, err)
SERVE_CASES = {
    "full": (
        {"id": "abc123", "model": "/Users/render/models/ltx-2.3-dev.ckpt",
         "ext": "mov", "assets": SERVE_ASSETS_J, "extra_args": SERVE_EXTRA_J},
        _h(), C(video_format="hevc")),
    "no-models-dir": (
        {"id": "abc123", "model": "/Users/render/models/ltx-2.3-dev.ckpt",
         "ext": "mp4", "assets": SERVE_ASSETS_J, "extra_args": SERVE_EXTRA_J},
        _h(models_dir=None), C(offline=True, video_format="hevc")),
    "no-vf": (
        {"id": "abc123", "model": "/Users/render/models/ltx-2.3-dev.ckpt",
         "ext": "mov", "assets": NONE_J, "extra_args": NONE_J},
        _h(), C()),
    "png": (
        {"id": "abc123", "model": "/Users/render/models/ltx-2.3-dev.ckpt",
         "ext": "png", "assets": SERVE_ASSETS_J, "extra_args": SERVE_EXTRA_J},
        _h(alias="vol", models_dir="/vol/Models"), C(video_format="hevc")),
    "non-absolute": (
        {"id": "bad1", "model": "relative/model.ckpt",
         "ext": "mov", "assets": NONE_J, "extra_args": NONE_J},
        _h(home="", models_dir=None), C()),
}


def _load_ref():
    """The module used for --dump: LTXQ_REF_MODULE when set, else ltxq."""
    path = os.environ.get("LTXQ_REF_MODULE")
    if not path:
        return ltxq
    spec = importlib.util.spec_from_file_location("ltxq_ref", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def dump(mod):
    out = {"oneshot": {}, "serve": {}}
    for name, k in ONESHOT_CASES.items():
        out["oneshot"][name] = mod.gen_args(
            k["model"], k["assets"], k["extra"], k["c"], k["ext"],
            k["models_dir"], k["video_format"])
    for name, (job, h, c) in SERVE_CASES.items():
        rd, args, err = mod.serve_args(job, h, c)
        out["serve"][name] = {"args": args, "err": err, "rd": rd}
    return out


if __name__ == "__main__" and "--dump" in sys.argv:
    json.dump(dump(_load_ref()), sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
    sys.exit(0)


class DtcustomParityTests(unittest.TestCase):
    """The dialect refactor must not move a single byte for dtcustom hosts."""

    @classmethod
    def setUpClass(cls):
        cls.gold = json.loads(GOLDENS_PATH.read_text())

    def test_oneshot_byte_identical(self):
        for name, k in ONESHOT_CASES.items():
            got = ltxq.gen_args(k["model"], k["assets"], k["extra"], k["c"],
                                k["ext"], k["models_dir"], k["video_format"])
            self.assertEqual(got, self.gold["oneshot"][name],
                             f"oneshot case {name!r}")

    def test_serve_argv_identical(self):
        for name, (job, h, c) in SERVE_CASES.items():
            rd, args, err = ltxq.serve_args(job, h, c)
            want = self.gold["serve"][name]
            self.assertEqual(rd, want["rd"], f"serve rd {name!r}")
            self.assertEqual(args, want["args"], f"serve args {name!r}")
            self.assertEqual(err, want["err"], f"serve err {name!r}")


class DialectResolverTests(unittest.TestCase):
    def test_per_host_beats_global_beats_default(self):
        self.assertEqual(ltxq.dialect_of({"cli_dialect": "dtcustom"}, None),
                         "dtcustom")
        self.assertEqual(
            ltxq.dialect_of({"cli_dialect": "dtcustom"},
                            {"cli_dialect": "dtofficial"}), "dtofficial")
        self.assertEqual(ltxq.dialect_of({"cli_dialect": "dtofficial"},
                                         {"cli_dialect": "dtcustom"}),
                         "dtcustom")
        self.assertEqual(ltxq.dialect_of({}, {"alias": "h1"}),
                         ltxq.DEFAULT_DIALECT)
        # empty string counts as "not set"
        self.assertEqual(ltxq.dialect_of({"cli_dialect": "dtofficial"},
                                         {"cli_dialect": ""}), "dtofficial")

    def test_unknown_dialect_fails_loudly(self):
        with self.assertRaises(ltxq.UnknownDialect):
            ltxq.dialect_of({"cli_dialect": "nope"}, None)
        with self.assertRaises(ltxq.UnknownDialect):
            ltxq.dialect_of({}, {"alias": "h1", "cli_dialect": "nope"})


class CapabilityGateTests(unittest.TestCase):
    def _job(self, **over):
        j = {"id": "j1", "backend": None, "assets": "[]", "extra_args": "[]"}
        j.update(over)
        return j

    def test_plain_job_runs_everywhere(self):
        caps = ltxq.job_caps(self._job(), "oneshot")
        self.assertTrue(caps <= {"image", "audio"})
        for d in ltxq.DIALECTS:
            self.assertEqual(ltxq.missing_caps(ltxq.DIALECTS[d], caps), [])

    def test_first_last_frame_gated_by_fflf_and_assets(self):
        j = self._job(assets=json.dumps([A("--first-frame", "f0.png"),
                                         A("--last-frame", "f121.png")]))
        caps = ltxq.job_caps(j, "oneshot")
        self.assertEqual(caps, {"first_frame", "last_frame", "fflf_preflight"})
        self.assertEqual(ltxq.missing_caps(ltxq.DIALECTS["dtcustom"], caps), [])
        self.assertEqual(ltxq.missing_caps(ltxq.DIALECTS["dtofficial"], caps),
                         ["fflf_preflight", "first_frame", "last_frame"])

    def test_serve_backend_needs_serve(self):
        j = self._job(backend="serve")
        caps = ltxq.job_caps(j, None)
        self.assertEqual(caps, {"serve"})
        self.assertEqual(ltxq.missing_caps(ltxq.DIALECTS["dtcustom"], caps), [])
        self.assertEqual(ltxq.missing_caps(ltxq.DIALECTS["dtofficial"], caps),
                         ["serve"])

    def test_host_default_backend_counts(self):
        # a job with no backend of its own inherits the host's default
        self.assertEqual(ltxq.job_caps(self._job(), "serve"), {"serve"})
        self.assertEqual(ltxq.job_caps(self._job(backend="oneshot"), "serve"),
                         set())

    def test_keyframe_extra_arg_needs_keyframe(self):
        j = self._job(extra_args=json.dumps(
            ["--keyframe", "@kf.png@:12:0.8", "--keyframe-strength", "0.5"]))
        caps = ltxq.job_caps(j, "oneshot")
        self.assertIn("keyframe", caps)
        self.assertEqual(ltxq.missing_caps(ltxq.DIALECTS["dtofficial"], caps),
                         ["keyframe"])
        self.assertEqual(ltxq.missing_caps(ltxq.DIALECTS["dtcustom"], caps), [])

    def test_unknown_extra_flag_is_not_gated(self):
        # the escape hatch stays open: an unknown flag is the engine's business
        j = self._job(extra_args=json.dumps(["--nag-scale", "0.5"]))
        self.assertEqual(ltxq.job_caps(j, "oneshot"), set())

    def test_build_argv_rejects_unsupported_flag(self):
        with self.assertRaises(ltxq.UnsupportedFlag):
            ltxq.build_argv("m.ckpt", [A("--first-frame", "f.png")], [], C(),
                            "mov", dialect="dtofficial")
        # same job is fine on the dialect that knows the flag
        toks, _ = ltxq.build_argv("m.ckpt", [A("--first-frame", "f.png")], [],
                                  C(), "mov", dialect="dtcustom")
        self.assertIn("--first-frame", toks)


class RulebookTests(unittest.TestCase):
    def test_both_dialects_declare_the_owned_flags(self):
        owned = ("model", "config_file", "prompt_file", "output", "models_dir",
                 "video_format", "no_download_missing", "disable_preview",
                 "offline")
        for name, spec in ltxq.DIALECTS.items():
            for sem in owned:
                self.assertIn(sem, spec["flags"], f"{name} lacks {sem}")
            self.assertEqual(spec["generate_cmd"], "generate")

    def test_dtofficial_lacks_ltx_surface(self):
        flags = ltxq.DIALECTS["dtofficial"]["flags"]
        for sem in ("first_frame", "middle_frame", "last_frame", "input_video",
                    "keyframe"):
            self.assertNotIn(sem, flags)
        self.assertFalse(ltxq.DIALECTS["dtofficial"]["serve"])
        self.assertFalse(ltxq.DIALECTS["dtofficial"]["fflf_preflight"])
        self.assertTrue(ltxq.DIALECTS["dtcustom"]["serve"])
        self.assertTrue(ltxq.DIALECTS["dtcustom"]["fflf_preflight"])

    def test_make_runner_uses_dialect_subcommand(self):
        src = ltxq.make_runner("m.ckpt", "~/dt-cli", False, [], [], C(), "mov",
                               dialect="dtofficial")
        self.assertIn("$HOME/dt-cli generate --model m.ckpt", src)


if __name__ == "__main__":
    unittest.main()
