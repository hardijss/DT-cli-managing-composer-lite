#!/usr/bin/env python3
"""ltxq v1.4 — submit/poll/collect draw-things-cli generations over SSH.
One-shot runner (default) and serve (warm worker) backends. Mac/Linux hosts.
Deps: python3 + pyyaml + system ssh/rsync/tar.
"""
import argparse, gc, hashlib, importlib.metadata, json, os, random, re, shlex, shutil, sqlite3, subprocess, sys, time, uuid
from pathlib import Path
import yaml

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "ltxq.db"

# Config search order: $LTXQ_CONF, then the repo checkout (existing CLI
# setups keep working unchanged), then the per-user macOS location the app
# manages. When nothing exists yet, hosts.yaml.example is seeded into the
# per-user location so a fresh install has something to edit.
_APP_SUPPORT = (Path.home() / "Library" / "Application Support" / "Ltxq"
                if sys.platform == "darwin" else None)

def _resolve_conf_path() -> Path:
    env = os.environ.get("LTXQ_CONF")
    if env:
        return Path(env).expanduser()
    candidates = [HERE / "hosts.yaml"]
    if _APP_SUPPORT:
        candidates.append(_APP_SUPPORT / "hosts.yaml")
    for c in candidates:
        if c.exists():
            return c
    example = HERE / "hosts.yaml.example"
    if _APP_SUPPORT and example.exists():
        try:
            _APP_SUPPORT.mkdir(parents=True, exist_ok=True)
            shutil.copy(example, candidates[1])
            return candidates[1]
        except OSError:
            pass
    return candidates[0]

CONF_PATH = _resolve_conf_path()

_conf_mtime = None

def conf_changed() -> bool:
    """True when hosts.yaml changed on disk since the last call (first call
    only baselines the mtime). Engine loops poll this between dispatches."""
    global _conf_mtime
    try:
        m = CONF_PATH.stat().st_mtime
    except OSError:
        m = None
    changed = _conf_mtime is not None and m != _conf_mtime
    _conf_mtime = m
    return changed
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15"]
MUX_DIR = HERE / ".ssh_mux"; MUX_DIR.mkdir(exist_ok=True)
MUX_OPTS = ["-o", "ControlMaster=auto",
            "-o", "ControlPath=" + str(MUX_DIR / "cm-%r@%h-%p"),
            "-o", "ControlPersist=10m"]

PCT   = re.compile(r"(\d{1,3})\s*%")
WROTE = re.compile(r"^Wrote:\s*(\S+)", re.M)
TOTAL = re.compile(r"Total generation time[^:]*:\s*([\d.]+)")
STEPS = re.compile(r"\((\d+) step\(s\)\): avg ([\d.]+) s, median ([\d.]+) s")
EVT   = re.compile(r"DT_WORKER_EVENT\s*(\{.*\})")
GENP  = re.compile(r"Generated\s*\[[^\]]*\]\s*(\d{1,3})\s*%")
MDIR_HDR = re.compile(r"^Models directory:\s*(.+)$", re.M)
ROW = re.compile(r"^(\S+\.ckpt)\s{2,}(.+?)\s{2,}(official|community)\s+(yes|no)(?:\s+(\S+))?\s*$", re.M)
SHUTDOWN = shlex.quote('{"command":"shutdown"}')
# `generate --help` option gutter: "  --model <model>" / "  -m, --model <model>" /
# "  --download-missing/--no-download-missing". Prose wrapped to the same gutter
# can false-positive, which is harmless for a drift check (one extra snapshot line).
GENOPT = re.compile(r"^\s{2,8}(?:-[a-zA-Z],\s)?(--[A-Za-z0-9-/]+)", re.M)

# ------------------------------------------------------------------- dialects
# Engine-CLI "rulebooks": which binary a host runs and how to speak to it. This
# is reviewed DATA, not a plugin framework — no runtime capability probing (a
# `--help` call must never decide what ltxq emits), no behavior hidden in code.
#
#   label     human name for notes/docs/UI
#   serve     does the binary have the `serve` warm-worker subcommand?
#   fflf_preflight  does it accept `generate ... --fflf-preflight`?
#   strict_frames  does it REJECT a --frames count off the model's grid
#                  (LTX/WAN 8n+1, MiniMax H3 17n+5) instead of rounding up?
#                  Documented fact, not enforced yet — see docs/cli-dialects.md
#   flags     semantic name -> CLI spelling, for EVERY flag the scheduler itself
#             emits. A semantic that is ABSENT is unsupported in that dialect:
#             ltxq must route the job to a host whose dialect has it, or refuse
#             it loudly — never emit a flag the binary does not know.
#             (Asset semantics double as capability names; `image`/`audio`/...
#             are the keys the gate looks up. Raw `extra_arg` tokens and
#             `--keyframe*` are covered by the `keyframe` entry.)
# See docs/cli-dialects.md; drift snapshots are per dialect
# (docs/generate_flags.<dialect>.txt, `ltxq flags`).
# Human guidance appended to refusal / unroutable notes when a capability is
# missing. Rulebook data (see `cap_hints` in DIALECTS below) so the messaging
# lives next to the dialect that owns the limitation.
_DT_OFFICIAL_FRAMES = (
    "dtofficial has no first/middle/last frames: its first --image is the canvas "
    "image and every later --image is a moodboard reference (not an endpoint) — "
    "attach the image to the Image slot (or extra images) instead, or send this "
    "job to a dtcustom host")
_DT_OFFICIAL_KEYFRAME = (
    "dtofficial has no --keyframe (path:index:strength) — it would degrade to a "
    "moodboard reference, losing the frame index and strength")

DIALECTS = {
    "dtcustom": {
        "label": "tod-dt-cli (DrawOtherThings CustomCLI)",
        "generate_cmd": "generate",
        "serve": True,
        "fflf_preflight": True,
        # a --frames count off the model's grid is NOT rejected (rounds up)
        "strict_frames": False,
        "flags": {
            "model": "--model",
            "config_file": "--config-file",
            "prompt_file": "--prompt-file",
            "output": "--output",
            "models_dir": "--models-dir",
            "video_format": "--video-format",
            "no_download_missing": "--no-download-missing",
            "disable_preview": "--disable-preview",
            "offline": "--offline",
            "seed": "--seed",
            "frames": "--frames",
            "image": "--image",
            "audio": "--audio",
            "first_frame": "--first-frame",
            "middle_frame": "--middle-frame",
            "last_frame": "--last-frame",
            "input_video": "--input-video",
            "keyframe": "--keyframe",
        },
    },
    "dtofficial": {
        "label": "draw-things-cli (upstream)",
        "generate_cmd": "generate",
        "serve": False,
        "fflf_preflight": False,
        # --frames off the model's grid is a hard usage error (exit 64)
        "strict_frames": True,
        "cap_hints": {
            "first_frame": _DT_OFFICIAL_FRAMES,
            "middle_frame": _DT_OFFICIAL_FRAMES,
            "last_frame": _DT_OFFICIAL_FRAMES,
            "keyframe": _DT_OFFICIAL_KEYFRAME,
        },
        # Shares most of the `generate` surface with dtcustom, but carries no
        # LTX asset flags (--first/middle/last-frame, --input-video, --keyframe*)
        # and no --fflf-preflight; its subcommands are generate/auth/models/
        # train/completion (no serve).
        "flags": {
            "model": "--model",
            "config_file": "--config-file",
            "prompt_file": "--prompt-file",
            "output": "--output",
            "models_dir": "--models-dir",
            "video_format": "--video-format",
            "no_download_missing": "--no-download-missing",
            "disable_preview": "--disable-preview",
            "offline": "--offline",
            "seed": "--seed",
            "frames": "--frames",
            "image": "--image",
            "audio": "--audio",
        },
    },
}
DEFAULT_DIALECT = "dtcustom"

# The six upload slots (semantic -> flag) in the DEFAULT dialect; the UI/API
# form fields, `--chain` slots and regen all speak these semantic names.
ASSET_SEMANTICS = ("image", "audio", "first_frame", "middle_frame", "last_frame",
                   "input_video")
FLAGMAP = tuple((s, DIALECTS[DEFAULT_DIALECT]["flags"][s]) for s in ASSET_SEMANTICS)
# Semantics whose value is a filesystem path: the serve backend requires them
# absolute (it hands the argv straight to a worker without a job-dir cwd).
PATH_SEMANTICS = ("config_file", "prompt_file", "output", "image", "audio",
                  "first_frame", "middle_frame", "last_frame", "input_video",
                  "models_dir")
FFLF_FLAGS = ("--first-frame", "--middle-frame", "--last-frame")


class UnknownDialect(ValueError):
    """hosts.yaml names a cli_dialect that DIALECTS does not define."""


class UnsupportedFlag(ValueError):
    """A dialect has no spelling for a flag the scheduler wanted to emit."""

    def __init__(self, dialect, semantic):
        self.dialect, self.semantic = dialect, semantic
        super().__init__(f"dialect {dialect!r} has no flag for {semantic!r}")

PROBE_SH = '''
echo "HOME=$HOME"; echo "UNAME=$(uname -s)"
test -x {cli} && echo "CLI=ok" || echo "CLI=missing"
echo "EV=$DRAWTHINGS_MODELS_DIR"
cand() {{
  [ -d "$1" ] || return 0
  k=$(ls -1 "$1"/*.ckpt 2>/dev/null | wc -l | tr -d " ")
  echo "CAND|$2|$1|$k"
}}
[ -n "$DRAWTHINGS_MODELS_DIR" ] && cand "$DRAWTHINGS_MODELS_DIR" env
P="$HOME/Library/Containers/com.liuliu.draw-things/Data/Library/Preferences/com.liuliu.draw-things.plist"
if [ -f "$P" ]; then
  plutil -convert json -o - "$P" 2>/dev/null \\
    | grep -oiE '"/(volumes|users)/[^"]+"' | tr -d '"' | sort -u | head -10 \\
    | while IFS= read -r s; do
        [ -d "$s" ] || continue
        k=$(ls -1 "$s"/*.ckpt 2>/dev/null | wc -l | tr -d " ")
        echo "CAND|prefext|$s|$k"
      done
fi
cand "$HOME/Library/Containers/com.liuliu.draw-things/Data/Documents/Models" container
cand "$HOME/Documents/Models" documents
cand "$(CDPATH= cd -- "$(dirname -- {cli})" 2>/dev/null && pwd)/Models" bindir
'''
ORDER = ("env", "prefext", "container", "documents", "bindir")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, created_at INT, name TEXT, parent_id TEXT,
  host TEXT, model TEXT, prompt TEXT, config_text TEXT,
  status TEXT, remote_dir TEXT, remote_wrote TEXT, local_dir TEXT, local_out TEXT,
  pct INT DEFAULT 0, pct_ts INT, started_at INT, finished_at INT, exit_code INT,
  total_gen_s REAL, steps INT, step_avg_s REAL, step_median_s REAL,
  assets TEXT, extra_args TEXT, num_frames INT, fps REAL, ext TEXT,
  backend TEXT, cur_off INT, log_tail TEXT, note TEXT, chain TEXT, batch TEXT);
CREATE TABLE IF NOT EXISTS hosts(
  alias TEXT PRIMARY KEY, max_jobs INT DEFAULT 1, enabled INT DEFAULT 1,
  home TEXT, models_dir TEXT, uname TEXT, backend TEXT,
  dest TEXT, ssh_opts TEXT, mux INT DEFAULT 1);
CREATE TABLE IF NOT EXISTS registry(
  host TEXT, model TEXT, name TEXT, source TEXT, downloaded INT, hf TEXT, seen_at INT,
  PRIMARY KEY(host, model));
CREATE TABLE IF NOT EXISTS merge_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT, pos INT NOT NULL,
  jid TEXT NOT NULL, added_at INT);
"""
MIGRATIONS = ("ALTER TABLE jobs ADD COLUMN assets TEXT",
              "ALTER TABLE jobs ADD COLUMN extra_args TEXT",
              "ALTER TABLE jobs ADD COLUMN num_frames INT",
              "ALTER TABLE jobs ADD COLUMN ext TEXT",
              "ALTER TABLE jobs ADD COLUMN backend TEXT",
              "ALTER TABLE jobs ADD COLUMN cur_off INT",
              "ALTER TABLE jobs ADD COLUMN fps REAL",
              "ALTER TABLE hosts ADD COLUMN models_dir TEXT",
              "ALTER TABLE hosts ADD COLUMN uname TEXT",
              "ALTER TABLE hosts ADD COLUMN backend TEXT",
              "ALTER TABLE hosts ADD COLUMN dest TEXT",
              "ALTER TABLE hosts ADD COLUMN ssh_opts TEXT",
              "ALTER TABLE hosts ADD COLUMN conn_type TEXT DEFAULT 'ssh'",
              "ALTER TABLE hosts ADD COLUMN cli_path TEXT",
              "ALTER TABLE hosts ADD COLUMN cli_dialect TEXT",
              "ALTER TABLE hosts ADD COLUMN mux INT DEFAULT 1",
              "ALTER TABLE hosts ADD COLUMN video_format TEXT",
              "ALTER TABLE jobs ADD COLUMN chain TEXT",
              "ALTER TABLE jobs ADD COLUMN batch TEXT")

RUNNER = """#!/bin/sh
# generated by ltxq v1.4
cd "$(dirname "$0")" || exit 97
USE_PTY={pty}
echo $$ > pid
date +%s > started_at
run_cli() {{
  if [ "$USE_PTY" = "1" ]; then
    case "$(uname -s)" in
      Darwin) script -q /dev/null {cli} {generate} {args} ;;
      *)      script -qec "{cli} {generate} {args}" /dev/null ;;
    esac
  else
    {cli} {generate} {args}
  fi
}}
run_cli > log.txt 2>&1 &
echo $! > cli_pid
wait $!
echo $? > exit_code
date +%s > finished_at
"""

# Section markers are always put on a line of their own (printf '\n---X\n').
# The engine's log can end WITHOUT a trailing newline (progress lines use \r),
# and a bare `echo ---A` then glued itself to the log's last line — the marker
# was never seen, `alive` defaulted to False, and a still-running job was marked
# failed with "process gone, no exit_code".
POLL_CMD = ('cd {rd} 2>/dev/null || {{ echo NODIR; exit 0; }}; '
            'printf "\n---E\n"; cat exit_code 2>/dev/null; '
            'printf "\n---P\n"; cat cli_pid 2>/dev/null; '
            'printf "\n---L\n"; tail -c 4000 log.txt 2>/dev/null; '
            'printf "\n---A\n"; '
            'kill -0 "$(cat cli_pid 2>/dev/null)" 2>/dev/null && echo alive || echo dead')

POLL_MARKERS = "EPLA"


def parse_poll(out):
    """Split a POLL_CMD reply into (exit_code_line, log, alive).

    Sections are marked by a line that is exactly '---E'/'---P'/'---L'/'---A'.
    Parse strictly first; if a section is missing, fall back to a lenient split
    on the markers wherever they appear. That fallback is what rescues replies
    where a marker was glued to a log line lacking a trailing newline."""
    parts, cur = {}, None
    for line in out.splitlines():
        if len(line) == 4 and line.startswith("---") and line[3] in POLL_MARKERS:
            cur = line[3]
            parts[cur] = []
        elif cur is not None:
            parts[cur].append(line)
    if not {"A", "L"} <= set(parts):
        parts, cur = {}, None
        for i, chunk in enumerate(re.split(r"---([EPLA])(?:\r?\n|$)", out)):
            if i % 2:
                cur = chunk
                parts.setdefault(cur, [])
            elif cur is not None:
                parts[cur].append(chunk)
    return ("\n".join(parts.get("E", [])).strip(),
            "\n".join(parts.get("L", [])),
            "\n".join(parts.get("A", [])).strip() == "alive")

HOSTDEST = {}                                   # alias -> (dest, extra_opts, mux)

def conf():
    if not CONF_PATH.exists():
        print(f"ERROR: hosts.yaml not found at {CONF_PATH}\n"
              f"  copy the example and edit it (search order: $LTXQ_CONF, "
              f"{HERE / 'hosts.yaml'}, {_APP_SUPPORT / 'hosts.yaml' if _APP_SUPPORT else '(none)'})",
              file=sys.stderr)
        raise SystemExit(1)
    c = yaml.safe_load(CONF_PATH.read_text())
    for k, v in dict(cli_path="~/tod-dt-cli", poll_secs=10, stall_secs=900,
                     remote_root="genwork", movies_dir="~/Movies/generations",
                     use_pty=True, keep_remote=False, download_missing=False,
                     disable_preview=True, offline=False, video_format="hevc",
                     cli_dialect=DEFAULT_DIALECT, hosts=[]).items():
        c.setdefault(k, v)
    return c

_DB_INITED = False

def db():
    """New sqlite connection. In threads an unclosed connection keeps its fds
    until a GC pass, so every caller in a long-lived process must close() it."""
    global _DB_INITED
    con = sqlite3.connect(DB_PATH, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    if not _DB_INITED:                    # schema + migrations once per process
        con.executescript(SCHEMA)
        for ddl in MIGRATIONS:
            try: con.execute(ddl)
            except sqlite3.OperationalError: pass
        con.commit()
        _DB_INITED = True
    return con

def sync_hosts(con):
    c = conf()
    hosts_cfg = c["hosts"]
    active_aliases = []
    for h in hosts_cfg:
        alias = h["alias"]
        dialect_of(c, h)                     # fail loudly on an unknown cli_dialect
        active_aliases.append(alias)
        enabled = 0 if h.get("enabled") is False else 1
        con.execute("INSERT OR IGNORE INTO hosts(alias) VALUES(?)", (alias,))
        # cli_dialect follows cli_path: hosts.yaml is authoritative, absence
        # means NULL, and NULL resolves to DEFAULT_DIALECT at read time.
        con.execute("UPDATE hosts SET max_jobs=?, models_dir=COALESCE(?, models_dir), "
                    "dest=?, ssh_opts=?, mux=?, conn_type=?, cli_path=?, "
                    "cli_dialect=?, "
                    "video_format=COALESCE(?, video_format), enabled=? WHERE alias=?",
                    (h.get("max_jobs", 1), h.get("models_dir"),
                     h.get("dest") or alias, json.dumps(h.get("ssh_opts", [])),
                     0 if h.get("mux") is False else 1,
                     h.get("conn_type", "ssh"), h.get("cli_path"),
                     h.get("cli_dialect"),
                     h.get("video_format"), enabled, alias))
    if active_aliases:
        placeholders = ",".join("?" * len(active_aliases))
        con.execute(f"UPDATE hosts SET enabled=0 WHERE alias NOT IN ({placeholders})", active_aliases)
    con.commit(); load_dests(con)

def clean_tmp(max_age_s=86400):
    """Prune temporary files and staged keyframe extracts older than max_age_s."""
    tmp = HERE / "jobs" / "_tmp"
    if not tmp.is_dir():
        return
    now = time.time()
    for p in tmp.iterdir():
        if p.is_file():
            try:
                if now - p.stat().st_mtime > max_age_s:
                    p.unlink(missing_ok=True)
            except OSError:
                pass

def rotate_worker_logs(c, con, max_bytes=10 * 1024 * 1024):
    """Rotate remote worker.log on enabled hosts if size exceeds max_bytes and host is idle."""
    for h in con.execute("SELECT * FROM hosts WHERE enabled=1").fetchall():
        if not h["home"]:
            continue
        busy = con.execute("SELECT COUNT(*) FROM jobs WHERE host=? AND status IN "
                           "('uploading','running','collecting','suspect','cancelling')",
                           (h["alias"],)).fetchone()[0]
        if busy > 0:
            continue
        w = shlex.quote(wdir(h))
        cmd = (f"if [ -f {w}/worker.log ]; then "
               f"sz=$(wc -c < {w}/worker.log 2>/dev/null || echo 0); "
               f"if [ \"$sz\" -gt {max_bytes} ]; then "
               f"cp {w}/worker.log {w}/worker.log.prev 2>/dev/null && : > {w}/worker.log; "
               f"echo ROTATED; fi; fi")
        try:
            r = ssh(h["alias"], cmd, timeout=15)
            if "ROTATED" in r.stdout:
                print(f"{h['alias']}: rotated worker.log (> {max_bytes // (1024*1024)}MB)")
        except Exception:
            pass

def load_dests(con):
    HOSTDEST.clear()
    for r in con.execute("SELECT alias, dest, ssh_opts, mux, conn_type FROM hosts"):
        HOSTDEST[r["alias"]] = (r["dest"] or r["alias"],
                                json.loads(r["ssh_opts"] or "[]"), bool(r["mux"]),
                                r["conn_type"] or "ssh")

def dest_of(alias):
    d, extra, mux, _ = HOSTDEST.get(alias, (alias, [], True, "ssh"))
    return d, [os.path.expanduser(x) if x.startswith("~") else x for x in extra], mux

def ssh_eopts(alias):
    _, extra, mux = dest_of(alias)
    return list(SSH_OPTS) + (list(MUX_OPTS) if mux else []) + list(extra)

def is_local(alias):
    """conn_type from the in-memory host cache; falls back to a db lookup
    (closed immediately) only when the alias isn't cached yet."""
    if alias in HOSTDEST:
        return HOSTDEST[alias][3] == "local"
    con = db()
    try:
        r = con.execute("SELECT conn_type FROM hosts WHERE alias=?", (alias,)).fetchone()
        return bool(r) and (r["conn_type"] or "ssh") == "local"
    finally:
        con.close()


def run_cmd(alias, cmd, timeout=60):
    """Run a shell command on the host: locally via zsh, else over ssh.
    Both paths return CompletedProcess with the same shape."""
    if is_local(alias):
        return subprocess.run(["/bin/zsh", "-c", cmd],
                              capture_output=True, text=True, timeout=timeout)
    d, _, _ = dest_of(alias)
    return subprocess.run(["ssh", *ssh_eopts(alias), d, cmd],
                          capture_output=True, text=True, timeout=timeout)


def ssh(alias, cmd, timeout=60):
    return run_cmd(alias, cmd, timeout)

# Callables(con, jid, kw) invoked after each committed set_job. Additive hook
# for the ui's SSE event stream (server.py registers a watcher); the CLI
# registers none and behavior is unchanged.
JOB_WATCHERS = []

def set_job(con, jid, **kw):
    if kw:
        con.execute(f"UPDATE jobs SET {','.join(k + '=?' for k in kw)} WHERE id=?",
                    list(kw.values()) + [jid])
        con.commit()
        for w in list(JOB_WATCHERS):
            try: w(con, jid, kw)
            except Exception as e: print("job watcher error:", repr(e))

def dollar_home(p):
    return "$HOME/" + p[2:] if p.startswith("~/") else p

def wdir(h):
    return f"{h['home']}/genwork/worker"

KFREF = re.compile(r"@([^@\s]+)@")

def resolve_extra(extra, rd=None):
    """Rewrite @file@ tokens referencing uploaded assets to their remote path:
    serve backend gets the absolute job-dir path, oneshot the bare name
    (runner.sh cds into the job dir)."""
    out = []
    for t in extra:
        out.append(KFREF.sub(lambda m: (f"{rd}/{m.group(1)}" if rd else m.group(1)), t))
    return out

def build_argv(model, assets, extra, c, ext, *, dialect=DEFAULT_DIALECT,
               models_dir=None, video_format=None, rd=None, home=None):
    """Dialect-aware `generate` tokens (binary and subcommand excluded).

    Returns ``(tokens, extra_start)``: ``tokens[:extra_start]`` are the fixed
    tokens (model/config/output/prompt, models-dir, video-format, the three
    booleans, then the asset flags); ``tokens[extra_start:]`` are the resolved
    ``extra_arg`` tokens. The split lets :func:`gen_args` reproduce the
    historical oneshot byte shape, where an empty extras list still leaves one
    trailing space.

    ``rd is None`` -> oneshot shape: values are job-dir-relative and ``$HOME``
    is preserved (runner.sh cds into the job dir). ``rd`` set -> serve shape:
    values are resolved against the job dir and ``$HOME`` is expanded to
    ``home``. NB the two backends have always emitted the config/output/prompt
    block in different orders; that historical quirk is preserved byte for byte.

    Raises :class:`UnsupportedFlag` when the dialect has no spelling for a flag
    the scheduler must emit.
    """
    spec = dialect_spec(dialect)

    def flag(semantic):
        return spec_flag(spec, dialect, semantic)

    def path(p):
        p = dollar_home(p)
        if rd is None:
            return p
        return p.replace("$HOME", home, 1) if home else p

    def jobfile(name):
        return name if rd is None else f"{rd}/{name}"

    ext = ext or "mov"
    if rd is None:                      # oneshot: model, cfg, out, prompt
        order = (("model", path(model)), ("config_file", "config.json"),
                 ("output", f"out.{ext}"), ("prompt_file", "prompt.txt"))
    else:                               # serve: model, cfg, prompt, out
        order = (("model", path(model)), ("config_file", "config.json"),
                 ("prompt_file", "prompt.txt"), ("output", f"out.{ext}"))
    toks = []
    for sem, val in order:
        toks += [flag(sem), val if sem == "model" else jobfile(val)]
    if models_dir:
        toks += [flag("models_dir"), path(models_dir)]
    if video_format and ext in ("mov", "mp4"):   # image output: flag meaningless
        toks += [flag("video_format"), video_format]
    if not c.get("download_missing"): toks.append(flag("no_download_missing"))
    if c.get("disable_preview"):      toks.append(flag("disable_preview"))
    if c.get("offline"):              toks.append(flag("offline"))
    for a_ in assets:
        f = a_.get("flag")
        if not f:
            continue
        sem = _FLAG_SEMANTIC.get(f)
        toks += [flag(sem) if sem else f, jobfile(a_["file"])]
    extra_start = len(toks)
    for t in resolve_extra(extra, rd):
        if rd is not None and t.startswith("~/"):
            t = (home or "") + t[1:]
        toks.append(t)
    return toks, extra_start

def gen_args(model, assets, extra, c, ext, models_dir=None, video_format=None,
             dialect=DEFAULT_DIALECT):
    """The generate argument STRING shared by runner.sh and the dispatch-time
    --fflf-preflight probe. Paths are job-dir-relative (runner.sh cds in)."""
    toks, xstart = build_argv(model, assets, extra, c, ext, dialect=dialect,
                              models_dir=models_dir, video_format=video_format)
    s = " ".join(shlex.quote(t) for t in toks[:xstart])
    s += " " + " ".join(shlex.quote(t) for t in toks[xstart:])
    return s

def make_runner(model, cli_path, use_pty, assets, extra, c, ext, models_dir=None,
                video_format=None, dialect=DEFAULT_DIALECT):
    args = gen_args(model, assets, extra, c, ext, models_dir, video_format, dialect)
    return RUNNER.format(pty=1 if use_pty else 0, cli=dollar_home(cli_path),
                         generate=dialect_spec(dialect)["generate_cmd"], args=args)

def upload(alias, rd, local_dir, names):
    if is_local(alias):
        dest = Path(rd).expanduser()
        dest.mkdir(parents=True, exist_ok=True)
        errs = []
        for n in names:
            try: shutil.copyfile(Path(local_dir) / n, dest / n)
            except OSError as e: errs.append(str(e))
        class _R: returncode = 1 if errs else 0; stderr = "\n".join(errs)
        return _R(), not errs
    tar = subprocess.Popen(["tar", "-cf", "-", "-C", str(local_dir), *names],
                           stdout=subprocess.PIPE)
    d = dest_of(alias)[0]
    r = subprocess.run(["ssh", *ssh_eopts(alias), d, f"mkdir -p {rd} && tar -xf - -C {rd}"],
                       stdin=tar.stdout, capture_output=True, text=True)
    tar.stdout.close(); tar.wait()
    return r, (r.returncode == 0 and tar.returncode == 0)

def cli_of(c, h):
    """CLI binary for this host: per-host override beats the global default."""
    if h is not None and h["cli_path"]:
        return h["cli_path"]
    return c["cli_path"]

def video_format_of(c, h):
    """--video-format preset: global hosts.yaml default, per-host override.
    An explicit empty string on a host omits the flag entirely."""
    return h["video_format"] if h["video_format"] is not None else c.get("video_format")

def _host_key(h, key):
    """Row/dict-safe field access: sqlite3.Row misses raise IndexError, plain
    dicts (hosts.yaml entries) raise KeyError."""
    if h is None:
        return None
    try:
        return h[key]
    except (KeyError, IndexError):
        return None

def dialect_of(c, h):
    """Engine-CLI dialect name for this host: per-host `cli_dialect` beats the
    global `cli_dialect` key, which beats DEFAULT_DIALECT. `cli_path` (where
    the binary is) is a SEPARATE axis — a host can point cli_path at a second
    checkout of the same dialect, or keep the old path with a new dialect.
    An empty string counts as "not set" (blanking the key). An unknown name
    raises UnknownDialect naming the host and the value: no silent fallback."""
    name = (_host_key(h, "cli_dialect")
            or (c.get("cli_dialect") if c else None) or DEFAULT_DIALECT)
    if name not in DIALECTS:
        where = _host_key(h, "alias") or "global cli_dialect"
        raise UnknownDialect(
            f"unknown cli_dialect {name!r} for host {where!r} — known dialects: "
            f"{', '.join(sorted(DIALECTS))} (fix hosts.yaml; see docs/cli-dialects.md)")
    return name

def dialect_spec(name):
    if name not in DIALECTS:
        raise UnknownDialect(f"unknown cli_dialect {name!r} — known dialects: "
                             f"{', '.join(sorted(DIALECTS))}")
    return DIALECTS[name]

def dialect_has(spec, cap):
    """Can this dialect satisfy capability `cap`? `cap` is a feature boolean
    (`serve`, `fflf_preflight`) or a semantic flag name (an asset slot, or
    `keyframe` for the raw --keyframe* family)."""
    if cap in ("serve", "fflf_preflight"):
        return bool(spec.get(cap))
    return cap in spec["flags"]

def spec_flag(spec, dialect, semantic):
    """The dialect's spelling for a semantic flag, or UnsupportedFlag."""
    spelling = spec["flags"].get(semantic)
    if spelling is None:
        raise UnsupportedFlag(dialect, semantic)
    return spelling

_FLAG_SEMANTIC = {f: s for s, f in FLAGMAP}      # default-dialect spellings only
# Every dialect's spelling -> semantic: the serve backend validates that the
# value after a path flag is absolute, whatever dialect spelled that flag.
_ALL_FLAG_SEMANTIC = {f: s for spec in DIALECTS.values()
                      for s, f in spec["flags"].items()}

def extra_semantic(token):
    """The capability a raw `extra_arg` token needs, or None when ltxq does not
    recognize it (an unknown flag is the engine's business — the escape hatch
    stays open). `--keyframe`, `--keyframe-strength`,
    `--keyframe-attention-strength` and `--keyframe=...` all need `keyframe`."""
    t = token.split("=", 1)[0]
    if t.startswith("--keyframe"):
        return "keyframe"
    return _FLAG_SEMANTIC.get(t)

def job_caps(job, host_backend=None):
    """Capabilities a job needs from a host's dialect, derived from the job's
    own data: a serve-backend job needs `serve`; every asset flag it carries
    (and every recognized keyframe/asset token in extra_args) needs the
    matching rulebook entry; a job carrying BOTH --first-frame and --last-frame
    needs `fflf_preflight` (that is exactly when dispatch runs the probe).
    `host_backend` is the host's default backend, which applies to jobs that
    do not name one."""
    caps = set()
    if (job["backend"] or host_backend or "oneshot") == "serve":
        caps.add("serve")
    for a in json.loads(job["assets"] or "[]"):
        sem = _FLAG_SEMANTIC.get(a.get("flag"))
        if sem:
            caps.add(sem)
    for t in json.loads(job["extra_args"] or "[]"):
        sem = extra_semantic(t)
        if sem:
            caps.add(sem)
    if {"first_frame", "last_frame"} <= caps:
        caps.add("fflf_preflight")
    return caps

def missing_caps(spec, caps):
    """Sorted capability names this dialect cannot satisfy."""
    return sorted(c_ for c_ in caps if not dialect_has(spec, c_))


def missing_caps_hint(dialect, missing):
    """Rulebook guidance for a capability gap, or "" — plain text, no separator.
    Lets a refusal teach the dialect's model (e.g. canvas/moodboard) instead of
    only naming what is absent."""
    hints = (DIALECTS.get(dialect) or {}).get("cap_hints") or {}
    out = []
    for cap in missing:
        h = hints.get(cap)
        if h and h not in out:
            out.append(h)
    return " ".join(out)

UNROUTABLE_PREFIX = "unroutable:"

def unroutable_jobs(c, con):
    """{job_id: note} for queued jobs no ENABLED host's dialect can run.

    Capability only — a full host is not unroutable, just busy. Jobs pinned to
    a host are judged against that host alone; unpinned jobs against every
    enabled host. With no enabled host at all the map is empty (that is a
    config state, not a per-job one)."""
    specs = [(h, dialect_spec(dialect_of(c, h)))
             for h in con.execute("SELECT * FROM hosts WHERE enabled=1").fetchall()]
    out = {}
    for job in con.execute("SELECT * FROM jobs WHERE status='queued'"):
        cands = [(h, s) for h, s in specs if job["host"] in (None, h["alias"])]
        if not cands:
            continue
        if any(not missing_caps(s, job_caps(job, h["backend"])) for h, s in cands):
            continue
        out[job["id"]] = _unroutable_note(job, cands)
    return out

def _unroutable_note(job, cands):
    """Deterministic note: the job's own required capabilities plus, per
    considered host, the capabilities its dialect lacks."""
    need = sorted(job_caps(job, None)) or ["?"]
    detail, hints = [], []
    for h, s in cands:
        lacks = missing_caps(s, job_caps(job, h["backend"]))
        d = dialect_of_spec(s)
        detail.append(f"{h['alias']}={d} lacks {', '.join(lacks)}")
        hint = missing_caps_hint(d, lacks)
        if hint and hint not in hints:
            hints.append(hint)
    tail = (" — " + " ".join(hints)) if hints else ""
    return (f"{UNROUTABLE_PREFIX} no enabled host dialect can run this job — needs "
            + ", ".join(need) + "; " + "; ".join(detail) + tail)

def dialect_of_spec(spec):
    """The dialect name of a DIALECTS entry (reverse lookup for messages)."""
    for name, s in DIALECTS.items():
        if s is spec:
            return name
    return "?"

def note_unroutable(c, con):
    """Surface unroutable queued jobs in `note` without spamming it.

    Writes only when the computed text differs from what is already stored, and
    only into an empty note or a previous unroutable note — a job's own
    diagnostic (upload failure, failed preflight) is never clobbered. Clears
    the note again once the job becomes routable (hosts.yaml edited, host
    re-enabled). Callers: dispatch() and the dashboard engine loop."""
    try:
        bad = unroutable_jobs(c, con)
    except Exception as e:                      # never take the engine loop down
        print("routing scan error:", repr(e))
        return
    for jid, text in bad.items():
        row = con.execute("SELECT note FROM jobs WHERE id=?", (jid,)).fetchone()
        note = (row["note"] or "") if row else ""
        if note != text and (not note or note.startswith(UNROUTABLE_PREFIX)):
            set_job(con, jid, note=text)
    for row in con.execute("SELECT id, note FROM jobs WHERE status='queued' "
                           "AND note LIKE ?", (UNROUTABLE_PREFIX + "%",)).fetchall():
        if row["id"] not in bad:
            set_job(con, row["id"], note="")

def probe(con, c, alias):
    hrow = con.execute("SELECT cli_path FROM hosts WHERE alias=?", (alias,)).fetchone()
    cli = (hrow and hrow["cli_path"]) or c["cli_path"]
    r = ssh(alias, PROBE_SH.format(cli=shlex.quote(dollar_home(cli))))
    if r.returncode != 0:
        print(f"{alias}: UNREACHABLE: {r.stderr.strip()[:200]}"); return
    home, ev, cands = "", "", []
    for line in r.stdout.splitlines():
        if line.startswith("HOME="):    home = line[5:]
        elif line.startswith("EV="):    ev = line[3:]
        elif line.startswith("CAND|"):
            f = line.split("|", 3)
            if len(f) == 4: cands.append({"label": f[1], "path": f[2], "ck": f[3]})
    darwin = 'UNAME=Darwin' in r.stdout
    ok = [x for x in cands if x["ck"].isdigit() and int(x["ck"]) > 0]
    pick = next((x for lbl in ORDER for x in ok if x["label"] == lbl), None) \
        or (cands[0] if cands else None)
    con.execute("UPDATE hosts SET home=?, uname=?, models_dir=COALESCE(models_dir, ?) "
                "WHERE alias=?",
                (home, "Darwin" if darwin else "other",
                 pick["path"] if pick else "", alias))
    con.commit()
    pinned = con.execute("SELECT models_dir FROM hosts WHERE alias=?", (alias,)).fetchone()[0]
    print(f"{alias}: pinned: {pinned or 'NOT FOUND — pin models_dir: in hosts.yaml'}")
    for x in sorted(cands, key=lambda x: ORDER.index(x["label"]) if x["label"] in ORDER else 9):
        mark = "  <-- pinned" if pick and x["path"] == pick["path"] else ""
        print(f"  [{x['label']}] {x['path']}  ({x['ck']} ckpt){mark}")
    m = [x for x in cands if x["label"] == "env"]
    if ev and not m:
        print(f"  note: env var set ({ev}) but invisible to non-interactive ssh "
              "(export in ~/.zshrc — move to ~/.zshenv; pin decides regardless)")
    elif m and m[0]["ck"] == "0":
        print(f"  WARN: $DRAWTHINGS_MODELS_DIR points at an empty dir: {ev}")
    if len(ok) > 1:
        print("  note: several candidates hold ckpts — pin wins; override in hosts.yaml if wrong")

def _worker_alive(h):
    r = ssh(h["alias"], f'kill -0 "$(cat {shlex.quote(wdir(h))}/worker.pid 2>/dev/null)" '
                        f'2>/dev/null && echo ALIVE || echo DEAD')
    return "ALIVE" in r.stdout


def next_dispatchable_job(con, alias, c=None, h=None):
    """Return the oldest queued job eligible for ``alias``.

    A batch-scoped chained job must wait until every active sibling has
    finished.  That keeps its launch-time continuation frame deterministic
    when several hosts have free capacity.  Both engine entry points use this
    selector so the dashboard and headless scheduler follow the same rule.

    When ``c``/``h`` are supplied, candidates this host's dialect cannot run
    are skipped, so the host walk falls through to a capable host instead of
    claiming a job it would only fail on.
    """
    spec = (dialect_spec(dialect_of(c, h)) if (c is not None and h is not None)
            else None)
    for cand in con.execute(
            "SELECT * FROM jobs WHERE status='queued' AND "
            "(host IS NULL OR host=?) ORDER BY created_at, rowid",
            (alias,)).fetchall():
        if spec is not None and missing_caps(spec, job_caps(cand, h["backend"])):
            continue
        if cand["chain"] and cand["batch"]:
            active = con.execute(
                "SELECT COUNT(*) FROM jobs WHERE batch=? AND id!=? AND status IN "
                "('uploading','running','collecting','suspect','cancelling')",
                (cand["batch"], cand["id"])).fetchone()[0]
            if active:
                continue
        return cand
    return None


def dispatch(c, con):
    for h in con.execute("SELECT * FROM hosts WHERE enabled=1").fetchall():
        busy = con.execute("SELECT COUNT(*) FROM jobs WHERE host=? AND status IN "
                           "('uploading','running','collecting','suspect','cancelling')",
                           (h["alias"],)).fetchone()[0]
        if busy >= h["max_jobs"]:
            continue
        job = next_dispatchable_job(con, h["alias"], c, h)
        if job:
            cur = con.execute("UPDATE jobs SET status='uploading', host=? "
                              "WHERE id=? AND status='queued'", (h["alias"], job["id"]))
            con.commit()
            if cur.rowcount:
                launch(c, con, h, job)
    # Surface queued jobs no enabled host's dialect can run (non-spamming).
    note_unroutable(c, con)

def launch(c, con, h, job):
    # Capability gate before any remote work: a job this host's dialect cannot
    # run must never be uploaded only to fail in the engine CLI. dispatch()
    # already routes around such hosts; reaching here means the job is pinned
    # to this host (or hosts.yaml changed mid-flight) — fail loudly.
    dialect = dialect_of(c, h)
    need = missing_caps(dialect_spec(dialect), job_caps(job, h["backend"]))
    if need:
        hint = missing_caps_hint(dialect, need)
        set_job(con, job["id"], status="failed",
                note=f"host '{h['alias']}' runs dialect {dialect!r}, which cannot "
                     f"run this job — missing {', '.join(need)}"
                     + (f" — {hint}" if hint else ""))
        return
    if not h["home"] or not h["models_dir"]:
        probe(con, c, h["alias"])
        h = con.execute("SELECT * FROM hosts WHERE alias=?", (h["alias"],)).fetchone()
        if not h["home"]:
            set_job(con, job["id"], status="queued",
                    note="host unreachable — probe failed"); return
    note = resolve_chain(con, job)
    if note:
        set_job(con, job["id"], note=note)
        job = con.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
    backend = job["backend"] or h["backend"] or "oneshot"
    if backend == "serve":
        return launch_serve(c, con, h, job)
    rd = f"{h['home']}/{c['remote_root']}/jobs/{job['id']}"
    ext = job["ext"] or "mov"
    assets = json.loads(job["assets"] or "[]")
    extra = json.loads(job["extra_args"] or "[]")
    vf = video_format_of(c, h)
    (Path(job["local_dir"]) / "runner.sh").write_text(make_runner(
        job["model"], cli_of(c, h), c["use_pty"], assets, extra, c, ext,
        models_dir=h["models_dir"], video_format=vf, dialect=dialect))
    names = ["runner.sh", "prompt.txt"]
    names.append("config.json" if (Path(job["local_dir"]) / "config.json").exists()
                 else "config.txt")
    names += [x["file"] for x in assets]
    r, ok = upload(h["alias"], rd, Path(job["local_dir"]), names)
    if not ok:
        set_job(con, job["id"], status="queued",
                note="upload failed: " + r.stderr.strip()[:200]); return
    md = dollar_home(h["models_dir"]) if h["models_dir"] else ""
    mdchk = (f'test -d {shlex.quote(md)} || {{ echo NODMDIR; exit 3; }}; ') if md else ""
    # CLI must exist and be executable on the host — fail before burning the upload.
    # Double quotes so $HOME expands remotely while spaces in the path survive.
    cchk = (f'test -x "{dollar_home(cli_of(c, h))}" '
            f'|| {{ echo NOCLI; exit 3; }}; ')
    r = ssh(h["alias"], f"{cchk}{mdchk}echo CHKOK")
    if "NOCLI" in r.stdout:
        set_job(con, job["id"], status="queued",
                note=f"engine CLI not found at {cli_of(c, h)} on host '{h['alias']}' "
                     "— check cli_path in hosts.yaml"); return
    if "NODMDIR" in r.stdout:
        set_job(con, job["id"], note=f"models dir missing on host (volume unmounted?) "
                                     f"— retrying: {h['models_dir']}"); return
    if r.returncode != 0:
        set_job(con, job["id"], note="launch failed: "
                                     + (r.stderr or r.stdout).strip()[:200]); return
    # --fflf-preflight at dispatch: once the chain frame is resolved into the
    # job dir, all first/last inputs exist, so the engine can validate them and
    # resolve indices before we burn a render on an unattended queue. Runs only
    # when BOTH frames are present (the engine's preflight validates the pair);
    # transport errors skip the check, an engine-level failure fails the job.
    pf_note = ""
    if {"--first-frame", "--last-frame"} <= {a.get("flag") for a in assets}:
        pf = (f"cd {shlex.quote(rd)} && {shlex.quote(dollar_home(cli_of(c, h)))} "
              f"{dialect_spec(dialect)['generate_cmd']} "
              f"{gen_args(job['model'], assets, extra, c, ext, models_dir=h['models_dir'], video_format=vf, dialect=dialect)} "
              f"--fflf-preflight")
        try:
            pr = ssh(h["alias"], pf, timeout=300)
        except Exception as e:
            pf_note = f"fflf preflight skipped (transport: {e})"[:160]
        else:
            (Path(job["local_dir"]) / "preflight.txt").write_text(pr.stdout + pr.stderr)
            if pr.returncode != 0:
                set_job(con, job["id"], status="failed",
                        note="fflf preflight failed: "
                             + (pr.stdout + pr.stderr).strip()[-280:])
                return
            out = " ".join((pr.stdout or "").split())
            pf_note = ("fflf preflight ok — " + out[-140:]) if out else "fflf preflight ok"
    r = ssh(h["alias"], f"cd {shlex.quote(rd)} && nohup sh runner.sh > launch.log 2>&1 & echo $!")
    ok = r.returncode == 0 and r.stdout.strip().isdigit()
    set_job(con, job["id"], status="running" if ok else "queued", host=h["alias"],
            backend="oneshot", remote_dir=rd,
            note=pf_note if ok else "launch failed: " + (r.stderr or r.stdout).strip()[:200])

def serve_args(job, h, c, dialect=None):
    """Serve-backend argv for a job: absolute paths, `$HOME` expanded, ready
    for the newline-delimited JSON request. Built by the same dialect-aware
    builder as oneshot, so both backends speak the host's dialect."""
    rd = f"{h['home']}/{c['remote_root']}/jobs/{job['id']}"
    dialect = dialect or dialect_of(c, h)
    args, _ = build_argv(job["model"], json.loads(job["assets"] or "[]"),
                         json.loads(job["extra_args"] or "[]"), c,
                         job["ext"] or "mov", dialect=dialect,
                         models_dir=h["models_dir"],
                         video_format=video_format_of(c, h),
                         rd=rd, home=h["home"])
    bad = [args[i] for i in range(len(args) - 1)
           if _ALL_FLAG_SEMANTIC.get(args[i]) in PATH_SEMANTICS
           and not args[i + 1].startswith("/")]
    return rd, (None if bad else args), \
           ("non-absolute path for " + ", ".join(bad) if bad else None)

def launch_serve(c, con, h, job):
    if not dialect_has(dialect_spec(dialect_of(c, h)), "serve"):
        set_job(con, job["id"], status="failed",
                note=f"host '{h['alias']}' runs dialect {dialect_of(c, h)!r}, which "
                     f"has no serve backend — run this job as oneshot instead")
        return
    if not _worker_alive(h):
        set_job(con, job["id"], status="queued", note="serve worker not running — run: "
                                     "python3 ltxq.py serve-start " + h["alias"]); return
    rd, args, err = serve_args(job, h, c)
    if err:
        set_job(con, job["id"], status="failed", note="serve args: " + err); return
    w = wdir(h)
    r = ssh(h["alias"], f"wc -c < {shlex.quote(w)}/worker.log 2>/dev/null || echo 0")
    try: off = int(r.stdout.strip() or 0)
    except ValueError: off = 0
    req = json.dumps({"id": job["id"], "args": args}, separators=(",", ":"))
    (Path(job["local_dir"]) / f"{job['id']}.req").write_text(req + "\n")
    names = [f"{job['id']}.req", "prompt.txt",
             "config.json" if (Path(job["local_dir"]) / "config.json").exists()
             else "config.txt"]
    names += [x["file"] for x in json.loads(job["assets"] or "[]")]
    up, ok = upload(h["alias"], rd, Path(job["local_dir"]), names)
    if not ok:
        set_job(con, job["id"], status="queued",
                note="upload failed: " + up.stderr.strip()[:200]); return
    ssh(h["alias"], f"cat {shlex.quote(rd + '/' + job['id'] + '.req')} > {shlex.quote(w)}/fifo")
    set_job(con, job["id"], status="running", host=h["alias"], backend="serve",
            remote_dir=rd, started_at=int(time.time()), cur_off=off, note="")

def poll_job(c, con, job):
    h = con.execute("SELECT * FROM hosts WHERE alias=?", (job["host"],)).fetchone()
    r = ssh(h["alias"], POLL_CMD.format(rd=shlex.quote(job["remote_dir"])))
    if r.returncode != 0:
        set_job(con, job["id"], note="poll ssh error: " + r.stderr.strip()[:200]); return
    if "NODIR" in r.stdout:
        set_job(con, job["id"], note="remote job dir missing"); return
    exit_s, log, alive = parse_poll(r.stdout)
    now, upd = int(time.time()), {}
    m = PCT.findall(log)
    if m and min(int(m[-1]), 100) != job["pct"]:
        upd.update(pct=min(int(m[-1]), 100), pct_ts=now)
    t, s = TOTAL.search(log), STEPS.search(log)
    if t: upd["total_gen_s"] = float(t.group(1))
    if s: upd.update(steps=int(s.group(1)), step_avg_s=float(s.group(2)),
                     step_median_s=float(s.group(3)))
    upd["log_tail"] = log[-4000:]
    if exit_s:
        code = int(exit_s.split()[-1])
        upd.update(exit_code=code, finished_at=now)
        if job["status"] == "cancelling":
            upd["status"] = "cancelled"; cleanup(c, h, job["remote_dir"])
        elif code == 0:
            wrote = WROTE.findall(log)
            if not wrote:
                wrote = WROTE.findall(ssh(h["alias"],
                    f"tail -c 200000 {shlex.quote(job['remote_dir'] + '/log.txt')}").stdout)
            if wrote:
                upd.update(status="collecting", remote_wrote=json.dumps(wrote), note="")
            else:
                upd.update(status="collecting", note="exit 0 but no Wrote: line found")
        else:
            upd.update(status="failed", note=f"exit {code}")
    elif not alive:
        upd.update(status="failed", note="process gone, no exit_code (crash / host reboot?)")
    elif now - (upd.get("pct_ts") or job["pct_ts"] or now) > c["stall_secs"] \
            and job["status"] != "suspect":
        upd["status"] = "suspect"
    set_job(con, job["id"], **upd)

def poll_serve_job(c, con, job):
    h = con.execute("SELECT * FROM hosts WHERE alias=?", (job["host"],)).fetchone()
    w = shlex.quote(wdir(h))
    r = run_cmd(h["alias"],
        f'echo SIZE=$(wc -c < {w}/worker.log 2>/dev/null || echo 0); '
        f'kill -0 "$(cat {w}/worker.pid 2>/dev/null)" 2>/dev/null '
        f'&& echo WALIVE || echo WDEAD; '
        f'tail -c +{(job["cur_off"] or 0) + 1} {w}/worker.log 2>/dev/null',
        timeout=60)
    if r.returncode != 0:
        set_job(con, job["id"], note="poll ssh error: "
                + r.stderr.strip()[:200]); return
    raw = r.stdout
    m = re.match(r"SIZE=(\d+)\r?\n(WALIVE|WDEAD)\r?\n", raw)
    if m: size, walive, data = int(m.group(1)), m.group(2) == "WALIVE", raw[m.end():]
    else: size, walive, data = (job["cur_off"] or 0), True, raw
    if not data.endswith("\n"):
        data = data[:data.rfind("\n") + 1]
    chunk = data
    now, upd = int(time.time()), {"cur_off": (job["cur_off"] or 0) + len(data.encode())}
    for pat in (GENP, PCT):
        mm = pat.findall(chunk)
        if mm and min(int(mm[-1]), 100) != job["pct"]:
            upd.update(pct=min(int(mm[-1]), 100), pct_ts=now); break
    t, s = TOTAL.search(chunk), STEPS.search(chunk)
    if t: upd["total_gen_s"] = float(t.group(1))
    if s: upd.update(steps=int(s.group(1)), step_avg_s=float(s.group(2)),
                     step_median_s=float(s.group(3)))
    if chunk.strip():
        upd["log_tail"] = ((job["log_tail"] or "") + chunk)[-4000:]
    term, msg, dur = None, "", None
    for line in chunk.splitlines():
        e = EVT.search(line)
        if not e: continue
        try: ev = json.loads(e.group(1))
        except json.JSONDecodeError: continue
        if ev.get("id") != job["id"]: continue
        if ev["event"] == "started":     upd.setdefault("started_at", now)
        elif ev["event"] == "completed": term, dur = "completed", ev.get("duration")
        elif ev["event"] == "failed":    term, msg = "failed", ev.get("message", "unknown")
    if dur and "total_gen_s" not in upd: upd["total_gen_s"] = round(dur, 2)
    if term == "completed":
        if job["status"] == "cancelling":
            upd.update(status="cancelled", finished_at=now)
            cleanup(c, h, wdir(h).replace("/worker", ""))  # no-op guard; real dir below
            cleanup(c, h, job["remote_dir"])
        else:
            wrote = WROTE.findall(chunk)
            if not wrote:
                wrote = WROTE.findall(ssh(h["alias"],
                    f"tail -c 200000 {shlex.quote(wdir(h) + '/worker.log')}").stdout)
            if wrote:
                upd.update(status="collecting", remote_wrote=json.dumps(wrote), note="")
            else:
                upd.update(status="collecting", note="completed but Wrote not found")
    elif term == "failed":
        note = msg[:200]
        if "ArgumentParser.ValidationError" in msg:
            note = (note + " [ValidationError: cause earlier in worker.log — usual: "
                    "missing --model, non-absolute path, unknown flag]")[:300]
        if job["status"] == "cancelling": upd["status"] = "cancelled"
        else: upd.update(status="failed", exit_code=1, finished_at=now, note=note)
    elif not walive and job["status"] != "cancelling":
        upd.update(status="failed", finished_at=now,
                   note="worker died mid-job (check worker.log tail)")
    set_job(con, job["id"], **upd)

def cleanup(c, h, rd):
    if rd and not c["keep_remote"]:
        ssh(h["alias"], f"rm -rf {shlex.quote(rd)}")

def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()

def collect(c, con, job):
    wrote = json.loads(job["remote_wrote"] or "[]")
    if not (job["host"] and wrote):
        set_job(con, job["id"], note="collect: missing host/wrote paths"); return
    h = con.execute("SELECT * FROM hosts WHERE alias=?", (job["host"],)).fetchone()
    dest = Path(c["movies_dir"]).expanduser() / f"{slug(job['name'])}-{job['id']}"
    dest.mkdir(parents=True, exist_ok=True)
    srcs = [f"{dest_of(job['host'])[0]}:{shlex.quote(w)}" for w in wrote]
    if is_local(job["host"]):
        import shutil as _sh
        errs = []
        for w in wrote:
            try: _sh.copyfile(Path(w).expanduser(), dest / Path(w).name)
            except OSError as e: errs.append(str(e))
        class _R: returncode = 1 if errs else 0; stderr = "\n".join(errs)
        r = _R()
    else:
        r = subprocess.run(["rsync", "-a", "--partial", "--timeout=120",
                            "-e", "ssh " + " ".join(ssh_eopts(job["host"])),
                            *srcs, str(dest) + "/"],
                           capture_output=True, text=True, timeout=7200)
    outs = []
    for w in wrote:
        p = dest / Path(w).name
        if p.exists():
            outs.append({"wrote": w, "local": str(p), "size": p.stat().st_size,
                         "sha256": sha256(p)})
    if r.returncode != 0 or len(outs) != len(wrote):
        set_job(con, job["id"], note="rsync retry: " + r.stderr.strip()[-200:]); return
    meta = dict(job); meta["outputs"] = outs
    if job["num_frames"] and job["fps"]:
        meta["duration_s"] = round(job["num_frames"] / job["fps"], 2)
    meta["collected_at"] = int(time.time())
    (dest / "metadata.json").write_text(json.dumps(meta, indent=2, default=str))
    set_job(con, job["id"], status="done", local_out=str(dest),
            finished_at=int(time.time()), note="")
    cleanup(c, h, job["remote_dir"])
    print(f"collected {job['id']} -> {dest} ({len(outs)} file(s))")

def slug(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")[:48] or "job"

def video_of(job):
    """First collected video file of a finished job, or None."""
    out = Path(job["local_out"] or "")
    if not out.is_dir():
        return None
    vids = sorted(out.glob("*.mov")) + sorted(out.glob("*.mp4"))
    return str(vids[0]) if vids else None

FFTOOL_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", "/usr/bin", "/bin")

def ff_tool(name):
    """Locate ffmpeg/ffprobe even under the minimal PATH a GUI app hands down."""
    p = shutil.which(name)
    if p:
        return p
    for d in FFTOOL_DIRS:
        c = Path(d) / name
        if c.exists():
            return str(c)
    raise RuntimeError(f"{name} not found — install ffmpeg (e.g. brew install ffmpeg)")

def extract_frame(v, frame, dst, timeout=120):
    """Extract one frame of video v to PNG dst. frame: int / 'first' / 'last'.
    Returns the extracted frame number; raises RuntimeError/ValueError."""
    fr = str(frame).strip()
    if fr == "first":
        n = 0
    elif fr == "last":
        pr = subprocess.run([ff_tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
                             "-count_frames", "-show_entries",
                             "stream=nb_read_frames", "-of", "csv=p=0", v],
                            capture_output=True, text=True, timeout=timeout)
        try: n = max(0, int(pr.stdout.strip()) - 1)
        except ValueError:
            raise RuntimeError("ffprobe failed: " + pr.stderr[-150:])
    elif not fr.isdigit():
        raise ValueError("frame must be a number, 'first' or 'last'")
    else:
        n = int(fr)
    r = subprocess.run([ff_tool("ffmpeg"), "-y", "-v", "error", "-i", v,
                        "-vf", f"select=eq(n\\,{n})", "-frames:v", "1", str(dst)],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError("ffmpeg: " + (r.stderr[-200:] or "no output"))
    return n

def resolve_chain(con, job):
    """Chain continuation: if the job has `chain` set (target slot), extract the
    last frame of the most recent finished generation and attach it to that
    slot. Jobs carrying a `batch` label look for the previous finished job
    *within that batch* only; unbatched jobs keep the global scope. Returns a
    note (also when skipped/omitted), or None. Never fails the job: a missing
    source or an extract error just runs without the frame."""
    slot = job["chain"]
    if not slot:
        return None
    flag = dict(FLAGMAP).get(slot)
    if not flag:
        return "chain: unknown slot " + slot
    assets = json.loads(job["assets"] or "[]")
    if any(a.get("flag") == flag for a in assets):
        return None                     # manual attachment wins; also requeue re-run
    if job["batch"]:
        prev = con.execute(
            "SELECT * FROM jobs WHERE status='done' AND id!=? AND local_out IS NOT NULL "
            "AND batch=? ORDER BY finished_at DESC, created_at DESC LIMIT 1",
            (job["id"], job["batch"])).fetchone()
        nogen = (f"chain: no finished generation in batch '{job['batch']}' yet "
                 "— ran without start frame")
    else:
        prev = con.execute(
            "SELECT * FROM jobs WHERE status='done' AND id!=? AND local_out IS NOT NULL "
            "ORDER BY finished_at DESC, created_at DESC LIMIT 1", (job["id"],)).fetchone()
        nogen = "chain: no finished generation to continue from — ran without start frame"
    v = video_of(prev) if prev else None
    if not v:
        return nogen
    tmp = Path(job["local_dir"]) / f"chain_{prev['id']}.tmp.png"
    try:
        n = extract_frame(v, "last", tmp)
    except (RuntimeError, ValueError) as e:
        tmp.unlink(missing_ok=True)
        return f"chain: extract failed ({e}) — ran without start frame"
    dst = tmp.with_name(f"chain_{prev['id']}_f{n}.png")
    tmp.rename(dst)
    assets.append({"flag": flag, "file": dst.name, "local": str(dst)})
    set_job(con, job["id"], assets=json.dumps(assets))
    return f"chain: {slot} = last frame (f{n}) of {prev['id']}"

def cmd_add(a):
    c, con = conf(), db(); sync_hosts(con)
    try:
        return _cmd_add(c, con, a)
    finally:
        con.close()

def _load_cfg(c, con, model, config_file, config_json):
    """Resolve a job's config: --config-file, else the per-model template.
    Applies --config-json on top, runs the shared warnings, and returns
    (cfg_text, parsed obj) — composers parse once and derive per-item copies."""
    if config_file:
        cfg_text = Path(config_file).read_text()
    else:
        t = HERE / "templates" / f"{model}.json"
        if not t.exists():
            sys.exit(f"no --config-file and no template at {t}")
        cfg_text = t.read_text()
    dups = []
    def _pairs(pairs):
        ks = [k for k, _ in pairs]
        dups.extend(k for k in set(ks) if ks.count(k) > 1)
        return dict(pairs)
    try:
        obj = json.loads(cfg_text, object_pairs_hook=_pairs)
    except json.JSONDecodeError as e:
        sys.exit(f"config is not valid JSON: {e}")
    if dups:
        print("warn: duplicate keys in config (last value wins): "
              + ", ".join(sorted(set(dups))))
    if config_json:
        obj.update(json.loads(config_json)); cfg_text = json.dumps(obj, indent=2)
    for k in ("width", "height"):
        if obj.get(k) and obj[k] % 64:
            print(f"warn: {k}={obj[k]} is not a multiple of 64")
    cm = obj.get("model")
    if isinstance(cm, str) and cm and cm != model:
        reg = con.execute("SELECT model FROM registry WHERE name=?", (cm,)).fetchone()
        if reg and reg[0] != model:
            print(f"warn: config model '{cm}' = {reg[0]} but --model is {model} "
                  "— --model governs resolution")
        elif not reg:
            print(f"note: config carries model name '{cm}'; resolution uses --model {model}")
    loras = [l.get("file") for l in obj.get("loras", []) if l.get("file")]
    if loras:
        print("lora deps:", ", ".join(loras), "(run 'check <id>' after host assignment)")
    return cfg_text, obj

def _norm_batch(label):
    b = re.sub(r"[^A-Za-z0-9._-]+", "_", (label or "").strip())[:48] or None
    if label and b != label.strip():
        print(f"batch label normalized: {b}")
    return b

def create_job(c, con, model, prompt, cfg_text, assets, extra, *, host=None,
               name=None, parent=None, ext="mov", backend=None, chain=None,
               batch=None, num_frames=None, fps=None):
    """The shared tail of every job-creation path (add, the web API, batch
    composers): write the job dir (prompt.txt, config.json, copied assets,
    runner.sh) and insert the row. assets is a list of (flag|None, path)
    pairs — each file is copied into the job dir; extra is a flat token list
    appended verbatim to the engine CLI. Returns the new jid."""
    jid = uuid.uuid4().hex[:10]
    if not name:
        name = " ".join(prompt.split())[:48] or jid
    d = HERE / "jobs" / jid; d.mkdir(parents=True)
    (d / "prompt.txt").write_text(prompt)
    (d / "config.json").write_text(cfg_text)
    rows = []
    for flag, p in assets:
        src = Path(p).expanduser()
        shutil.copyfile(src, d / src.name)
        rows.append({"flag": flag, "file": src.name, "local": str(src)})
    host_cli = c["cli_path"]
    host_dialect = c.get("cli_dialect") or DEFAULT_DIALECT
    if host:
        hr = con.execute("SELECT * FROM hosts WHERE alias=?",
                         (host,)).fetchone()
        if hr:
            if hr["cli_path"]: host_cli = hr["cli_path"]
            host_dialect = dialect_of(c, hr)
    (d / "runner.sh").write_text(make_runner(model, host_cli, c["use_pty"],
                                             rows, extra, c, ext,
                                             dialect=host_dialect))
    con.execute("INSERT INTO jobs(id,created_at,name,parent_id,host,model,prompt,"
                "config_text,status,local_dir,pct_ts,assets,extra_args,num_frames,"
                "fps,ext,backend,chain,batch) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (jid, int(time.time()), name, parent, host, model, prompt,
                 cfg_text, "queued", str(d), int(time.time()), json.dumps(rows),
                 json.dumps(extra), num_frames, fps, ext, backend, chain, batch))
    con.commit()
    return jid

def _cmd_add(c, con, a):
    if ".." in a.model or a.model.startswith("/"):
        sys.exit("invalid --model: must be a model id, not a path with '..' or '/'")
    prompt = Path(a.prompt_file).read_text()
    cfg_text, obj = _load_cfg(c, con, a.model, a.config_file, a.config_json)
    assets = []
    for attr, flag in FLAGMAP:
        v = getattr(a, attr, None)
        # `image` is the one multi-valued slot: repeatable --image, order kept
        for p in (v if isinstance(v, (list, tuple)) else [v]):
            if p: assets.append((flag, p))
    for p in a.upload or []:
        assets.append((None, p))
    extra = []
    for ea in a.extra_arg or []:
        extra += shlex.split(ea)
    seed = a.seed if a.seed is not None else \
        (random.randint(1, 2**31 - 1) if a.new_seed else None)
    if seed is not None and seed < 0:
        # engines reject a negative --seed; a negative seed REQUEST means random
        print(f"seed {seed} is not a valid engine seed — using a random one")
        seed = random.randint(1, 2**31 - 1)
    if seed is not None:
        extra += ["--seed", str(seed)]; print("seed:", seed)
    if a.frames is not None:
        if a.frames < 1: sys.exit("--frames must be a positive integer")
        extra += ["--frames", str(a.frames)]; print("frames:", a.frames)
    jid = create_job(c, con, a.model, prompt, cfg_text, assets, extra,
                     host=a.host, name=a.name, parent=a.parent, ext=a.ext,
                     backend=a.backend, chain=a.chain, batch=_norm_batch(a.batch),
                     num_frames=a.frames if a.frames is not None
                                else obj.get("numFrames"),
                     fps=obj.get("fps"))
    print(jid)

# --- add-batch: audio-segment batch composer (docs/expansion-of-this-idea.md Idea 1)

NATNUM = re.compile(r"^(.*?)(\d+)$")

def _natural_key(p):
    """Sort key for zero-padded segment names: common stem first, then the
    embedded number numerically (gaps in numbering are fine)."""
    m = NATNUM.search(p.stem)
    return (m.group(1) if m else p.stem, int(m.group(2)) if m else -1, p.name)

# Frame grids: valid lengths are step*n + base with n >= min_n.
# LTX-2 / WAN: 8n+1 (composer floor 9 via min_n=1 — the historical max(n, 1)
# clamp). MiniMax H3: 17n+5 with n >= 0 (5 frames valid; spec 2026-09-15).
# Unknown models default to the LTX grid.
DEFAULT_GRID = (8, 1, 1)
FRAME_GRIDS = (
    ("minimax", (17, 5, 0)),
    ("ltx", (8, 1, 1)),
    ("wan", (8, 1, 1)),
)


def frame_grid(model):
    """(step, base, min_n) for a model name; LTX-2 8n+1 is the default.
    'H3' matches on token boundaries so MiniMax-H3 and minimax_h3_7b match,
    but wanh3x does not."""
    m = (model or "").lower()
    if "minimax" in m or re.search(r"(?<![a-z0-9])h3(?![a-z0-9])", m):
        return (17, 5, 0)
    for key, grid in FRAME_GRIDS:
        if key in m:
            return grid
    return DEFAULT_GRID


def _snap_frames(raw, up=True, grid=DEFAULT_GRID):
    """Snap a raw frame count onto a model's frame grid (step*n + base,
    n >= min_n). LTX-2/WAN: 8n+1 (floor 9); MiniMax H3: 17n+5 (floor 5).
    up pads ~a fraction of a second of silence, down trims the tail."""
    step, base, min_n = grid
    n = ((raw - base) + (step - 1)) // step if up else (raw - base) // step
    return step * max(n, min_n) + base

def _parse_audio_manifest(path):
    """Parse the cutting helper's manifest; None when the file doesn't carry
    the header (prompt sidecars etc. are excluded by this check)."""
    text = Path(path).read_text()
    if "Frame Length Manifest" not in text:
        return None
    m = re.search(r"^#\s*(\d+)\s*FPS", text, re.M)
    fps = int(m.group(1)) if m else None
    m = re.search(r"^#\s*Original File:\s*(.+)$", text, re.M)
    orig = m.group(1).strip() if m else None
    m = re.search(r"^#\s*Total Segments:\s*(\d+)", text, re.M)
    total = int(m.group(1)) if m else None
    per = {Path(n).name: int(f) for n, f in
           re.findall(r"^(.+?\.wav):\s*(\d+)\s+frames", text, re.M | re.I)}
    i = text.find("# Raw Frame Counts")
    raw = [int(x) for x in re.findall(r"^\s*(\d+)\s*$", text[i:], re.M)] if i >= 0 else []
    return {"fps": fps, "orig": orig, "total": total, "per": per, "raw": raw}

def _wav_duration(path):
    r = subprocess.run([ff_tool("ffprobe"), "-v", "error", "-show_entries",
                        "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError("ffprobe: " + r.stderr.strip()[-150:])
    return float(r.stdout.strip())

def _fit_wav(src, frames, fps, dst, mode):
    """Fit src to exactly frames/fps seconds: 'pad' appends silence to the
    grid length (never invents audible content), 'trim' drops the tail that
    doesn't fit."""
    args = [ff_tool("ffmpeg"), "-y", "-v", "error", "-i", str(src)]
    if mode == "pad":
        args += ["-af", "apad"]
    args += ["-t", f"{frames / fps:.6f}", str(dst)]
    r = subprocess.run(args, capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError(f"ffmpeg {mode}: " + (r.stderr.strip()[-150:] or "no output"))
    return dst

def cmd_add_batch(a):
    c, con = conf(), db(); sync_hosts(con)
    try:
        return _cmd_add_batch(c, con, a)
    finally:
        con.close()

def _cmd_add_batch(c, con, a):
    """Audio mode: one job per wav segment in a directory. Frames come from
    the helper's manifest (verbatim, cross-checked) or ffprobe + grid snap;
    prompts from same-basename .txt sidecars, falling back to --prompt-file.
    Non-grid lengths default to rounding up with a silence pad. Validates the
    whole batch before queueing anything."""
    if ".." in a.model or a.model.startswith("/"):
        sys.exit("invalid --model: must be a model id, not a path with '..' or '/'")
    segdir = Path(a.dir).expanduser()
    if not segdir.is_dir():
        sys.exit(f"not a directory: {segdir}")
    wavs = sorted((p for p in segdir.iterdir()
                   if p.suffix.lower() == ".wav" and not p.name.startswith(".")),
                  key=_natural_key)
    if not wavs:
        sys.exit(f"no .wav segments in {segdir}")
    wav_stems = {p.stem for p in wavs}
    for jp in sorted(segdir.glob("*.json")):
        if jp.stem not in wav_stems:
            print(f"warn: {jp.name} in {segdir.name} does not match any .wav segment",
                  file=sys.stderr)
    if a.manifest:
        man = _parse_audio_manifest(Path(a.manifest).expanduser())
        if man is None:
            sys.exit(f"{a.manifest} does not carry a Frame Length Manifest header")
    else:
        found = [(p, _parse_audio_manifest(p)) for p in sorted(segdir.glob("*.txt"))]
        found = [(p, m) for p, m in found if m]
        if len(found) > 1:
            sys.exit(f"{len(found)} manifests in {segdir} — pass --manifest explicitly")
        man_path, man = found[0] if found else (None, None)
        if man:
            print(f"manifest: {man_path.name}")
    cfg_text, obj = _load_cfg(c, con, a.model, a.config_file, a.config_json)
    grid = frame_grid(a.model)
    step, base, min_n = grid
    grid_floor = step * min_n + base
    print(f"frame grid: {step}n+{base} (floor {grid_floor}) — {a.model}")
    tfps = obj.get("fps")
    fps = float(tfps) if tfps else (float(man["fps"]) if man and man["fps"] else None)
    if man and man["fps"] and tfps and float(man["fps"]) != float(tfps):
        sys.exit(f"manifest fps {man['fps']} != template config fps {tfps} — refusing "
                 "(mismatched fps would silently mis-time every segment)")
    errors, items = [], []
    if man:
        if man["total"] is not None and man["total"] != len(wavs):
            errors.append(f"manifest Total Segments ({man['total']}) != wavs in dir ({len(wavs)})")
        if set(man["per"]) != {p.name for p in wavs}:
            errors.append("manifest file lines don't match the wavs in the dir")
        if man["raw"] and man["raw"] != list(man["per"].values()):
            errors.append("Raw Frame Counts block != per-file frame counts")
    for i, p in enumerate(wavs):
        if man:
            src_frames = man["per"].get(p.name)
            if src_frames is None:
                errors.append(f"{p.name}: no manifest entry"); continue
        else:
            try:
                dur = _wav_duration(p)
            except (RuntimeError, ValueError) as e:
                errors.append(f"{p.name}: {e}"); continue
            if not fps:
                errors.append(f"{p.name}: no manifest and no fps (template config or "
                              "manifest header) — cannot derive a frame count"); continue
            src_frames = round(dur * fps)
            if src_frames < grid_floor:
                errors.append(f"{p.name}: {dur:.2f}s is under the {grid_floor}-frame "
                              f"minimum ({step}n+{base}) at {fps:g} fps"); continue
        note, fit = "", None
        if src_frames % step != base:
            if a.on_non_grid == "refuse":
                errors.append(f"{p.name}: {src_frames} frames is non-{step}n+{base} "
                              "(--on-non-grid refuse)"); continue
            frames = _snap_frames(src_frames, up=(a.on_non_grid == "round-up"),
                                  grid=grid)
            fit = "pad" if frames > src_frames else "trim"
            note = (f"non-{step}n+{base} {src_frames} → {frames} frames "
                    f"({abs(src_frames - frames) / fps:.2f}s "
                    + ("padded" if fit == "pad" else "trimmed") + ")")
        else:
            frames = src_frames
        sidecar = segdir / (p.stem + ".txt")
        if sidecar.exists():
            prompt = sidecar.read_text()
        elif a.prompt_file:
            prompt = Path(a.prompt_file).read_text()
        else:
            errors.append(f"{p.name}: no {p.stem}.txt sidecar and no --prompt-file")
            continue
        if not prompt.strip():
            errors.append(f"{p.name}: empty prompt ({sidecar.name if sidecar.exists() else '--prompt-file'})")
            continue
        img = None
        if i == 0 and a.image:
            # explicit start image (CLI/UI) wins over a same-named sidecar
            img = Path(a.image).expanduser()
            if not img.exists():
                errors.append(f"--image not found: {img}"); continue
        else:
            for ext in (".png", ".jpg", ".jpeg", ".webp"):
                cand = segdir / (p.stem + ext)
                if cand.exists():
                    img = cand; break
        cfg_cand = segdir / (p.stem + ".json")
        cfg_delta = None
        if cfg_cand.exists():
            try:
                parsed = json.loads(cfg_cand.read_text())
                if not isinstance(parsed, dict):
                    errors.append(f"{cfg_cand.name}: JSON root must be an object {{...}}, got {type(parsed).__name__}")
                else:
                    cfg_delta = parsed
                    for k in ("width", "height"):
                        if cfg_delta.get(k) and cfg_delta[k] % 64:
                            print(f"warn: {cfg_cand.name}: {k}={cfg_delta[k]} is not a multiple of 64")
                    if man and man["fps"] and cfg_delta.get("fps") and float(man["fps"]) != float(cfg_delta["fps"]):
                        errors.append(f"{cfg_cand.name}: fps {cfg_delta['fps']} != manifest fps {man['fps']}")
            except json.JSONDecodeError as e:
                errors.append(f"{cfg_cand.name}: invalid JSON ({e})")
        items.append({"wav": p, "frames": frames, "prompt": prompt, "name": p.stem,
                      "note": note, "fit": fit, "image": img,
                      "cfg_file": cfg_cand.name if cfg_delta is not None else None,
                      "cfg_delta": cfg_delta})
    if errors:
        for e in errors:
            print("ERROR:", e, file=sys.stderr)
        sys.exit(f"batch refused: {len(errors)} problem(s) across {len(wavs)} segments — nothing queued")
    if man and man["orig"]:
        default_batch = Path(man["orig"]).stem
    else:
        default_batch = segdir.name
    batch = _norm_batch(a.batch or default_batch)
    seed = a.seed
    if seed is not None and seed < 0:
        print(f"seed {seed} is not a valid engine seed — random per segment")
        seed = None
    seed_extra = ["--seed", str(seed)] if seed is not None else []
    chain = "image" if a.chain else None
    if chain:
        print("chain: segments 2..N get the previous segment's last frame as "
              "--image (batch dispatch self-serializes)")
    tmp = HERE / "jobs" / "_tmp" / ("batch_" + uuid.uuid4().hex[:8])
    tmp.mkdir(parents=True, exist_ok=True)
    print(f"queueing {len(items)} job(s) as batch '{batch}'")
    active_overlay = {}
    active_src = None
    try:
        for i, it in enumerate(items):
            src = it["wav"]
            if it["fit"]:
                try:
                    src = _fit_wav(it["wav"], it["frames"], fps,
                                   tmp / it["wav"].name, it["fit"])
                except RuntimeError as e:
                    print(f"ERROR: {it['name']}: {e} — segment skipped", file=sys.stderr)
                    continue
            cfg_tag = ""
            if it["cfg_delta"] is not None:
                active_overlay.update(it["cfg_delta"])
                active_src = it["cfg_file"]
                cfg_tag = f"  [config: {it['cfg_file']}]"
            elif active_src:
                cfg_tag = f"  [config: from {active_src}]"

            job_cfg = dict(obj)
            if active_overlay:
                job_cfg.update(active_overlay)
            job_cfg["numFrames"] = it["frames"]
            assets = [("--audio", str(src))]
            if it["image"]:
                assets.append(("--image", str(it["image"])))
            job_fps = job_cfg.get("fps") or tfps
            jid = create_job(c, con, a.model, it["prompt"],
                             json.dumps(job_cfg, indent=2),
                             assets, list(seed_extra),
                             host=a.host, name=it["name"], ext=a.ext,
                             backend=a.backend, batch=batch, chain=chain if i else None,
                             num_frames=it["frames"], fps=job_fps)
            tag = "  [--image]" if it["image"] else ""
            if it["image"] and chain and i:
                tag += "  [chain skipped — manual --image wins]"
            tag += cfg_tag
            print(f"{jid}  {it['name']}  {it['frames']} frames{tag}"
                  + (f"  [{it['note']}]" if it["note"] else ""))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

# --- add-pairs: keyframe-pair batch composer (docs/expansion-of-this-idea.md Idea 2)

PAIR_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")

def _stem_number(stem):
    """(prefix, number) when the stem ends in digits, else None."""
    m = NATNUM.search(stem)
    return (m.group(1), int(m.group(2))) if m else None

def _as_frames(v):
    """A positive-int frame count from JSON/config input, or None."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n >= 1 else None

def _on_grid(n, grid):
    step, base, min_n = grid
    return (n - base) % step == 0 and n >= step * min_n + base

def _snap_nearest(raw, grid):
    """Snap to the nearest grid length; ties round up."""
    up = _snap_frames(raw, up=True, grid=grid)
    down = _snap_frames(raw, up=False, grid=grid)
    return up if raw - down >= up - raw else down

def _img_dims(path):
    """(width, height) of a still via ffprobe, or None when unreadable."""
    r = subprocess.run([ff_tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x",
                        str(path)], capture_output=True, text=True, timeout=60)
    if r.returncode != 0 or "x" not in r.stdout:
        return None
    try:
        w, h = r.stdout.strip().split("x")
        return int(w), int(h)
    except ValueError:
        return None

def pairs_strategy(c, con, host):
    """Which CLI spelling a keyframe pair composes as, implied by the pinned
    host's dialect: 'frame_slots' (--first-frame/--last-frame — both endpoints
    pinned, adjacent renders join exactly at the shared still, dispatch runs
    the fflf-preflight probe) or 'canvas_ref' (repeated --image: #1 is the
    canvas/start frame, #2 a moodboard reference of the target — the end frame
    is steered, not pinned, so seams are not exact). Unpinned jobs always
    compose as frame_slots, so a multi-image pair can never land on the
    dtcustom engine (upstream crash on more than one --image)."""
    if not host:
        return "frame_slots"
    hr = con.execute("SELECT * FROM hosts WHERE alias=?", (host,)).fetchone()
    if not hr:
        sys.exit(f"no such host: {host}")
    dialect = dialect_of(c, hr)
    spec = dialect_spec(dialect)
    if {"first_frame", "last_frame"} <= set(spec["flags"]):
        return "frame_slots"
    if "image" in spec["flags"]:
        return "canvas_ref"
    sys.exit(f"host {host} (dialect {dialect}) can express neither frame slots "
             "nor repeated --image — cannot compose keyframe pairs")

def _pair_assets(strategy, first, last, audio):
    """The per-dialect spelling of one keyframe pair (see pairs_strategy).
    A half-empty pair composes with the still it has: on frame_slots a missing
    last rides as --image (start-pinned i2v — a lone --first-frame is
    documented invalid), a missing first keeps a lone --last-frame; on
    canvas_ref the remaining still becomes the canvas image."""
    assets = []
    if strategy == "canvas_ref":
        if first:
            assets.append(("--image", str(first)))
        if last:
            assets.append(("--image", str(last)))
    elif first and last:
        assets += [("--first-frame", str(first)), ("--last-frame", str(last))]
    elif last:
        assets.append(("--last-frame", str(last)))
    elif first:
        assets.append(("--image", str(first)))
    if audio:
        assets.append(("--audio", str(audio)))
    return assets

def _dir_stills(segdir, order):
    """The ordered still spine of a folder. 'number' (default): images whose
    stem ends in digits — the number is RELATIVE ORDER only (natural sort:
    gaps, non-padded and shuffled files are fine; two files claiming one
    number are ambiguous and refuse). 'name': every image, alphabetically.
    Returns (stills, errors, warnings)."""
    errors, warnings = [], []
    images = sorted((p for p in segdir.iterdir()
                     if p.is_file() and p.suffix.lower() in PAIR_IMAGE_EXTS
                     and not p.name.startswith(".")), key=lambda p: p.name.lower())
    if order == "name":
        return images, errors, warnings
    numbered, by_num = [], {}
    for p in images:
        sn = _stem_number(p.stem)
        if not sn:
            warnings.append(f"ignored unnumbered image: {p.name}")
            continue
        prefix, num = sn
        if num in by_num:
            errors.append(f"two stills claim number {num}: {by_num[num].name} and "
                          f"{p.name} — the order is ambiguous, rename one")
            continue
        by_num[num] = p
        numbered.append((prefix, num, p))
    if not numbered:
        hint = ('  i=1; for f in *.png; do mv "$f" "image$(printf \'%04d\' $i).png"; '
                "i=$((i+1)); done")
        errors.append(f"no numbered stills in {segdir.name} — name stills "
                      "<prefix><number>.<ext> (any prefix, gaps fine) or pass "
                      f"--order name; quick rename:\n{hint}")
        return [], errors, warnings
    prefixes = sorted({prefix for prefix, _, _ in numbered})
    if len(prefixes) > 1:
        errors.append(f"mixed still prefixes ({', '.join(prefixes)}) — the numeric "
                      "order would be ambiguous; keep one prefix per folder")
    numbered.sort(key=lambda t: (t[1], t[2].name))
    return [p for _, _, p in numbered], errors, warnings

def _dir_companions(segdir, npairs):
    """Numbered companion sidecars keyed by pair index (1..npairs): the number
    decides, the stem prefix is free (prompt0001.txt, cfg1.json, aud0001.wav).
    Returns ({kind: {num: path}}, errors, warnings)."""
    kinds = {".txt": "prompt", ".json": "config", ".wav": "audio"}
    out = {"prompt": {}, "config": {}, "audio": {}}
    errors, warnings = [], []
    for p in sorted(segdir.iterdir(), key=lambda p: p.name.lower()):
        if p.name.startswith(".") or not p.is_file():
            continue
        kind = kinds.get(p.suffix.lower())
        if not kind:
            continue
        sn = _stem_number(p.stem)
        if not sn:
            warnings.append(f"ignored {p.suffix} file without a number: {p.name}")
            continue
        num = sn[1]
        if num < 1 or num > npairs:
            warnings.append(f"orphan {p.name}: no pair {num} (pairs are 1..{npairs})")
            continue
        if num in out[kind]:
            errors.append(f"two {kind} sidecars claim pair {num}: "
                          f"{out[kind][num].name} and {p.name}")
            continue
        out[kind][num] = p
    return out, errors, warnings

def pairs_plan_from_dir(segdir, order="number", prompt_text=None):
    """The model-independent layer of a pairs batch: the still palette, the
    consecutive pair list, per-pair companions and prompt texts. Returns
    (manifest, errors) — parse warnings ride in manifest['warnings']. Nothing
    here depends on model/fps/grid; lengths are derived later (pairs_resolve).
    The manifest doubles as the future --manifest input for external feeds."""
    stills, errors, warnings = _dir_stills(segdir, order)
    manifest = {"version": 1, "order": order, "batch": segdir.name,
                "stills": [p.name for p in stills], "pairs": [], "warnings": warnings}
    if not stills:
        return manifest, errors
    if len(stills) < 2:
        errors.append(f"need at least 2 stills for a pair batch (got {len(stills)})")
        return manifest, errors
    comp, cerr, cwarn = _dir_companions(segdir, len(stills) - 1)
    errors += cerr
    manifest["warnings"] += cwarn
    for k in range(1, len(stills)):
        pc = comp["prompt"].get(k)
        if pc is not None:
            prompt, src = pc.read_text(), pc.name
        elif prompt_text is not None:
            prompt, src = prompt_text, "shared"
        else:
            # no prompt yet: pairs_resolve refuses empty prompts at queue time,
            # so a dashboard session can be saved unfinished and fixed inline
            prompt, src = "", ""
        manifest["pairs"].append({
            "first": stills[k - 1].name, "last": stills[k].name,
            "prompt": prompt, "prompt_src": src,
            "config": comp["config"][k].name if k in comp["config"] else None,
            "audio": comp["audio"][k].name if k in comp["audio"] else None,
            "frames_override": None})
    return manifest, errors

def pairs_resolve(c, con, manifest, *, model, host=None, config_file=None,
                  config_json=None, frames_per_pair=None, on_non_grid="round-up",
                  base_dir=None):
    """The model-dependent layer: fps, frame grid, composition strategy, and
    each pair's frame count / wav fit / config overlay. Returns
    (res, errors, warnings); res['pairs'] aligns with manifest['pairs'] by
    index (None where the pair is broken — already reported in errors).
    Frame-length precedence: explicit frames_override (snapped to the grid) >
    audio sidecar (duration-derived, snapped, wav fitted) > that pair's config
    numFrames > --frames-per-pair > template numFrames. Soft gaps warn instead
    of blocking: an empty prompt, and a pair with one still missing (composed
    with the side it has — see _pair_assets). Blocking: a referenced still
    gone from disk, a pair with no stills at all, broken companions, or no
    frame-length source."""
    errors, warnings = [], []
    cfg_text, obj = _load_cfg(c, con, model, config_file, config_json)
    grid = frame_grid(model)
    step, base, min_n = grid
    tfps = obj.get("fps")
    fps = float(tfps) if tfps else None
    strategy = pairs_strategy(c, con, host)
    base_dir = Path(base_dir) if base_dir else Path(".")
    res = {"model": model, "fps": fps, "grid": f"{step}n+{base}",
           "grid_floor": step * min_n + base, "strategy": strategy,
           "cfg_obj": obj, "pairs": []}
    for k, pair in enumerate(manifest["pairs"], 1):
        notes = []
        if not str(pair.get("prompt") or "").strip():
            warnings.append(f"pair {k}: has no prompt — queueing anyway"
                            if not pair.get("prompt_src")
                            else f"pair {k}: empty prompt ({pair['prompt_src']})")
        first = base_dir / pair["first"] if pair.get("first") else None
        last = base_dir / pair["last"] if pair.get("last") else None
        gone = [tag for tag, p in (("first", first), ("last", last))
                if p and not p.exists()]
        if gone:
            errors.append(f"pair {k}: {' and '.join(gone)} still not found: "
                          f"{', '.join(pair[tag] for tag in gone)}")
            res["pairs"].append(None)
            continue
        if first is None and last is None:
            errors.append(f"pair {k}: has no stills — assign at least one "
                          "(a pair with neither side cannot render)")
            res["pairs"].append(None)
            continue
        if first is None or last is None:
            if strategy == "canvas_ref":
                warnings.append(f"pair {k}: one still missing — the remaining "
                                "still becomes the canvas image (start-pinned i2v)")
            elif first is None:
                warnings.append(f"pair {k}: no first still — composed as a lone "
                                "--last-frame (end pinned, start free; unvalidated "
                                "against the engine)")
            else:
                warnings.append(f"pair {k}: no last still — composed as --image "
                                "(start-pinned i2v; a lone --first-frame is invalid)")
        cfg_delta = None
        if pair["config"]:
            cp = base_dir / pair["config"]
            if not cp.exists():
                errors.append(f"pair {k}: config sidecar not found: {pair['config']}")
            else:
                try:
                    parsed = json.loads(cp.read_text())
                    if not isinstance(parsed, dict):
                        errors.append(f"pair {k}: {pair['config']}: JSON root must "
                                      "be an object {...}")
                    else:
                        cfg_delta = parsed
                        for dim in ("width", "height"):
                            if cfg_delta.get(dim) and cfg_delta[dim] % 64:
                                warnings.append(f"pair {k}: {pair['config']}: "
                                                f"{dim}={cfg_delta[dim]} is not a multiple of 64")
                        if fps and cfg_delta.get("fps") and float(cfg_delta["fps"]) != fps:
                            errors.append(f"pair {k}: {pair['config']}: fps "
                                          f"{cfg_delta['fps']} != template fps {tfps:g}")
                except json.JSONDecodeError as e:
                    errors.append(f"pair {k}: {pair['config']}: invalid JSON ({e})")
        audio_frames, fit = None, None
        if pair["audio"]:
            ap = base_dir / pair["audio"]
            if not ap.exists():
                errors.append(f"pair {k}: audio not found: {pair['audio']}")
            elif not fps:
                errors.append(f"pair {k}: {pair['audio']} needs a template fps to "
                              "derive its frame count")
            else:
                try:
                    dur = _wav_duration(ap)
                except (RuntimeError, ValueError) as e:
                    errors.append(f"pair {k}: {e}")
                else:
                    raw = round(dur * fps)
                    if raw < res["grid_floor"]:
                        errors.append(f"pair {k}: {pair['audio']}: {dur:.2f}s is under "
                                      f"the {res['grid_floor']}-frame minimum ({res['grid']}) "
                                      f"at {fps:g} fps")
                    elif raw % step != base:
                        if on_non_grid == "refuse":
                            errors.append(f"pair {k}: {pair['audio']}: {raw} frames is "
                                          f"non-{res['grid']} (--on-non-grid refuse)")
                        else:
                            audio_frames = _snap_frames(raw, up=(on_non_grid == "round-up"),
                                                        grid=grid)
                            fit = "pad" if audio_frames > raw else "trim"
                            notes.append(f"{pair['audio']}: {raw} → {audio_frames} frames "
                                         f"({abs(raw - audio_frames) / fps:.2f}s "
                                         f"{'padded' if fit == 'pad' else 'trimmed'})")
                    else:
                        audio_frames = raw
        if audio_frames is not None and cfg_delta is not None and \
                cfg_delta.get("numFrames") is not None and not pair["frames_override"]:
            cn = _as_frames(cfg_delta["numFrames"])
            if cn is not None and cn != audio_frames:
                errors.append(f"pair {k}: {pair['config']}: numFrames {cn} conflicts "
                              f"with audio-derived {audio_frames} — drop one of them")
        if pair["frames_override"]:
            frames = _snap_nearest(pair["frames_override"], grid)
            if frames != pair["frames_override"]:
                notes.append(f"length snapped to the {res['grid']} grid "
                             f"({pair['frames_override']} → {frames})")
            if audio_frames is not None and audio_frames != frames:
                notes.append(f"overriding audio-derived length ({audio_frames} → "
                             f"{frames}); the wav is "
                             f"{'trimmed' if frames < audio_frames else 'silence-padded'}")
                fit = "trim" if frames < audio_frames else "pad"
        elif audio_frames is not None:
            frames = audio_frames
        elif cfg_delta is not None and cfg_delta.get("numFrames") is not None:
            frames = _as_frames(cfg_delta["numFrames"])
            if frames is None:
                errors.append(f"pair {k}: {pair['config']}: invalid numFrames "
                              f"{cfg_delta['numFrames']!r}")
                res["pairs"].append(None)
                continue
            if not _on_grid(frames, grid):
                warnings.append(f"pair {k}: numFrames {frames} from {pair['config']} "
                                f"is off the {res['grid']} grid")
        elif frames_per_pair:
            frames = frames_per_pair
            if not _on_grid(frames, grid):
                warnings.append(f"pair {k}: --frames-per-pair {frames} is off the "
                                f"{res['grid']} grid")
        elif obj.get("numFrames") is not None:
            frames = _as_frames(obj["numFrames"])
            if frames is None:
                errors.append(f"pair {k}: template numFrames {obj['numFrames']!r} "
                              "is not a positive integer")
                res["pairs"].append(None)
                continue
            if not _on_grid(frames, grid):
                warnings.append(f"pair {k}: template numFrames {frames} is off the "
                                f"{res['grid']} grid")
        else:
            errors.append(f"pair {k}: no frame length source (audio sidecar, config "
                          "numFrames, --frames-per-pair, or template numFrames)")
            res["pairs"].append(None)
            continue
        for tag, p in (("first", first), ("last", last)):
            dims = _img_dims(p)
            if dims and obj.get("width") and obj.get("height") and \
                    tuple(dims) != (obj["width"], obj["height"]):
                warnings.append(f"pair {k}: {pair[tag]} is {dims[0]}x{dims[1]}, "
                                f"template is {obj['width']}x{obj['height']} — "
                                "the engine will resize")
        res["pairs"].append({"first": pair.get("first") or None,
                             "last": pair.get("last") or None,
                             "prompt": pair["prompt"], "prompt_src": pair["prompt_src"],
                             "config": pair["config"], "audio": pair["audio"],
                             "frames": frames, "fit": fit, "notes": notes,
                             "cfg_delta": cfg_delta})
    for k in range(2, len(manifest["pairs"]) + 1):
        prev, cur = manifest["pairs"][k - 2], manifest["pairs"][k - 1]
        if prev.get("last") and cur.get("first") and prev["last"] != cur["first"]:
            warnings.append(f"seam: pair {k - 1} ends at {prev['last']} but pair {k} "
                            f"starts at {cur['first']} — no shared still (a visible "
                            "cut, not a join)")
    used = {p[tag] for p in manifest["pairs"] for tag in ("first", "last") if p.get(tag)}
    unused = [n for n in manifest.get("stills", []) if n not in used]
    if unused:
        shown = ", ".join(unused[:5]) + (f" … +{len(unused) - 5} more"
                                         if len(unused) > 5 else "")
        warnings.append(f"{len(unused)} unused still(s): {shown}")
    return res, errors, warnings

def pairs_queue(c, con, manifest, res, *, model, host=None, ext="mov",
                backend=None, batch=None, seed=None, base_dir=None):
    """The shared queueing tail: one independent job per resolved pair (no
    chain — every pair already carries both keyframes, so the batch dispatches
    in parallel). The caller has validated: no errors in res/errors."""
    base_dir = Path(base_dir) if base_dir else Path(".")
    batch = _norm_batch(batch or manifest.get("batch"))
    if seed is not None and seed < 0:
        print(f"seed {seed} is not a valid engine seed — random per pair")
        seed = None
    seed_extra = ["--seed", str(seed)] if seed is not None else []
    strategy = res["strategy"]
    if strategy == "canvas_ref":
        print("pairs compose as canvas_ref (--image canvas + moodboard reference): "
              "the end frame is steered, not pinned — adjacent renders will not "
              "join exactly at the shared still")
    tmp = HERE / "jobs" / "_tmp" / ("pairsfit_" + uuid.uuid4().hex[:8])
    tmp.mkdir(parents=True, exist_ok=True)
    jids = []
    try:
        for k, (pair, v) in enumerate(zip(manifest["pairs"], res["pairs"]), 1):
            if v is None:
                continue                     # resolve already reported this pair
            audio_src = None
            if pair["audio"]:
                ap = base_dir / pair["audio"]
                if v["fit"]:
                    try:
                        audio_src = _fit_wav(ap, v["frames"], res["fps"],
                                             tmp / Path(pair["audio"]).name, v["fit"])
                    except RuntimeError as e:
                        print(f"ERROR: pair {k}: {e} — pair skipped", file=sys.stderr)
                        continue
                else:
                    audio_src = ap
            job_cfg = dict(res["cfg_obj"])
            if v["cfg_delta"]:
                job_cfg.update(v["cfg_delta"])
            job_cfg["numFrames"] = v["frames"]
            assets = _pair_assets(strategy,
                                  base_dir / pair["first"] if pair.get("first") else None,
                                  base_dir / pair["last"] if pair.get("last") else None,
                                  audio_src)
            jid = create_job(c, con, model, pair["prompt"],
                             json.dumps(job_cfg, indent=2), assets, list(seed_extra),
                             host=host, name=f"pair-{k:04d}", ext=ext,
                             backend=backend, batch=batch,
                             num_frames=v["frames"], fps=res["fps"])
            jids.append(jid)
            tag = "".join(f"  [{n}]" for n in v["notes"])
            print(f"{jid}  pair-{k:04d}  {pair.get('first') or '—'} → "
                  f"{pair.get('last') or '—'}  {v['frames']} frames{tag}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return jids

def cmd_add_pairs(a):
    c, con = conf(), db(); sync_hosts(con)
    try:
        return _cmd_add_pairs(c, con, a)
    finally:
        con.close()

def _cmd_add_pairs(c, con, a):
    """Keyframe-pair mode: one job per consecutive still pair in a directory.
    Model-first: the model/host choice fixes fps, the frame grid and the
    composition strategy (frame_slots vs canvas_ref); the gen list is planned
    against it. Prompts from numbered .txt sidecars falling back to
    --prompt-file. Validates the whole batch before queueing anything."""
    if ".." in a.model or a.model.startswith("/"):
        sys.exit("invalid --model: must be a model id, not a path with '..' or '/'")
    segdir = Path(a.dir).expanduser()
    if not segdir.is_dir():
        sys.exit(f"not a directory: {segdir}")
    prompt_text = Path(a.prompt_file).read_text() if a.prompt_file else None
    manifest, errors = pairs_plan_from_dir(segdir, order=a.order,
                                           prompt_text=prompt_text)
    res, r_errors, r_warnings = None, [], []
    if manifest["pairs"]:
        res, r_errors, r_warnings = pairs_resolve(
            c, con, manifest, model=a.model, host=a.host, config_file=a.config_file,
            config_json=a.config_json, frames_per_pair=a.frames_per_pair,
            on_non_grid=a.on_non_grid, base_dir=segdir)
    errors += r_errors
    head = f"pairs: {len(manifest['pairs'])} job(s) from {len(manifest['stills'])} stills"
    if res:
        head += f" — {res['grid']} grid"
        if res["fps"]:
            head += f", {res['fps']:g} fps"
    print(head)
    if res:
        print(f"composition: {res['strategy']} ({a.host or 'unpinned — any dtcustom host'})")
    for w in manifest["warnings"] + r_warnings:
        print(f"warn: {w}", file=sys.stderr)
    if errors:
        for e in errors:
            print("ERROR:", e, file=sys.stderr)
        sys.exit(f"batch refused: {len(errors)} problem(s) across "
                 f"{len(manifest['pairs'])} pairs — nothing queued")
    pairs_queue(c, con, manifest, res, model=a.model, host=a.host, ext=a.ext,
                backend=a.backend, batch=a.batch, seed=a.seed, base_dir=segdir)

def cmd_regen(a):
    con = db(); sync_hosts(con)
    try:
        return _cmd_regen(con, a)
    finally:
        con.close()

def _strip_flag_pair(toks, flag):
    out, skip = [], False
    for t in toks:
        if skip: skip = False; continue
        if t == flag: skip = True; continue
        out.append(t)
    return out

def _cmd_regen(con, a):
    p = con.execute("SELECT * FROM jobs WHERE id=?", (a.id,)).fetchone()
    if not p: sys.exit("no such job")
    pd = Path(p["local_dir"])
    cfg = pd / "config.json"
    if not cfg.exists(): cfg = pd / "config.txt"
    extra = json.loads(p["extra_args"] or "[]")
    if a.seed is not None or a.new_seed:
        extra = _strip_flag_pair(extra, "--seed")
    if a.frames is not None:
        extra = _strip_flag_pair(extra, "--frames")
    ns = argparse.Namespace(model=a.model or p["model"],
                            prompt_file=str(pd / "prompt.txt"), config_file=str(cfg),
                            config_json=a.config_json, host=a.host, name=a.name,
                            parent=p["id"], seed=a.seed, new_seed=a.new_seed,
                            frames=a.frames,
                            ext=a.ext, backend=a.backend or p["backend"],
                            chain=p["chain"], batch=a.batch or p["batch"],
                            upload=[],
                            extra_arg=[" ".join([t]) for t in extra])
    for k, _ in FLAGMAP: setattr(ns, k, None)
    ns.image = []              # the multi-valued slot: keep every image, in order
    for x in json.loads(p["assets"] or "[]"):
        fpath = pd / x["file"]
        if not fpath.exists(): continue
        flg = x.get("flag")
        matched = False
        if flg:
            for attr, f in FLAGMAP:
                if f == flg:
                    if attr == "image":
                        ns.image.append(str(fpath))
                    else:
                        setattr(ns, attr, str(fpath))
                    matched = True
                    break
        if not matched:
            ns.upload.append(str(fpath))
    cmd_add(ns)

def cmd_cancel(a):
    con = db(); sync_hosts(con)
    try:
        return _cmd_cancel(con, a)
    finally:
        con.close()

def _cmd_cancel(con, a):
    job = con.execute("SELECT * FROM jobs WHERE id=?", (a.id,)).fetchone()
    if not job: sys.exit("no such job")
    if job["status"] in ("done", "failed", "cancelled"):
        print("already terminal"); return
    if job["status"] == "queued" or not job["host"]:
        set_job(con, a.id, status="cancelled"); print("cancelled (was queued)"); return
    if (job["backend"] or "oneshot") == "serve":
        set_job(con, a.id, status="cancelling",
                note="serve: no per-job kill — request runs to completion; "
                     "result will be discarded")
        print("marked cancelling (serve backend)"); return
    set_job(con, a.id, status="cancelling")
    qrd = shlex.quote(job["remote_dir"])
    ssh(job["host"], f'pkill -TERM -P "$(cat {qrd}/cli_pid 2>/dev/null)" 2>/dev/null; '
                     f'kill "$(cat {qrd}/cli_pid 2>/dev/null)" 2>/dev/null; '
                     f'kill "$(cat {qrd}/pid 2>/dev/null)" 2>/dev/null; true')
    print("cancel requested — poll will mark it cancelled")

def engine_lock():
    """Non-blocking single-engine guard; returns fd to keep open or None."""
    import fcntl
    fd = open(HERE / "engine.lock", "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd.write(str(os.getpid())); fd.flush()
        return fd
    except OSError:
        fd.close(); return None

def cmd_run(a):
    c, con = conf(), db(); sync_hosts(con)
    lock = engine_lock()
    if not lock:
        sys.exit("another engine is running (ui or run) — see engine.lock holder")
    for j in con.execute("SELECT * FROM jobs WHERE status='uploading'"):
        set_job(con, j["id"], status="queued", note="engine restarted; re-upload")
    print(f"ltxq: polling every {c['poll_secs']}s — Ctrl-C to stop (use tmux/nohup)")
    last_gc = 0
    while True:
        try:
            now = time.time()
            if conf_changed():
                try:
                    c = conf()
                    sync_hosts(con)
                    print(f"hosts.yaml reloaded from {CONF_PATH}")
                except Exception as e:
                    con.rollback()
                    print(f"hosts.yaml reload failed ({e!r}) — keeping previous config")
            if now - last_gc > 600:
                gc.collect()
                clean_tmp()
                rotate_worker_logs(c, con)
                last_gc = now
            for st in ("running", "suspect", "cancelling"):
                for j in con.execute("SELECT * FROM jobs WHERE status=?", (st,)).fetchall():
                    if (j["backend"] or "oneshot") == "serve":
                        poll_serve_job(c, con, j)
                    else:
                        poll_job(c, con, j)
            for j in con.execute("SELECT * FROM jobs WHERE status='collecting'").fetchall():
                collect(c, con, j)
            dispatch(c, con)
        except KeyboardInterrupt:
            sys.exit(0)
        except Exception as e:
            print("loop error:", repr(e))
        time.sleep(c["poll_secs"])

def cmd_ls(a):
    con = db(); sync_hosts(con); now = int(time.time())
    for j in con.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 50"):
        el = (j["finished_at"] or now) - (j["started_at"] or j["created_at"])
        print(f"{j['id']}  {j['status']:<10} {(j['backend'] or '-'):<7} "
              f"{j['host'] or '-':<8} {j['pct']:>3}%  {el // 60:>4}m  "
              f"{j['name'][:40]}  {(j['note'] or '')[:60]}")

def cmd_probe(a):
    con, c = db(), conf(); sync_hosts(con)
    for h in con.execute("SELECT * FROM hosts WHERE enabled=1").fetchall():
        if not a.alias or h["alias"] == a.alias:
            probe(con, c, h["alias"])

def _models_dir(con, c, alias):
    h = con.execute("SELECT * FROM hosts WHERE alias=?", (alias,)).fetchone()
    if not h: sys.exit("unknown host " + alias)
    if not h["models_dir"]:
        probe(con, c, alias)
        h = con.execute("SELECT * FROM hosts WHERE alias=?", (alias,)).fetchone()
        if not h or not h["models_dir"]:
            sys.exit("models dir not found; set models_dir: in hosts.yaml")
    return h

def cmd_models(a):
    con, c = db(), conf(); sync_hosts(con)
    try:
        return _cmd_models(con, c, a)
    finally:
        con.close()

def _cmd_models(con, c, a):
    h = _models_dir(con, c, a.alias)
    dl = "" if a.catalog else "--downloaded-only "
    r = ssh(a.alias, f'{dollar_home(cli_of(c, h))} models list {dl}'
                     f'--offline --models-dir {shlex.quote(h["models_dir"])}', timeout=120)
    if r.returncode != 0:
        print(f"models list failed rc={r.returncode}: {r.stderr[-300:]}"); return
    hdr = MDIR_HDR.search(r.stdout)
    if hdr:
        same = hdr.group(1).strip().rstrip("/").lower() == h["models_dir"].rstrip("/").lower()
        print(f"resolved dir: {hdr.group(1).strip()}  ->  "
              f"{'MATCHES pin' if same else 'MISMATCH vs pin ' + h['models_dir']}")
    now = int(time.time())
    for m in ROW.finditer(r.stdout):
        mid, name, src, dnl, hf = m.groups()
        con.execute("INSERT OR REPLACE INTO registry VALUES(?,?,?,?,?,?,?)",
                    (a.alias, mid, name.strip(), src, dnl == "yes",
                     None if hf in (None, "-") else hf, now))
    con.commit()
    print(r.stdout.strip())

def cmd_stage(a):
    con, c = db(), conf(); sync_hosts(con)
    h = _models_dir(con, c, a.alias)
    dl = "" if a.allow_download else "--offline"
    print(f"ensure {a.model} on {a.alias} ({h['models_dir']})"
          f"{' [DOWNLOADS ALLOWED]' if a.allow_download else ' [offline]'}")
    r = ssh(a.alias, f'{dollar_home(cli_of(c, h))} models ensure '
                     f'--model {shlex.quote(a.model)} {dl} '
                     f'--models-dir {shlex.quote(h["models_dir"])}', timeout=3600)
    print(r.stdout[-2000:])
    if r.returncode != 0:
        sys.exit(f"ensure failed rc={r.returncode}: {r.stderr[-500:]}")

def cmd_check(a):
    con, c = db(), conf(); sync_hosts(con)
    job = con.execute("SELECT * FROM jobs WHERE id=?", (a.id,)).fetchone()
    if not job: sys.exit("no such job")
    alias = a.host or job["host"]
    if not alias: sys.exit("job has no host yet; pass --host")
    h = _models_dir(con, c, alias)
    have = set(ssh(alias, f"ls -1 {shlex.quote(h['models_dir'])} 2>/dev/null").stdout.split())
    obj = json.loads(job["config_text"] or "{}")
    need = [job["model"]] + [l.get("file") for l in obj.get("loras", []) if l.get("file")]
    missing = []
    for f in need:
        if not f.endswith(".ckpt"):
            print("(skip — not a file id) ", f); continue
        ok = f in have
        print(("ok      " if ok else "MISSING ") + f)
        if not ok: missing.append(f)
    sys.exit(1 if missing else 0)

def cmd_doctor(a):
    print("ltxq doctor")
    con, c = db(), conf(); sync_hosts(con)
    ok = True
    def chk(label, cond, detail=""):
        nonlocal ok
        print(f"  [{'ok' if cond else 'FAIL'}] {label}" +
              (f" — {detail}" if detail and not cond else ""))
        ok = ok and cond
    jid = "doctest0" + uuid.uuid4().hex[:4]
    con.execute("INSERT INTO jobs(id,created_at,status,name) VALUES(?,?,?,?)",
                (jid, int(time.time()), "queued", "doctor"))
    con.commit()
    before = con.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0]
    set_job(con, jid, pct=1)
    marked = con.execute("SELECT COUNT(*) FROM jobs WHERE status='queued' AND pct=1"
                         ).fetchone()[0]
    con.execute("DELETE FROM jobs WHERE id=?", (jid,)); con.commit()
    chk("set_job updates only its own row", marked == 1,
        f"{marked}/{before} queued rows touched" if marked != 1 else "")
    for h in con.execute("SELECT * FROM hosts WHERE enabled=1").fetchall():
        r = ssh(h["alias"], "echo ping")
        chk(f"{h['alias']}: ssh reachable", r.returncode == 0,
            r.stderr.strip()[:120])
        pr = ssh(h["alias"],
                 'tail -c +1 /dev/null; echo $((3+1))')
        chk(f"{h['alias']}: remote shell arithmetic ok", "4" in pr.stdout,
            "remote login shell may not be bash/zsh-compatible")
        cr = ssh(h["alias"],
                 f'test -x "{dollar_home(cli_of(c, h))}" && echo CLIOK')
        chk(f"{h['alias']}: tod-cli present ({cli_of(c, h)})", "CLIOK" in cr.stdout,
            "not found or not executable — check cli_path in hosts.yaml")
        if _worker_alive(h):
            chk(f"{h['alias']}: serve worker running", True)
        else:
            print(f"  [ --] {h['alias']}: serve worker not running (ok unless "
                  "serve jobs queued)")
    try:
        importlib.metadata.version("flask")
        print("  [ok] flask installed")
    except importlib.metadata.PackageNotFoundError:
        print("  [FAIL] flask not installed — `ui` unavailable"); ok = False
    for tool in ("ffmpeg", "ffprobe"):
        try:
            ff_tool(tool)
            print(f"  [ok] {tool} available")
        except RuntimeError as e:
            chk(f"{tool} available", False, str(e))
    print("doctor:", "PASS" if ok else "FAIL")

def _gen_flags(help_text):
    flags = set()
    for m in GENOPT.finditer(help_text):
        flags.update(p for p in m.group(1).split("/") if p.startswith("--"))
    return flags

def _snap_path(dialect):
    """Committed `generate --help` option snapshot for one dialect."""
    return HERE / "docs" / f"generate_flags.{dialect}.txt"

def cmd_flags(a):
    """Per-dialect engine-CLI drift check.

    Runs `generate --help` on each host over the existing transport, groups the
    hosts by their `cli_dialect`, and diffs each dialect's option set against
    its committed snapshot (`docs/generate_flags.<dialect>.txt`). Hosts on
    DIFFERENT dialects are never reported as skew against each other — skew is
    only meaningful within one dialect (an older binary of the same dialect).
    Cross-dialect option differences are printed informationally. See
    docs/cli-dialects.md."""
    con, c = db(), conf(); sync_hosts(con)
    rows = con.execute("SELECT * FROM hosts WHERE enabled=1").fetchall()
    if a.aliases:
        byname = {h["alias"]: h for h in rows}
        missing = [x for x in a.aliases if x not in byname]
        if missing: sys.exit(f"no such enabled host(s): {', '.join(missing)}")
        rows = [byname[x] for x in a.aliases]
    if not rows: sys.exit("no enabled hosts")
    by_dialect, unions = {}, {}
    for h in rows:
        d = dialect_of(c, h)
        cli = shlex.quote(dollar_home(cli_of(c, h)))
        r = ssh(h["alias"], f"{cli} {dialect_spec(d)['generate_cmd']} --help",
                timeout=60)
        # the USAGE header distinguishes real help from a stub that ignores
        # --help and starts generating
        if r.returncode != 0 or "USAGE" not in r.stdout:
            print(f"{h['alias']} [{d}]: no help output "
                  f"({r.stderr.strip()[:120] or 'empty'})")
            continue
        fl = _gen_flags(r.stdout)
        by_dialect.setdefault(d, {})[h["alias"]] = fl
        print(f"{h['alias']} [{d}]: {len(fl)} options")
    if not by_dialect: sys.exit(1)
    drift = False
    for d, hosts in sorted(by_dialect.items()):
        union = set().union(*hosts.values())
        unions[d] = union
        for alias, fl in sorted(hosts.items()):
            skew = union - fl
            if skew:
                print(f"{alias} [{d}]: host skew — missing {', '.join(sorted(skew))}")
                drift = True
        snap_path = _snap_path(d)
        snap = set()
        if snap_path.exists():
            snap = {l.strip() for l in snap_path.read_text().splitlines()
                    if l.strip() and not l.startswith("#")}
        else:
            print(f"note: no snapshot yet at {snap_path.relative_to(HERE)}")
        new, gone = sorted(union - snap), sorted(snap - union)
        for f in new:  print(f"[{d}] NEW    ", f)
        for f in gone: print(f"[{d}] GONE   ", f)
        drift = drift or bool(new or gone)
        if a.update:
            snap_path.write_text(
                f"# {d} `generate --help` option snapshot for `ltxq flags`.\n"
                "# One long option per line; combined --x/--no-x forms are stored split.\n"
                f"# Updated {time.strftime('%Y-%m-%d')} from: "
                f"{', '.join(sorted(hosts))}\n"
                + "\n".join(sorted(union)) + "\n")
            print(f"[{d}] snapshot updated: {snap_path.relative_to(HERE)} "
                  f"({len(union)} options)")
    if len(unions) > 1:
        allf = set().union(*unions.values())
        common = set.intersection(*unions.values())
        diff = allf - common
        print("cross-dialect (informational) — differs between dialects: "
              + (", ".join(sorted(diff)) if diff else "none"))
    print("flags:", "DRIFT" if drift else "in sync with snapshot(s)")
    sys.exit(1 if drift else 0)

def cmd_ui(a):
    sys.path.insert(0, str(HERE))
    import server
    server.run_ui(a)

def cmd_reconcile(a):
    con, c = db(), conf(); sync_hosts(con)
    clean_tmp()
    rotate_worker_logs(c, con)
    known = {r[0] for r in con.execute("SELECT id FROM jobs")}
    for h in con.execute("SELECT * FROM hosts WHERE enabled=1").fetchall():
        if not h["home"]:
            probe(con, c, h["alias"])
            h = con.execute("SELECT * FROM hosts WHERE alias=?", (h["alias"],)).fetchone()
        rd = f"{h['home']}/{c['remote_root']}/jobs"
        unk = [x for x in ssh(h["alias"], f"ls -1 {shlex.quote(rd)} 2>/dev/null").stdout.split()
               if x not in known]
        print(h["alias"], "unknown remote job dirs:", unk or "none")

def cmd_serve_start(a):
    con, c = db(), conf(); sync_hosts(con)
    try:
        return _cmd_serve_start(con, c, a)
    finally:
        con.close()

def _cmd_serve_start(con, c, a):
    h = _models_dir(con, c, a.alias)
    if not dialect_has(dialect_spec(dialect_of(c, h)), "serve"):
        sys.exit(f"{a.alias}: dialect {dialect_of(c, h)!r} has no 'serve' subcommand "
                 "— serve workers are unsupported on this host")
    if not h["home"]:
        probe(con, c, a.alias)
        h = con.execute("SELECT * FROM hosts WHERE alias=?", (a.alias,)).fetchone()
    if _worker_alive(h): print(f"{a.alias}: worker already running"); return
    w = wdir(h)
    qw = shlex.quote(w)
    # v1.4.1: plain-pipe stdin instead of PTY wrapper (PTY breaks serve's
    # newline-delimited JSON protocol); rotate worker.log before boot so we
    # only ever scan bytes written by this worker.
    boot = (f"mkdir -p {qw} && cd {qw} || exit 1; mkdir -p reqs; "
            f"[ -p fifo ] || mkfifo fifo; "
            f'kill "$(cat holder.pid 2>/dev/null)" 2>/dev/null; true; '
            f'kill "$(cat worker.pid 2>/dev/null)" 2>/dev/null; true; '
            f"mv worker.log worker.log.prev 2>/dev/null; "
            f"nohup {shlex.quote(dollar_home(cli_of(c, h)).replace('$HOME', h['home'], 1))} serve "
            f"< fifo >> worker.log 2>&1 & echo $! > worker.pid; "
            f"nohup sleep 100000 > fifo 2>/dev/null & echo $! > holder.pid")
    r = ssh(a.alias, boot, timeout=30)
    if r.returncode != 0:
        print(f"{a.alias}: boot script failed rc={r.returncode}: "
              f"{(r.stderr or r.stdout).strip()[:300]}"); return
    base = 0
    out = ""
    for _ in range(15):
        time.sleep(2)
        out = ssh(a.alias,
                  f'cat {qw}/worker.log 2>/dev/null; '
                  f'echo ---POST; kill -0 "$(cat {qw}/worker.pid 2>/dev/null)" '
                  f'2>/dev/null && echo ALIVE || echo DEAD')
        if '"ready"' in out.stdout:
            print(f"{a.alias}: serve worker ready ({w})"); return
    post = out.stdout.split("---POST", 1)[-1].strip()
    if post == "DEAD":
        print(f"{a.alias}: worker died at startup (error should be at top of worker.log)")
    else:
        print(f"{a.alias}: no ready event in 30s (worker process still alive)")
    print(f"— state: worker.pid=$(cat {qw}/worker.pid 2>/dev/null) "
          f"holder.pid=$(cat {qw}/holder.pid 2>/dev/null)")
    t = ssh(a.alias, f"tail -c 800 {qw}/worker.log 2>/dev/null").stdout.strip()
    if t: print(f"— worker.log:\n{t}")

def cmd_serve_stop(a):
    con, c = db(), conf(); sync_hosts(con)
    h = con.execute("SELECT * FROM hosts WHERE alias=?", (a.alias,)).fetchone()
    if not h: sys.exit("unknown host")
    busy = con.execute("SELECT id FROM jobs WHERE host=? AND backend='serve' AND status IN "
                       "('running','uploading','suspect','cancelling')",
                       (a.alias,)).fetchone()
    if busy and not a.force:
        sys.exit(f"serve job {busy[0]} in flight; shutdown mid-job semantics unspecified "
                 f"— --force to proceed")
    ssh(a.alias, f"echo {SHUTDOWN} > {shlex.quote(wdir(h))}/fifo")
    print("shutdown sent (holder dies on next serve-start); verify via worker.log")

def main():
    p = argparse.ArgumentParser(prog="ltxq")
    sp = p.add_subparsers(dest="cmd", required=True)
    g = sp.add_parser("add"); g.add_argument("--model", required=True)
    g.add_argument("--prompt-file", required=True); g.add_argument("--config-file")
    g.add_argument("--config-json"); g.add_argument("--host")
    g.add_argument("--name"); g.add_argument("--parent", help=argparse.SUPPRESS)
    g.add_argument("--seed", type=int); g.add_argument("--new-seed", action="store_true")
    g.add_argument("--frames", type=int,
                   help="engine frame count, passed through as --frames <n> "
                        "(overrides the config's numFrames)")
    g.add_argument("--ext", default="mov", choices=["mov", "mp4", "png"])
    g.add_argument("--backend", choices=["oneshot", "serve"])
    g.add_argument("--image", action="append", default=[],
                   help="input image; repeat for extra reference images — order "
                        "matters (the first is the primary/canvas image and later "
                        "ones are ordered references)")
    for f in ("--audio", "--first-frame", "--middle-frame",
              "--last-frame", "--input-video"):
        g.add_argument(f)
    g.add_argument("--upload", action="append", default=[])
    g.add_argument("--extra-arg", action="append", default=[],
                   help='raw tokens, e.g. --extra-arg "--keyframe kf.png:12:0.8"')
    g.add_argument("--chain", choices=["first_frame", "image"],
                   help="continue from the previous generation: at launch time, "
                        "attach the last frame of the newest finished render "
                        "to this slot")
    g.add_argument("--batch", help="group label (e.g. moodboard-v1); 'chain' then "
                                   "scopes to the previous finished job in this batch")
    g = sp.add_parser("add-batch", help="queue one job per .wav segment in a directory "
                                        "(frames from the cutting helper's manifest, or ffprobe)")
    g.add_argument("dir")
    g.add_argument("--model", required=True)
    g.add_argument("--prompt-file",
                   help="shared fallback prompt for segments without a .txt sidecar")
    g.add_argument("--config-file"); g.add_argument("--config-json")
    g.add_argument("--host"); g.add_argument("--seed", type=int)
    g.add_argument("--ext", default="mov", choices=["mov", "mp4", "png"])
    g.add_argument("--backend", choices=["oneshot", "serve"])
    g.add_argument("--batch", help="group label (default: the manifest's Original File "
                                   "stem, else the directory name)")
    g.add_argument("--manifest",
                   help="explicit manifest path (default: auto-detect the single "
                        "header-bearing .txt in the segment dir)")
    g.add_argument("--on-non-grid", choices=["round-up", "round-down", "refuse"],
                   default="round-up",
                   help="a segment whose frame count is not on the model's frame grid: round up and pad "
                        "the wav with silence (default), round down and trim it, "
                        "or refuse the batch")
    g.add_argument("--chain", action="store_true",
                   help="visual continuity: segments 2..N get the previous "
                        "segment's last frame attached as --image at launch time "
                        "(first-frame alone is invalid — it only pairs with "
                        "--last-frame); batch-chained jobs defer dispatch until "
                        "the rest of the batch is done, so the batch runs serially")
    g.add_argument("--image",
                   help="start image attached as --image to the first segment "
                        "(the chain anchor); overrides a same-named sidecar image. "
                        "Same-named <stem>.png/jpg/jpeg/webp files next to the wavs "
                        "are always picked up as per-segment --image")
    g = sp.add_parser("add-pairs", help="queue one job per consecutive still pair "
                                        "in a directory (keyframe interpolation: "
                                        "first frame → last frame)")
    g.add_argument("dir")
    g.add_argument("--model", required=True)
    g.add_argument("--prompt-file",
                   help="shared fallback prompt for pairs without a numbered .txt sidecar")
    g.add_argument("--config-file"); g.add_argument("--config-json")
    g.add_argument("--host", help="pin a host; its dialect picks the composition "
                                  "strategy (dtcustom: --first-frame/--last-frame; "
                                  "dtofficial: repeated --image)")
    g.add_argument("--seed", type=int)
    g.add_argument("--ext", default="mov", choices=["mov", "mp4", "png"])
    g.add_argument("--backend", choices=["oneshot", "serve"])
    g.add_argument("--batch", help="group label (default: the directory name)")
    g.add_argument("--order", choices=["number", "name"], default="number",
                   help="still order: embedded number (default — gaps, non-padded "
                        "and shuffled files are fine) or plain alphabetical name")
    g.add_argument("--frames-per-pair", type=int,
                   help="uniform frame count for pairs without their own length "
                        "source (an audio sidecar or config numFrames wins; the "
                        "template's numFrames loses)")
    g.add_argument("--on-non-grid", choices=["round-up", "round-down", "refuse"],
                   default="round-up",
                   help="an audio sidecar off the model's frame grid: round up and "
                        "silence-pad the wav (default), round down and trim it, or "
                        "refuse the batch")
    g = sp.add_parser("regen"); g.add_argument("id")
    g.add_argument("--model"); g.add_argument("--config-json"); g.add_argument("--host")
    g.add_argument("--name"); g.add_argument("--seed", type=int)
    g.add_argument("--new-seed", action="store_true")
    g.add_argument("--frames", type=int,
                   help="engine frame count override (replaces the original --frames)")
    g.add_argument("--ext", default="mov", choices=["mov", "mp4", "png"])
    g.add_argument("--backend", choices=["oneshot", "serve"])
    g.add_argument("--batch", help="override the group label (default: keep the original)")
    sp.add_parser("ls"); sp.add_parser("run"); sp.add_parser("reconcile")
    sp.add_parser("doctor")
    g = sp.add_parser("ui"); g.add_argument("--port", type=int, default=8765)
    g.add_argument("--no-engine", action="store_true",
                   help="serve UI without the run loop (external `run` in tmux)")
    g = sp.add_parser("probe"); g.add_argument("alias", nargs="?")
    g = sp.add_parser("models"); g.add_argument("alias"); g.add_argument("--catalog",
                                                                        action="store_true")
    g = sp.add_parser("stage"); g.add_argument("alias"); g.add_argument("model")
    g.add_argument("--allow-download", action="store_true")
    g = sp.add_parser("check"); g.add_argument("id"); g.add_argument("--host")
    g = sp.add_parser("flags", help="diff each host's engine-CLI generate options "
                                    "against its dialect's snapshot "
                                    "(docs/generate_flags.<dialect>.txt)")
    g.add_argument("aliases", nargs="*")
    g.add_argument("--update", action="store_true",
                   help="rewrite the snapshot from what the hosts report")
    g = sp.add_parser("cancel"); g.add_argument("id")
    g = sp.add_parser("serve-start"); g.add_argument("alias")
    g = sp.add_parser("serve-stop"); g.add_argument("alias")
    g.add_argument("--force", action="store_true")
    args = p.parse_args()
    {"add": cmd_add, "add-batch": cmd_add_batch, "add-pairs": cmd_add_pairs,
     "ls": cmd_ls, "run": cmd_run,
     "cancel": cmd_cancel,
     "regen": cmd_regen, "probe": cmd_probe, "reconcile": cmd_reconcile,
     "models": cmd_models, "check": cmd_check, "stage": cmd_stage,
     "flags": cmd_flags,
     "serve-start": cmd_serve_start, "serve-stop": cmd_serve_stop,
     "doctor": cmd_doctor, "ui": cmd_ui}[args.cmd](args)

if __name__ == "__main__":
    main()
