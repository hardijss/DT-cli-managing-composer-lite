"""ltxq M2 web UI — single process running the engine loop + dashboard API.

Run: ./venv/bin/python ltxq.py ui [--port 8765] [--no-engine]
"""
import argparse, contextlib, gc, io, json, re, shlex, shutil, subprocess, sys, tempfile, threading, time, uuid
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

import flask
import yaml

import ltxq

HERE = ltxq.HERE
STATIC = HERE / "static"

# job ids are uuid4().hex[:10]; anything else (esp. "..", "/") is not a job
JID_RE = re.compile(r"[0-9a-f]{10}")
MAX_UPLOAD = 4 * 1024**3          # cap multipart bodies (inputs are media files)

STATE = {
    "engine": False,            # engine thread running
    "port": None,               # port the dashboard listens on (set by run_ui)
    "stop": threading.Event(),
    "idle": {},                 # alias -> {busy, reason, busy_since, released}
    "hosts": {},                # alias -> {worker_alive} refreshed by engine
    "lock": threading.Lock(),
    "host_paused": {},          # alias -> {paused: bool, release_at: int|None}
}


# ---------------------------------------------------------------- event stream
# In-process fan-out for GET /api/events (SSE). Each subscriber gets its own
# bounded queue; publishers never block (oldest events are dropped on
# overflow and a `refresh` event asks the client to re-fetch /api/state).
# There is no replay: on (re)connect the client gets a `hello` snapshot.

class _EventBus:
    def __init__(self, maxlen=1000):
        self._cv = threading.Condition()
        self._subs = {}                     # token -> deque of (event, data)
        self._maxlen = maxlen

    def subscribe(self):
        token = object()
        with self._cv:
            self._subs[token] = deque(maxlen=self._maxlen)
        return token

    def unsubscribe(self, token):
        with self._cv:
            self._subs.pop(token, None)

    def publish(self, event, data):
        with self._cv:
            for q in self._subs.values():
                if len(q) >= q.maxlen:
                    q.clear()
                    q.append(("refresh", {"reason": "overflow"}))
                q.append((event, data))
            self._cv.notify_all()

    def pop(self, token, timeout=15.0):
        """Pop the oldest queued event, or None after `timeout` seconds idle."""
        with self._cv:
            q = self._subs.get(token)
            if q is None:
                return None
            if not q:
                self._cv.wait(timeout)
                q = self._subs.get(token)   # unsubscribed while waiting
                if q is None or not q:
                    return None
            return q.popleft()


EVENTS = _EventBus()


def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# ---------------------------------------------------------------- idle policy

def _policy(h, c):
    pol = (c.get("idle_policy") or {})
    for extra in c.get("hosts", []):
        if extra.get("alias") == h["alias"]:
            pol = {**pol, **(extra.get("idle_policy") or {})}
    return pol


def _host_busy(h, pol):
    """(busy, reason) — load average above threshold or heavy app running."""
    pats = "|".join(pol.get("heavy_processes") or [])
    r = ltxq.ssh(h["alias"],
                 "uptime; " + (f"pgrep -ifl {shlex.quote(pats)} 2>/dev/null || true"
                               if pats else "true"))
    if r.returncode != 0:
        return False, ""
    load, hits = 0.0, []
    for line in r.stdout.splitlines():
        if "load average" in line:             # macOS: "... load averages: 1.46 1.01 0.72"
            try:
                load = float(line.split("load average")[1].lstrip("s: ").split()[0])
            except (ValueError, IndexError):
                pass
        elif line.strip() and line[0].isdigit():
            hits.append(line.split(maxsplit=1)[-1].strip())
    max_load = float(pol.get("max_load") or 0)
    if hits:
        return True, "heavy process: " + ", ".join(hits[:3])
    if max_load and load > max_load:
        return True, f"load {load:.1f} > {max_load:g}"
    return False, ""


def idle_gate(c, con, h):
    """Return True if dispatch to this host is allowed right now.
    Also handles auto serve-stop when the host stays busy, and auto
    serve-start again when it goes idle with queued serve jobs."""
    pol = _policy(h, c)
    if not pol.get("enabled"):
        return True
    st = STATE["idle"].setdefault(h["alias"], {})
    queued = con.execute("SELECT COUNT(*) FROM jobs WHERE status='queued' AND "
                         "(host IS NULL OR host=?)", (h["alias"],)).fetchone()[0]
    if not queued:
        st.update(busy=False, reason="")
        return True
    busy, reason = _host_busy(h, pol)
    now = int(time.time())
    if busy:
        if not st.get("busy"):
            st.update(busy=True, reason=reason, busy_since=now, released=False)
            print(f"idle-policy: {h['alias']} paused — {reason}")
        st["reason"] = reason
        release = int(pol.get("pause_release_secs") or 300)
        if (now - st.get("busy_since", now) >= release and not st.get("released")
                and ltxq._worker_alive(h)):
            w = ltxq.shlex.quote(ltxq.wdir(h))
            ltxq.ssh(h["alias"], f"echo {ltxq.SHUTDOWN} > {w}/fifo")
            st["released"] = True
            print(f"idle-policy: {h['alias']} busy {release}s+ — serve worker "
                  "stopped to free memory")
        return False
    if st.get("busy"):
        print(f"idle-policy: {h['alias']} idle again — dispatching")
    st.update(busy=False, reason="")
    if st.pop("released", False) and queued:
        # we stopped the worker; bring it back before dispatching
        with contextlib.redirect_stdout(io.StringIO()):
            ltxq.cmd_serve_start(argparse.Namespace(alias=h["alias"]))
        print(f"idle-policy: {h['alias']} serve worker restarted")
    return True


# --------------------------------------------------------------- engine loop

def _dispatch_allowed(alias):
    """Return True if dispatching to this host is currently permitted.
    Auto-clears a scheduled release when the release time has passed."""
    st = STATE["host_paused"].get(alias)
    if not st or not st.get("paused"):
        return True
    release_at = st.get("release_at")
    if release_at and time.time() >= release_at:
        st["paused"] = False
        st["release_at"] = None
        print(f"[queue] {alias}: scheduled release triggered — dispatch resumed")
        return True
    return False


def _reload_conf(con):
    """Re-read hosts.yaml after an on-disk change and re-sync the hosts table.
    Keeps the previous config on parse/validation errors so a half-saved file
    can't kill dispatch. Returns the new conf, or None to keep the old one."""
    try:
        c = ltxq.conf()
        ltxq.sync_hosts(con)
        print(f"[engine] hosts.yaml reloaded from {ltxq.CONF_PATH}")
        _publish_hosts()
        return c
    except Exception as e:
        con.rollback()
        print(f"[engine] hosts.yaml reload failed ({e!r}) — keeping previous config")
        return None


def engine_loop():
    c, con = ltxq.conf(), ltxq.db(); ltxq.sync_hosts(con)
    STATE["engine"] = True
    EVENTS.publish("engine", {"engine": True})
    for j in con.execute("SELECT * FROM jobs WHERE status='uploading'"):
        ltxq.set_job(con, j["id"], status="queued", note="engine restarted; re-upload")
    print(f"[engine] polling every {c['poll_secs']}s")
    for tool in ("ffmpeg", "ffprobe"):
        try:
            ltxq.ff_tool(tool)
        except RuntimeError as e:
            print(f"[engine] WARNING: {e} — frame extraction / chain "
                  "continuation will fail (generation is unaffected)")
    last_gc = 0
    while not STATE["stop"].is_set():
        try:
            now = time.time()
            if ltxq.conf_changed():
                if (new_c := _reload_conf(con)) is not None:
                    c = new_c
            if now - last_gc > 600:
                gc.collect()
                ltxq.clean_tmp()
                ltxq.rotate_worker_logs(c, con)
                last_gc = now
            for st in ("running", "suspect", "cancelling"):
                for j in con.execute("SELECT * FROM jobs WHERE status=?", (st,)).fetchall():
                    if (j["backend"] or "oneshot") == "serve":
                        ltxq.poll_serve_job(c, con, j)
                    else:
                        ltxq.poll_job(c, con, j)
            for j in con.execute("SELECT * FROM jobs WHERE status='collecting'").fetchall():
                ltxq.collect(c, con, j)
            for h in con.execute("SELECT * FROM hosts WHERE enabled=1").fetchall():
                if not _dispatch_allowed(h["alias"]):
                    continue
                if not idle_gate(c, con, h):
                    continue
                busy = con.execute("SELECT COUNT(*) FROM jobs WHERE host=? AND status IN "
                                   "('uploading','running','collecting','suspect','cancelling')",
                                   (h["alias"],)).fetchone()[0]
                if busy >= h["max_jobs"]:
                    continue
                # Keep the dashboard scheduler aligned with the headless
                # dispatcher: batch-scoped chained jobs wait for active
                # siblings, so their continuation frame is deterministic.
                job = ltxq.next_dispatchable_job(con, h["alias"], c, h)
                if job:
                    cur = con.execute("UPDATE jobs SET status='uploading', host=? "
                                      "WHERE id=? AND status='queued'", (h["alias"], job["id"]))
                    con.commit()
                    if cur.rowcount:
                        EVENTS.publish("job", {"job": jrow(con.execute(
                            "SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone())})
                        ltxq.launch(c, con, h, job)
            # Surface queued jobs no enabled host's dialect can run.
            ltxq.note_unroutable(c, con)
            try:
                new_hosts = host_states(con)
                if new_hosts != STATE["hosts"]:
                    STATE["hosts"] = new_hosts
                    EVENTS.publish("host", {"hosts": _hosts_view(con)})
            except Exception as e:
                print("[engine] host_states error:", repr(e))
        except Exception as e:
            print("[engine] loop error:", repr(e))
        STATE["stop"].wait(c["poll_secs"])
    con.close()
    STATE["engine"] = False
    EVENTS.publish("engine", {"engine": False})
    print("[engine] stopped")


def host_states(con):
    """Lightweight per-host snapshot for the UI: worker liveness (only when a
    worker dir exists) and in-flight queue depth."""
    out = {}
    for h in con.execute("SELECT * FROM hosts WHERE enabled=1"):
        d = {}
        if ltxq.is_local(h["alias"]):
            d["conn"] = "local"
        else:
            d["conn"] = "ssh"
        wdirp = ltxq.wdir(h)
        r = ltxq.ssh(h["alias"],
                     f'test -f {shlex.quote(wdirp)}/worker.pid && '
                     f'kill -0 "$(cat {shlex.quote(wdirp)}/worker.pid)" 2>/dev/null '
                     f'&& echo ALIVE || echo DEAD', timeout=15)
        d["worker_alive"] = "ALIVE" in r.stdout
        d["in_flight"] = con.execute(
            "SELECT COUNT(*) FROM jobs WHERE host=? AND status IN "
            "('uploading','running','collecting','suspect','cancelling')",
            (h["alias"],)).fetchone()[0]
        d["queued"] = con.execute(
            "SELECT COUNT(*) FROM jobs WHERE status='queued' AND "
            "(host IS NULL OR host=?)", (h["alias"],)).fetchone()[0]
        d["cli_path"] = ltxq.cli_of(ltxq.conf(), h)
        d["cli_dialect"] = ltxq.dialect_of(ltxq.conf(), h)
        d["frame_roles"] = ltxq.dialect_has(
            ltxq.dialect_spec(d["cli_dialect"]), "first_frame")
        out[h["alias"]] = d
    return out


# ------------------------------------------------------------------ flask app

app = flask.Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD


@app.before_request
def _csrf_guard():
    """Reject cross-origin POSTs (browsers always send Origin on cross-site
    requests). Non-browser clients (curl) send no Origin and are unaffected;
    the dashboard binds to 127.0.0.1, so the browser is the only remote-ish
    client to worry about."""
    if flask.request.method != "POST":
        return None
    origin = flask.request.headers.get("Origin", "")
    if origin and urlparse(origin).netloc.lower() != \
            (flask.request.host or "").lower():
        return flask.jsonify(error="cross-origin request rejected"), 403
    return None


def jid_ok(jid):
    return bool(JID_RE.fullmatch(jid or ""))


def safe_upload_name(fn):
    """Basename of a client-supplied filename — multipart filenames may carry
    '/' or '..' (curl sends them verbatim); never let them escape _tmp."""
    return Path(fn or "upload.bin").name or "upload.bin"


def jrow(j):
    d = dict(j)
    cfg = {}
    try: cfg = json.loads(j["config_text"] or "{}")
    except json.JSONDecodeError: pass
    steps = cfg.get("steps") or j["steps"]
    pct = j["pct"]
    if steps and steps > 0:
        pct = max(pct, min(int(pct), 100))  # raw pct stays authoritative
    d["bar"] = pct
    d["elapsed"] = ((j["finished_at"] or int(time.time()))
                    - (j["started_at"] or j["created_at"]))
    return d


def _job_event(con, jid, kw):
    """ltxq.JOB_WATCHERS callback: after every committed job mutation, push
    the fresh row to SSE clients (same shape as GET /api/job/<jid>)."""
    j = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if j:
        EVENTS.publish("job", {"job": jrow(j)})


ltxq.JOB_WATCHERS.append(_job_event)


def _publish_job(jid):
    """Emit a job event for mutations that bypass set_job (insert/delete)."""
    if not jid_ok(jid):
        return
    with contextlib.closing(ltxq.db()) as con:
        j = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if j:
            EVENTS.publish("job", {"job": jrow(j)})


def _publish_hosts():
    """Emit a host event (pause/resume changes paused state immediately,
    before the engine's next snapshot)."""
    with contextlib.closing(ltxq.db()) as con:
        EVENTS.publish("host", {"hosts": _hosts_view(con)})


def _hosts_view(con):
    """The `hosts` array of /api/state, reused for hello/host SSE events."""
    hosts = []
    for h in con.execute("SELECT * FROM hosts WHERE enabled=1"):
        hp = STATE["host_paused"].get(h["alias"], {})
        hosts.append({
            "alias": h["alias"], "models_dir": h["models_dir"],
            "max_jobs": h["max_jobs"], "backend": h["backend"],
            "idle": STATE["idle"].get(h["alias"], {}),
            "paused": bool(hp.get("paused")),
            "release_at": hp.get("release_at"),
            **STATE["hosts"].get(h["alias"], {}),
        })
    return hosts


def state_dict():
    with contextlib.closing(ltxq.db()) as con:
        jobs = [jrow(j) for j in con.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT 50")]
        return {"jobs": jobs, "hosts": _hosts_view(con),
                "merge": merge_view(con), "merge_run": dict(MERGE_RUN),
                "engine": STATE["engine"], "now": int(time.time())}


@app.get("/")
def index():
    # no-cache: the dashboard is a single file that changes with the app —
    # a stale webview must never outlive an upgrade
    r = flask.send_from_directory(STATIC, "index.html")
    r.headers["Cache-Control"] = "no-cache"
    return r


@app.get("/next")
def index_next():
    # dev preview of the in-progress composer shell; the stable UI at "/" stays
    # frozen while static/index-next.html is built out slice by slice. Both
    # URLs share the same API and queue, so /next is tested against real jobs.
    r = flask.send_from_directory(STATIC, "index-next.html")
    r.headers["Cache-Control"] = "no-cache"
    return r


@app.get("/api/state")
def api_state():
    return flask.jsonify(**state_dict())


# --------------------------------------------------------------- diagnostics
# Powers the dashboard's gear-icon overlay ("Settings & server status"):
# component inventory (presence / resolved path / version of everything the
# server actually uses), the effective hosts.yaml settings, and an engine +
# per-host summary. Read-only snapshot, computed per request (only fetched
# when the overlay opens or its Refresh button is pressed); tool versions are
# cached because they cannot change while the server runs.

_TOOL_TTL = 600.0                     # seconds; tool resolution + -version run
_TOOL_CACHE = {"at": 0.0, "tools": {}}


def _version_line(path):
    """First line of `<tool> -version` (ffmpeg/ffprobe style)."""
    try:
        r = subprocess.run([path, "-version"], capture_output=True, text=True,
                           timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        return f"(version check failed: {e})"
    for line in (r.stdout or r.stderr).splitlines():
        if line.strip():
            return line.strip()
    return ""


def _tool_status(name):
    """{present, path, version} for a CLI helper resolved like the engine does."""
    now = time.time()
    cached = _TOOL_CACHE["tools"].get(name)
    if cached and now - _TOOL_CACHE["at"] < _TOOL_TTL:
        return cached
    try:
        path = ltxq.ff_tool(name)
        info = {"present": True, "path": path, "version": _version_line(path)}
    except RuntimeError:
        info = {"present": False, "path": None, "version": None}
    _TOOL_CACHE["tools"][name] = info
    _TOOL_CACHE["at"] = now
    return info


def _pkg_version(dist):
    try:
        import importlib.metadata as im
        return im.version(dist)
    except Exception:
        return None


def _git_head():
    try:
        r = subprocess.run(["git", "-C", str(HERE), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or None
    except OSError:
        return None


@app.get("/api/status")
def api_status():
    conf, conf_error = None, None
    try:
        conf = ltxq.conf()
    except SystemExit:
        conf_error = (f"hosts.yaml missing — expected at {ltxq.CONF_PATH} "
                      "(Settings → Edit hosts.yaml creates one from the example)")
    except yaml.YAMLError as e:
        conf_error = f"hosts.yaml parse error (fix or Apply a valid file): {e}"
    components = [
        {"name": "ffmpeg", **_tool_status("ffmpeg")},
        {"name": "ffprobe", **_tool_status("ffprobe")},
        {"name": "python", "present": True, "path": sys.executable,
         "version": sys.version.split()[0]},
        {"name": "flask", "present": True, "path": None,
         "version": _pkg_version("flask")},
        {"name": "pyyaml", "present": True, "path": None,
         "version": _pkg_version("pyyaml")},
    ]
    db_bytes = None
    try:
        db_bytes = ltxq.DB_PATH.stat().st_size
    except OSError:
        pass
    settings, hosts = {}, []
    if conf is not None:
        settings = {k: conf[k] for k in
                    ("cli_path", "poll_secs", "stall_secs", "remote_root",
                     "movies_dir", "use_pty", "keep_remote", "offline",
                     "disable_preview", "download_missing")}
    with contextlib.closing(ltxq.db()) as con:
        jobs = dict(con.execute(
            "SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
        for h in con.execute("SELECT * FROM hosts ORDER BY alias"):
            live = STATE["hosts"].get(h["alias"], {})
            hosts.append({
                "alias": h["alias"], "enabled": bool(h["enabled"]),
                "backend": h["backend"], "max_jobs": h["max_jobs"],
                "models_dir": h["models_dir"], "conn": live.get("conn"),
                "worker_alive": live.get("worker_alive"),
                "cli_path": ltxq.cli_of(conf, h) if conf else h["cli_path"],
                "cli_dialect": (ltxq.dialect_of(conf, h) if conf
                                else (h["cli_dialect"] or ltxq.DEFAULT_DIALECT)),
                "frame_roles": (ltxq.dialect_has(ltxq.dialect_spec(
                    ltxq.dialect_of(conf, h)), "first_frame") if conf else None),
            })
    templates = HERE / "templates"
    return flask.jsonify(
        server={"port": STATE["port"], "engine": STATE["engine"],
                "poll_secs": (conf or {}).get("poll_secs"), "repo": str(HERE),
                "db": str(ltxq.DB_PATH), "db_bytes": db_bytes,
                "jobs": jobs, "git": _git_head(),
                "conf_path": str(ltxq.CONF_PATH), "conf_error": conf_error,
                "templates": templates.exists()
                and len(list(templates.glob("*.json"))) or 0},
        components=components, settings=settings, hosts=hosts)


@app.get("/api/events")
def api_events():
    """SSE stream of job/host/engine state changes (see docs/api.md).
    Bootstrap: a `hello` event with the full /api/state payload; then `job`,
    `job_removed`, `host`, `engine` events as state changes. A `: ping`
    comment every ~15 s keeps intermediaries from timing out the stream."""
    token = EVENTS.subscribe()

    def gen():
        try:
            yield "retry: 2000\n\n"
            yield _sse("hello", state_dict())
            while True:
                item = EVENTS.pop(token, timeout=15.0)
                if item is None:
                    yield ": ping\n\n"
                else:
                    yield _sse(*item)
        finally:
            EVENTS.unsubscribe(token)

    return flask.Response(gen(), mimetype="text/event-stream",
                          headers={"Cache-Control": "no-cache",
                                   "X-Accel-Buffering": "no"})


def _capture(fn, *args_):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            fn(*args_)
        except SystemExit as e:                       # cmd_add exits on bad input
            return None, (str(e) or buf.getvalue()).strip()
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"
    return buf.getvalue().strip(), None


@app.post("/api/add")
def api_add():
    f = flask.request.form
    files = flask.request.files
    prompt = (f.get("prompt") or "").strip()
    if not prompt:
        return flask.jsonify(error="prompt is required"), 400
    model = f.get("model") or ""
    if not model:
        return flask.jsonify(error="model is required"), 400
    if ".." in model or model.startswith("/"):
        return flask.jsonify(error="invalid model name"), 400
    chain = f.get("chain") or None
    if chain not in (None, "first_frame", "image"):
        return flask.jsonify(error="chain must be 'first_frame' or 'image'"), 400
    tmp = HERE / "jobs" / "_tmp"; tmp.mkdir(parents=True, exist_ok=True)
    prompt_file = tmp / f"prompt_{int(time.time()*1000)}.txt"
    prompt_file.write_text(prompt)
    # base config: per-model template if present, else the last completed
    # job's config (UI overlay in config_json applies on top either way)
    template = HERE / "templates" / f"{model}.json"
    config_file = None
    if not template.exists():
        with contextlib.closing(ltxq.db()) as con0:
            row = con0.execute("SELECT config_text FROM jobs WHERE status='done' AND "
                               "config_text IS NOT NULL ORDER BY created_at DESC").fetchone()
            if not row:
                return flask.jsonify(error="no config: provide a full config JSON "
                                    "(use 'Fill config from last job' after a first "
                                    "render, or add templates/"
                                    + model + ".json)"), 400
            config_file = tmp / f"cfg_{int(time.time()*1000)}.json"
            config_file.write_text(row["config_text"])
    ns = argparse.Namespace(
        model=model, prompt_file=str(prompt_file),
        config_file=config_file, config_json=f.get("config_json") or None,
        host=f.get("host") or None, name=f.get("name") or None,
        parent=None, seed=int(f["seed"]) if f.get("seed") else None,
        frames=int(f["frames"]) if f.get("frames") else None,
        new_seed=bool(f.get("new_seed")), ext=f.get("ext") or "mov",
        backend=f.get("backend") or None, chain=chain,
        batch=(f.get("batch") or "").strip() or None,
        **{attr: None for attr, _ in ltxq.FLAGMAP},
        upload=[], extra_arg=[ea for ea in (f.get("extra_arg") or "").splitlines()
                              if ea.strip()])
    # `image` is repeatable (order matters: first = primary/canvas image);
    # every other slot stays single-valued.
    uploaded_images = []
    for attr, _ in ltxq.FLAGMAP:
        ups = files.getlist("image") if attr == "image" else [files.get(attr)]
        saved = []
        for up in ups:
            if not up or not up.filename:
                continue
            p = tmp / f"{int(time.time()*1000)}_{safe_upload_name(up.filename)}"
            up.save(p)
            saved.append(str(p))
        if attr == "image":
            uploaded_images = saved
        elif saved:
            setattr(ns, attr, saved[0])
    for up in files.getlist("upload"):
        p = tmp / f"{int(time.time()*1000)}_{safe_upload_name(up.filename)}"
        up.save(p)
        ns.upload.append(str(p))
    # keyframes: each row = uploaded image + "index[:strength[:attention]]";
    # the image rides along as a plain upload and the --keyframe token uses
    # the @file@ placeholder that resolve_extra() maps to the remote path
    for i, kf in enumerate(files.getlist("keyframe_file")):
        spec = (f.getlist("keyframe_spec") or [])[i] or "0"
        if not kf.filename:
            continue
        p = tmp / f"{int(time.time()*1000)}_{safe_upload_name(kf.filename)}"
        kf.save(p)
        ns.upload.append(str(p))
        ns.extra_arg.append(f"--keyframe @{p.name}@:{spec.strip()}")
    for flag in ("--keyframe-strength", "--keyframe-attention-strength"):
        v = f.get(flag[2:])
        if v:
            ns.extra_arg += [flag, v]
    # ffmpeg-extracted frames staged earlier (/api/extract): slot -> staged name
    # Staged images (Load / + media / copy media) come first, in staged order,
    # so a Load round-trip keeps the original image order and the first image
    # stays the canvas/primary one; freshly uploaded images follow. Every other
    # slot still takes a single staged value, and only when it is empty.
    staged_images = [str(STAGE / st) for st in f.getlist("staged_image")
                     if _staged_ok(STAGE / st)]
    ns.image = staged_images + uploaded_images
    for slot in ("audio", "first_frame", "middle_frame", "last_frame", "input_video"):
        if getattr(ns, slot, None):
            continue
        st = f.get(f"staged_{slot}")
        if st and _staged_ok(STAGE / st):
            setattr(ns, slot, str(STAGE / st))
    st = f.get("staged_keyframe")
    if st and _staged_ok(STAGE / st):
        idx = f.get("keyframe_index", "0").strip() or "0"
        spec = ":".join([idx] + [x for x in (f.get("keyframe_strength"),
                                             f.get("keyframe_attention_strength"))
                                  if x])
        ns.upload.append(str(STAGE / st))
        ns.extra_arg.append(f"--keyframe @{Path(st).name}@:{spec}")
    for st in f.getlist("staged_upload"):
        if _staged_ok(STAGE / st):
            ns.upload.append(str(STAGE / st))
    out, err = _capture(ltxq.cmd_add, ns)
    if err:
        return flask.jsonify(error=err), 400
    jid = out.splitlines()[-1].split()[-1] if out else None
    if jid:
        _publish_job(jid)
    return flask.jsonify(jid=jid, warnings=out)


STAGE = HERE / "jobs" / "_tmp"

def _staged_ok(p):
    p = Path(p)
    return p.exists() and p.parent == STAGE and p.name.startswith("stage_")


@app.post("/api/add-batch")
def api_add_batch():
    """Audio-segment batch (ltxq add-batch): the segments come either as
    uploaded folder files (webkitdirectory picker, staged flat into _tmp) or
    as a server-side path typed into the form; everything else routes through
    cmd_add_batch via a Namespace, exactly like /api/add does for cmd_add."""
    f = flask.request.form
    model = f.get("model") or ""
    if not model:
        return flask.jsonify(error="model is required"), 400
    if ".." in model or model.startswith("/"):
        return flask.jsonify(error="invalid model name"), 400
    on_grid = f.get("on_non_grid") or "round-up"
    if on_grid not in ("round-up", "round-down", "refuse"):
        return flask.jsonify(error="invalid on_non_grid"), 400
    staged_dir = None
    try:
        ups = flask.request.files.getlist("files")
        if ups:
            staged_dir = STAGE / f"batchseg_{int(time.time()*1000)}"
            staged_dir.mkdir(parents=True)
            for up in ups:
                up.save(staged_dir / safe_upload_name(up.filename))
            segdir = str(staged_dir)
        else:
            segdir = (f.get("dir") or "").strip()
            if not segdir:
                return flask.jsonify(error="pick a folder or enter a segment "
                                           "directory path"), 400
            segdir = str(Path(segdir).expanduser())
        prompt = (f.get("prompt") or "").strip()
        prompt_file = None
        if prompt:
            prompt_file = STAGE / f"prompt_{int(time.time()*1000)}.txt"
            prompt_file.write_text(prompt)
        image_path = None
        up = flask.request.files.get("image")
        if up and up.filename:
            image_path = STAGE / f"{int(time.time()*1000)}_{safe_upload_name(up.filename)}"
            up.save(image_path)
        ns = argparse.Namespace(
            dir=segdir, model=model, prompt_file=prompt_file,
            config_file=None, config_json=f.get("config_json") or None,
            host=f.get("host") or None,
            seed=int(f["seed"]) if f.get("seed") else None,
            ext=f.get("ext") or "mov", backend=f.get("backend") or None,
            batch=(f.get("batch") or "").strip() or None, manifest=None,
            chain="image" if f.get("chain") else None,
            image=image_path,
            on_non_grid=on_grid)
        # base config: per-model template if present, else the last completed
        # job's config — same fallback as /api/add
        if not (HERE / "templates" / f"{model}.json").exists():
            with contextlib.closing(ltxq.db()) as con0:
                row = con0.execute(
                    "SELECT config_text FROM jobs WHERE status='done' AND "
                    "config_text IS NOT NULL ORDER BY created_at DESC").fetchone()
            if not row:
                return flask.jsonify(error="no config: add templates/" + model
                                    + ".json or run 'Fill config from last job' "
                                    "on the main form once"), 400
            ns.config_file = STAGE / f"cfg_{int(time.time()*1000)}.json"
            ns.config_file.write_text(row["config_text"])
        out, err = _capture(ltxq.cmd_add_batch, ns)
        if err:
            return flask.jsonify(error=err), 400
        jids = re.findall(r"^([0-9a-f]{10})\s", out, re.M)
        for jid in jids:
            _publish_job(jid)
        return flask.jsonify(jids=jids, report=out)
    finally:
        if staged_dir:
            shutil.rmtree(staged_dir, ignore_errors=True)


# --- keyframe-pair batch (ltxq add-pairs; docs/expansion-of-this-idea.md Idea 2)
# Two-phase: /preview stages a stills folder and plans the pair sequence (the
# model-independent manifest, the editable truth), the dashboard edits it
# (autosaved via PUT), and /queue re-validates server-side before queueing.
# Every response = {sid, manifest, view, errors, warnings}: manifest is what
# the client edits, view is what the chosen model/host derive from it.

PAIRS_SID_RE = re.compile(r"[0-9a-f]{8}\Z")

def _pairs_dir(sid):
    return STAGE / f"pairs_{sid}"

def _pairs_sid_ok(sid):
    return bool(PAIRS_SID_RE.match(sid or "")) and _pairs_dir(sid).is_dir()

def _pairs_staged(staging, name):
    """A manifest file reference must be a bare basename of a file that really
    sits in this session's staging dir (never a path that could escape it)."""
    p = staging / Path(name or "").name
    return bool(name) and p.parent == staging and p.is_file()

def _pairs_gc(max_age_s=7 * 86400):
    """Prune abandoned pairs staging sessions (never queue-able again — the
    files live on in the job dirs once queued)."""
    now = time.time()
    for p in STAGE.glob("pairs_*"):
        if p.is_dir():
            try:
                if now - p.stat().st_mtime > max_age_s:
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass

def _unique_name(staging, base):
    p = staging / base
    if not p.exists():
        return base
    stem, suf = Path(base).stem, Path(base).suffix
    i = 2
    while (staging / f"{stem}_{i}{suf}").exists():
        i += 1
    return f"{stem}_{i}{suf}"

def _stage_uploads(staging, ups):
    """Save uploaded files flat into the staging dir under unique basenames.
    Returns the list of staged names."""
    out = []
    for up in ups:
        if not up or not up.filename:
            continue
        name = _unique_name(staging, safe_upload_name(up.filename))
        up.save(staging / name)
        out.append(name)
    return out

def _pairs_opts(f):
    """Pairs-panel options from form fields or a JSON dict (both have .get).
    Returns (opts, error)."""
    model = (f.get("model") or "").strip()
    if not model or ".." in model or model.startswith("/"):
        return None, "model is required"
    order = f.get("order") or "number"
    if order not in ("number", "name"):
        return None, "invalid order"
    onn = f.get("on_non_grid") or "round-up"
    if onn not in ("round-up", "round-down", "refuse"):
        return None, "invalid on_non_grid"
    fpp = f.get("frames_per_pair")
    try:
        fpp = int(fpp) if fpp not in (None, "") else None
    except (TypeError, ValueError):
        return None, "invalid frames_per_pair"
    opts = {"model": model, "host": (f.get("host") or "").strip() or None,
            "order": order,
            "config_json": (f.get("config_json") or "").strip() or None,
            "frames_per_pair": fpp, "on_non_grid": onn}
    return opts, None

def _pairs_fallback_config(model):
    """Per-model template if present, else the last completed job's config
    staged to _tmp — same fallback as /api/add and /api/add-batch. Returns
    (config_file_or_None, error_or_None)."""
    if (HERE / "templates" / f"{model}.json").exists():
        return None, None
    with contextlib.closing(ltxq.db()) as con0:
        row = con0.execute("SELECT config_text FROM jobs WHERE status='done' AND "
                           "config_text IS NOT NULL ORDER BY created_at DESC").fetchone()
    if not row:
        return None, ("no config: add templates/" + model + ".json or run "
                      "'Fill config from last job' on the main form once")
    STAGE.mkdir(parents=True, exist_ok=True)
    p = STAGE / f"cfg_{int(time.time()*1000)}.json"
    p.write_text(row["config_text"])
    return str(p), None

def _pairs_payload(c, con, sid, manifest):
    """Resolve the stored manifest against its stored opts. view is None when
    resolution could not run at all (bad model/host/config) — errors say why."""
    opts = manifest.get("opts") or {}
    warnings = list(manifest.get("warnings", []))
    errors = list(manifest.get("plan_errors", []))
    if not opts.get("model"):
        return {"sid": sid, "manifest": manifest, "view": None,
                "errors": errors + ["no model chosen"], "warnings": warnings}
    config_file, err = _pairs_fallback_config(opts["model"])
    if err:
        return {"sid": sid, "manifest": manifest, "view": None,
                "errors": errors + [err], "warnings": warnings}
    try:
        res, rerrors, rwarns = ltxq.pairs_resolve(
            c, con, manifest, model=opts["model"], host=opts.get("host"),
            config_file=config_file, config_json=opts.get("config_json"),
            frames_per_pair=opts.get("frames_per_pair"),
            on_non_grid=opts.get("on_non_grid") or "round-up",
            base_dir=_pairs_dir(sid))
    except SystemExit as e:                       # bad host / bad config
        return {"sid": sid, "manifest": manifest, "view": None,
                "errors": errors + [str(e)], "warnings": warnings}
    warnings += rwarns
    errors += rerrors
    view = None
    if res:
        view = {"model": res["model"], "fps": res["fps"], "grid": res["grid"],
                "grid_floor": res["grid_floor"], "strategy": res["strategy"],
                "pairs": [{k: v for k, v in p.items() if k != "cfg_delta"}
                          if p else None for p in res["pairs"]]}
    return {"sid": sid, "manifest": manifest, "view": view,
            "errors": errors, "warnings": warnings}

def _pairs_absorb(staging, manifest, srcdir, errors):
    """Append a stills folder to a session: its consecutive pairs go to the
    end of the sequence, its files are copied into the staging dir under
    collision-safe names and every reference is remapped."""
    opts = manifest.get("opts") or {}
    sub, sub_errors = ltxq.pairs_plan_from_dir(
        srcdir, order=opts.get("order", "number"),
        prompt_text=manifest.get("shared_prompt"))
    errors += sub_errors
    if not sub["stills"]:
        return errors
    ren = {}
    names = list(dict.fromkeys(                    # stills double as pair endpoints
        list(sub["stills"]) +
        [p[k] for p in sub["pairs"]
         for k in ("first", "last", "config", "audio") if p.get(k)]))
    for name in names:
        dst_name = _unique_name(staging, name)
        shutil.copyfile(srcdir / name, staging / dst_name)
        ren[name] = dst_name
    manifest["stills"] += [ren[s] for s in sub["stills"]]
    for p in sub["pairs"]:
        for k in ("first", "last", "config", "audio"):
            if p.get(k):
                p[k] = ren[p[k]]
        manifest["pairs"].append(p)
    manifest["warnings"] += sub["warnings"]
    return errors

def _pairs_stage_in(ups, typed_dir):
    """Materialize a folder (uploaded files or a typed server-side path) into
    a throwaway dir, the same shape the folder picker produces."""
    tmp = STAGE / f"pairsin_{int(time.time()*1000)}_{uuid.uuid4().hex[:4]}"
    tmp.mkdir(parents=True)
    if ups:
        for up in ups:
            if up and up.filename:
                up.save(tmp / safe_upload_name(up.filename))
    else:
        for p in sorted(typed_dir.iterdir()):
            if p.is_file():
                shutil.copyfile(p, tmp / safe_upload_name(p.name))
    return tmp

@app.post("/api/pairs/preview")
def api_pairs_preview():
    """Stage a stills folder and plan the pair sequence. The folder comes
    either as uploaded files (webkitdirectory picker) or a server-side path."""
    _pairs_gc()
    f = flask.request.form
    opts, err = _pairs_opts(f)
    if err:
        return flask.jsonify(error=err), 400
    ups = flask.request.files.getlist("files")
    src = (f.get("dir") or "").strip()
    if not ups and not src:
        return flask.jsonify(error="pick a folder or enter a stills "
                                   "directory path"), 400
    typed_dir = None
    if not ups:
        typed_dir = Path(src).expanduser()
        if not typed_dir.is_dir():
            return flask.jsonify(error=f"not a directory: {typed_dir}"), 400
    sid = uuid.uuid4().hex[:8]
    staging = _pairs_dir(sid)
    staging.mkdir(parents=True)
    try:
        c, con = ltxq.conf(), ltxq.db()
        try:
            manifest = {"version": 1, "sid": sid, "stills": [], "pairs": [],
                        "warnings": [], "plan_errors": [], "opts": opts,
                        "batch": (f.get("batch") or "").strip()
                                 or (typed_dir.name if typed_dir else "pairs"),
                        "shared_prompt": (f.get("prompt") or "").strip() or None}
            if ups:
                tmp = _pairs_stage_in(ups, None)
                errors = _pairs_absorb(staging, manifest, tmp, [])
                shutil.rmtree(tmp, ignore_errors=True)
            else:
                errors = _pairs_absorb(staging, manifest, typed_dir, [])
            manifest["plan_errors"] = errors
            (staging / "manifest.json").write_text(json.dumps(manifest, indent=2))
            payload = _pairs_payload(c, con, sid, manifest)
        finally:
            con.close()
    except OSError as e:
        shutil.rmtree(staging, ignore_errors=True)
        return flask.jsonify(error=str(e)), 400
    return flask.jsonify(**payload)

@app.post("/api/pairs/<sid>/folder")
def api_pairs_folder(sid):
    """Append another folder's parsed pairs to the end of the sequence."""
    if not _pairs_sid_ok(sid):
        return flask.jsonify(error="no such pairs session"), 404
    staging = _pairs_dir(sid)
    ups = flask.request.files.getlist("files")
    src = (flask.request.form.get("dir") or "").strip()
    if not ups and not src:
        return flask.jsonify(error="pick a folder or enter a stills "
                                   "directory path"), 400
    typed_dir = None
    if not ups:
        typed_dir = Path(src).expanduser()
        if not typed_dir.is_dir():
            return flask.jsonify(error=f"not a directory: {typed_dir}"), 400
    c, con = ltxq.conf(), ltxq.db()
    try:
        manifest = json.loads((staging / "manifest.json").read_text())
        errors = []
        if ups:
            tmp = _pairs_stage_in(ups, None)
            errors = _pairs_absorb(staging, manifest, tmp, [])
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            errors = _pairs_absorb(staging, manifest, typed_dir, [])
        manifest["plan_errors"] = errors
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2))
        payload = _pairs_payload(c, con, sid, manifest)
    finally:
        con.close()
    return flask.jsonify(**payload)

@app.post("/api/pairs/<sid>/images")
def api_pairs_images(sid):
    """Add single images to the stills palette (assignable to empty pair slots)."""
    if not _pairs_sid_ok(sid):
        return flask.jsonify(error="no such pairs session"), 404
    staging = _pairs_dir(sid)
    ups = flask.request.files.getlist("files")
    if not ups:
        return flask.jsonify(error="no images uploaded"), 400
    manifest = json.loads((staging / "manifest.json").read_text())
    manifest["stills"] += _stage_uploads(staging, ups)
    (staging / "manifest.json").write_text(json.dumps(manifest, indent=2))
    c, con = ltxq.conf(), ltxq.db()
    try:
        payload = _pairs_payload(c, con, sid, manifest)
    finally:
        con.close()
    return flask.jsonify(**payload)

@app.get("/api/pairs/<sid>")
def api_pairs_get(sid):
    """Re-fetch a session (refresh recovery): stored manifest + fresh view."""
    if not _pairs_sid_ok(sid):
        return flask.jsonify(error="no such pairs session"), 404
    staging = _pairs_dir(sid)
    manifest = json.loads((staging / "manifest.json").read_text())
    c, con = ltxq.conf(), ltxq.db()
    try:
        payload = _pairs_payload(c, con, sid, manifest)
    finally:
        con.close()
    return flask.jsonify(**payload)

@app.put("/api/pairs/<sid>/manifest")
def api_pairs_put(sid):
    """Autosave edits (reorder / delete / add pair / inline prompt text /
    per-pair length) and re-derive the view; opts updates re-plan on model or
    host change. Only the editable fields are taken from the client."""
    if not _pairs_sid_ok(sid):
        return flask.jsonify(error="no such pairs session"), 404
    body = flask.request.get_json(silent=True) or {}
    staging = _pairs_dir(sid)
    manifest = json.loads((staging / "manifest.json").read_text())
    if isinstance(body.get("pairs"), list):
        pairs = []
        for p in body["pairs"]:
            if not isinstance(p, dict):
                return flask.jsonify(error="each pair must be an object"), 400
            fo = p.get("frames_override")
            if fo is not None:
                try:
                    fo = int(fo)
                except (TypeError, ValueError):
                    return flask.jsonify(error="frames_override must be an "
                                               "integer"), 400
            pairs.append({
                "first": Path(p.get("first") or "").name or None,
                "last": Path(p.get("last") or "").name or None,
                "prompt": str(p.get("prompt") or ""),
                "prompt_src": str(p.get("prompt_src") or ""),
                "config": Path(p["config"]).name if p.get("config") else None,
                "audio": Path(p["audio"]).name if p.get("audio") else None,
                "frames_override": fo})
        manifest["pairs"] = pairs
    if isinstance(body.get("stills"), list):
        manifest["stills"] = [Path(s or "").name for s in body["stills"]
                              if _pairs_staged(staging, Path(s or "").name)]
    if isinstance(body.get("opts"), dict):
        opts, err = _pairs_opts(body["opts"])
        if err:
            return flask.jsonify(error=err), 400
        manifest["opts"] = opts
    if "shared_prompt" in body:
        manifest["shared_prompt"] = (body.get("shared_prompt") or "").strip() or None
    (staging / "manifest.json").write_text(json.dumps(manifest, indent=2))
    c, con = ltxq.conf(), ltxq.db()
    try:
        payload = _pairs_payload(c, con, sid, manifest)
    finally:
        con.close()
    return flask.jsonify(**payload)

@app.post("/api/pairs/<sid>/queue")
def api_pairs_queue(sid):
    """Re-validate the stored manifest server-side, then queue one job per
    pair. The staging dir is removed on success (assets live in the job dirs)."""
    if not _pairs_sid_ok(sid):
        return flask.jsonify(error="no such pairs session"), 404
    staging = _pairs_dir(sid)
    manifest = json.loads((staging / "manifest.json").read_text())
    f = flask.request.form
    ext = f.get("ext") or "mov"
    if ext not in ("mov", "mp4", "png"):
        return flask.jsonify(error="invalid ext"), 400
    backend = f.get("backend") or None
    if backend and backend not in ("oneshot", "serve"):
        return flask.jsonify(error="invalid backend"), 400
    try:
        seed = int(f["seed"]) if f.get("seed") else None
    except (TypeError, ValueError):
        return flask.jsonify(error="invalid seed"), 400
    c, con = ltxq.conf(), ltxq.db()
    try:
        opts = manifest.get("opts") or {}
        config_file, err = _pairs_fallback_config(opts.get("model") or "")
        if err:
            return flask.jsonify(error=err), 400
        try:
            res, errors, warnings = ltxq.pairs_resolve(
                c, con, manifest, model=opts.get("model"), host=opts.get("host"),
                config_file=config_file, config_json=opts.get("config_json"),
                frames_per_pair=opts.get("frames_per_pair"),
                on_non_grid=opts.get("on_non_grid") or "round-up",
                base_dir=staging)
        except SystemExit as e:
            return flask.jsonify(error=str(e)), 400
        if errors:
            return flask.jsonify(error="batch refused — nothing queued",
                                 errors=errors), 400
        out, cerr = _capture(lambda: ltxq.pairs_queue(
            c, con, manifest, res, model=opts.get("model"),
            host=opts.get("host"), ext=ext, backend=backend,
            batch=(f.get("batch") or "").strip() or manifest.get("batch"),
            seed=seed, base_dir=staging))
        if cerr:
            return flask.jsonify(error=cerr), 400
        jids = re.findall(r"^([0-9a-f]{10})\s", out, re.M)
        for jid in jids:
            _publish_job(jid)
    finally:
        con.close()
    shutil.rmtree(staging, ignore_errors=True)
    return flask.jsonify(jids=jids, report=out)

@app.get("/api/pairs/<sid>/file/<name>")
def api_pairs_file(sid, name):
    """Staged thumbnails (works for uploads, typed paths and refresh recovery)."""
    if not _pairs_sid_ok(sid):
        return "no such session", 404
    p = _pairs_dir(sid) / Path(name).name
    if not _pairs_staged(_pairs_dir(sid), name):
        return "no such file", 404
    return flask.send_file(p)

@app.post("/api/pairs/<sid>/discard")
def api_pairs_discard(sid):
    """Drop a session and its staged files."""
    if not _pairs_sid_ok(sid):
        return flask.jsonify(error="no such pairs session"), 404
    shutil.rmtree(_pairs_dir(sid), ignore_errors=True)
    return flask.jsonify(discarded=sid)


# --- LLM prompt synthesis (docs/expansion-of-this-idea.md "The LLM helper
# stage"). Endpoint profiles live in llm.yaml (hosts.yaml pattern: plain HTTP
# to OpenAI-compatible /v1 servers — Ollama / LM Studio, localhost or LAN, no
# API keys). Two-phase by construction: synthesis writes into the editable
# per-pair prompt fields (prompt_src "llm"); nothing queues without review.

@app.get("/api/llm/endpoints")
def api_llm_endpoints():
    c = ltxq.llm_conf()
    return flask.jsonify(active=c["active"], timeout_s=c["timeout_s"],
                         endpoints=c["endpoints"])

@app.get("/api/llm/models")
def api_llm_models():
    """Model ids of one endpoint (proxy of GET /v1/models)."""
    ep, timeout, err = ltxq.llm_endpoint(
        (flask.request.args.get("endpoint") or "").strip() or None)
    if err:
        return flask.jsonify(error=err, models=[]), 400
    try:
        return flask.jsonify(models=ltxq.llm_models(ep["base_url"], timeout=timeout))
    except ltxq.LLMError as e:
        return flask.jsonify(error=str(e), models=[]), 502

@app.get("/api/llm/directives")
def api_llm_directives():
    """The directive library for the dropdown; `default` follows the pairs
    generation model when ?model= is given (H3 models speak the MiniMax
    format, everything else the LTX default)."""
    model = (flask.request.args.get("model") or "").strip()
    return flask.jsonify(directives=ltxq.llm_directives(),
                         default=ltxq.llm_default_directive(model))

LLM_DIRECTIVE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")

@app.get("/api/llm/template")
def api_llm_template():
    """One directive's text (nameless = the default)."""
    name = (flask.request.args.get("name") or "").strip() or None
    txt, meta, err = ltxq.llm_directive(name)
    if err:
        return flask.jsonify(error=err, name=name or "", template=""), 400
    return flask.jsonify(name=meta["name"], template=txt,
                         custom=meta["custom"], path=meta["path"])

@app.put("/api/llm/template")
def api_llm_template_put():
    """Write a user-local directive (override of a shipped variant, or a new
    one) — the shipped library in templates/ stays untouched."""
    d = flask.request.get_json(silent=True) or {}
    txt = str(d.get("template") or "").strip()
    name = (d.get("name") or "").strip()
    if not txt:
        return flask.jsonify(error="template must not be empty"), 400
    if not LLM_DIRECTIVE_NAME_RE.match(name):
        return flask.jsonify(error="invalid directive name"), 400
    ddir = ltxq.CONF_PATH.parent / ltxq.LLM_DIRECTIVES_DIR
    try:
        ddir.mkdir(parents=True, exist_ok=True)
        (ddir / f"{name}.txt").write_text(txt.rstrip() + "\n")
    except OSError as e:
        return flask.jsonify(error=f"cannot write {ddir}: {e}"), 500
    return flask.jsonify(ok=True, name=name, template=txt, custom=True,
                         path=str(ddir / f"{name}.txt"))


def _pairs_llm_setup(d, gen_model=None):
    """(endpoint, vision model, directive name, directive text, timeout_s,
    error) from a synth request body. The directive falls back to the
    model-matched default when omitted (never when explicitly named); a
    vanished default falls back to ltx-default."""
    ep, timeout, err = ltxq.llm_endpoint((d.get("endpoint") or "").strip() or None)
    if err:
        return None, None, None, None, timeout, err
    model = (d.get("model") or "").strip() or ep.get("model") or ""
    if not model:
        return None, None, None, None, timeout, ("no model chosen — pick a "
            "vision model in the pairs panel (or set model: on the endpoint "
            "in llm.yaml)")
    explicit = (d.get("directive") or "").strip()
    name = explicit or ltxq.llm_default_directive(gen_model)
    txt, meta, terr = ltxq.llm_directive(name)
    if terr and not explicit and name != ltxq.LLM_DEFAULT_DIRECTIVE:
        name = ltxq.LLM_DEFAULT_DIRECTIVE
        txt, meta, terr = ltxq.llm_directive(name)
    if terr:
        return None, None, None, None, timeout, terr
    return ep, model, name, txt, timeout, None


def _pairs_pair_subs(view, i):
    """{{DUR}}/{{FRAMES}}/{{FPS}} values for one pair from the resolved view
    (empty when the pair's frame length or fps could not be derived — a
    directive needing them then fails that pair with a clear error)."""
    subs = {}
    fps = (view or {}).get("fps")
    rows = (view or {}).get("pairs") or []
    frames = rows[i].get("frames") if i < len(rows) and rows[i] else None
    if frames and fps:
        subs.update(FRAMES=int(frames), FPS=fps, DUR=f"{frames / fps:.2f}")
    return subs


def _pairs_pair_facts(subs):
    """Clip facts for the user message, so a directive can steer with the
    real length even without using {{DUR}} in its output."""
    return ({"frames": subs["FRAMES"], "fps": subs["FPS"],
             "seconds": subs["DUR"]} if subs.get("DUR") else None)


def _pairs_manifest_write(staging, manifest):
    (staging / "manifest.json").write_text(json.dumps(manifest, indent=2))


SYNTH_RUN = {"running": False, "sid": None, "done": 0, "total": 0,
             "current": None, "errors": [], "skipped": []}
_SYNTH_LOCK = threading.Lock()        # serializes run attempts

def _pairs_synth_worker(sid, work, ep, model, dtext, timeout):
    """Bulk runner: sequential vision calls (a local LLM serves one request at
    a time). Each pair is described from a manifest snapshot, but the result
    is written back onto a FRESH re-read — only that pair's prompt fields
    change — so dashboard edits made during a (slow) LLM call survive, and a
    pair deleted or reordered mid-run is skipped rather than mis-described."""
    staging = _pairs_dir(sid)

    def find(manifest, item):
        return next((p for p in manifest.get("pairs", [])
                     if p.get("first") == item["first"]
                     and p.get("last") == item["last"]), None)

    for n, item in enumerate(work):
        SYNTH_RUN["current"] = {"n": n + 1, "pair": item["i"] + 1}
        try:
            manifest = json.loads((staging / "manifest.json").read_text())
            pair = find(manifest, item)
            if pair is None:
                SYNTH_RUN["skipped"].append(
                    {"pair": item["i"] + 1, "reason": "pair removed"})
                continue
            msgs = ltxq.pairs_pair_messages(
                pair, staging, manifest.get("shared_prompt"), dtext,
                facts=item.get("facts"))
            txt = ltxq.llm_fill_placeholders(
                ltxq.llm_chat(ep["base_url"], model, msgs,
                              timeout=timeout).strip(),
                item.get("subs") or {})
            if not txt:
                raise ltxq.LLMError("LLM returned an empty prompt")
            manifest = json.loads((staging / "manifest.json").read_text())
            pair = find(manifest, item)
            if pair is None:
                SYNTH_RUN["skipped"].append(
                    {"pair": item["i"] + 1, "reason": "pair removed"})
                continue
            pair["prompt"], pair["prompt_src"] = txt, "llm"
            _pairs_manifest_write(staging, manifest)
        except ltxq.LLMError as e:            # one bad call never sinks the batch
            SYNTH_RUN["errors"].append({"pair": item["i"] + 1, "error": str(e)})
        except Exception as e:
            SYNTH_RUN["errors"].append(
                {"pair": item["i"] + 1, "error": f"{type(e).__name__}: {e}"})
        SYNTH_RUN["done"] = n + 1
    SYNTH_RUN.update(running=False, current=None)
    print(f"[synth] {sid}: {SYNTH_RUN['done']}/{len(work)} pair(s) described, "
          f"{len(SYNTH_RUN['errors'])} error(s), {len(SYNTH_RUN['skipped'])} skipped")

@app.post("/api/pairs/<sid>/synth")
def api_pairs_synth(sid):
    """Describe pair transition(s) with the endpoint's vision model, steered
    by a directive from the library (body `directive`; default follows the
    pairs model — H3 gets the MiniMax FL2VA format). Body {index: N}
    synthesizes one pair synchronously; body without an index runs all pairs
    in a background thread. Bulk never overwrites a non-empty inline prompt
    (your typed text) and skips pairs with unassigned stills."""
    if not _pairs_sid_ok(sid):
        return flask.jsonify(error="no such pairs session"), 404
    d = flask.request.get_json(silent=True) or {}
    manifest = json.loads((_pairs_dir(sid) / "manifest.json").read_text())
    ep, model, dname, dtext, timeout, err = _pairs_llm_setup(
        d, gen_model=(manifest.get("opts") or {}).get("model"))
    if err:
        return flask.jsonify(error=err), 400
    single = d.get("index") is not None
    if single:
        try:
            i = int(d["index"])
        except (TypeError, ValueError):
            return flask.jsonify(error="invalid index"), 400
        if not (0 <= i < len(manifest.get("pairs") or [])):
            return flask.jsonify(error="index out of range"), 400
        c, con = ltxq.conf(), ltxq.db()
        try:
            view0 = _pairs_payload(c, con, sid, manifest)["view"]
        finally:
            con.close()
        subs = _pairs_pair_subs(view0, i)
        with _SYNTH_LOCK:
            if SYNTH_RUN["running"]:
                return flask.jsonify(error="a synthesis run is already active"), 409
            try:
                txt = ltxq.pairs_describe_pair(
                    manifest["pairs"][i], _pairs_dir(sid),
                    manifest.get("shared_prompt"), ep["base_url"], model,
                    dtext, timeout=timeout,
                    facts=_pairs_pair_facts(subs), subs=subs)
                manifest["pairs"][i]["prompt"] = txt
                manifest["pairs"][i]["prompt_src"] = "llm"
            except ltxq.LLMError as e:
                return flask.jsonify(error=str(e)), 502
            manifest["llm_directive"] = dname
            _pairs_manifest_write(_pairs_dir(sid), manifest)
        c, con = ltxq.conf(), ltxq.db()
        try:
            payload = _pairs_payload(c, con, sid, manifest)
        finally:
            con.close()
        return flask.jsonify(**payload)
    c, con = ltxq.conf(), ltxq.db()
    try:
        view = _pairs_payload(c, con, sid, manifest)["view"]
    finally:
        con.close()
    with _SYNTH_LOCK:
        if SYNTH_RUN["running"]:
            return flask.jsonify(error="a synthesis run is already active"), 409
        work = []
        for i, p in enumerate(manifest.get("pairs") or []):
            if p.get("prompt_src") == "inline" and (p.get("prompt") or "").strip():
                continue                       # never overwrite typed text
            if not (p.get("first") and p.get("last")):
                continue                       # nothing to describe yet
            subs = _pairs_pair_subs(view, i)
            work.append({"i": i, "first": p["first"], "last": p["last"],
                         "subs": subs, "facts": _pairs_pair_facts(subs)})
        if not work:
            return flask.jsonify(error="nothing to describe — pairs lack "
                                       "stills or already carry inline prompts"), 400
        manifest = json.loads((_pairs_dir(sid) / "manifest.json").read_text())
        manifest["llm_directive"] = dname      # provenance for the review UI
        _pairs_manifest_write(_pairs_dir(sid), manifest)
        SYNTH_RUN.update(running=True, sid=sid, done=0, total=len(work),
                         current=None, errors=[], skipped=[])
        threading.Thread(target=_pairs_synth_worker,
                         args=(sid, work, ep, model, dtext, timeout),
                         daemon=True).start()
    return flask.jsonify(ok=True, total=len(work))

@app.get("/api/pairs/<sid>/synth")
def api_pairs_synth_status(sid):
    """Progress of the bulk synthesis run (the UI polls this)."""
    if not _pairs_sid_ok(sid):
        return flask.jsonify(error="no such pairs session"), 404
    return flask.jsonify(
        running=SYNTH_RUN["running"] and SYNTH_RUN["sid"] == sid,
        sid=SYNTH_RUN["sid"], done=SYNTH_RUN["done"], total=SYNTH_RUN["total"],
        current=SYNTH_RUN["current"], errors=SYNTH_RUN["errors"],
        skipped=SYNTH_RUN["skipped"])


@app.get("/api/view/<jid>")
def api_view(jid):
    if not jid_ok(jid):
        return "no such job", 404
    with contextlib.closing(ltxq.db()) as con:
        j = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if not j or j["status"] != "done":
            return "no collected video for this job", 404
    v = ltxq.video_of(j)
    if not v:
        return "no video file found", 404
    return flask.send_file(v)


@app.post("/api/extract")
def api_extract():
    d = flask.request.get_json(force=True) or {}
    jid = d.get("jid", "")
    if not jid_ok(jid):
        return flask.jsonify(error="job is not done / not found"), 400
    with contextlib.closing(ltxq.db()) as con:
        j = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if not j or j["status"] != "done":
            return flask.jsonify(error="job is not done / not found"), 400
        v = ltxq.video_of(j)
        if not v:
            return flask.jsonify(error="no video in output dir"), 400
        STAGE.mkdir(parents=True, exist_ok=True)
        tmp = STAGE / f"stage_{int(time.time()*1000)}_{j['id']}.tmp.png"
        try:
            n = ltxq.extract_frame(v, str(d.get("frame", "")).strip(), tmp)
        except (RuntimeError, ValueError, OSError) as e:
            tmp.unlink(missing_ok=True)
            return flask.jsonify(error=str(e)), 400
        dst = tmp.with_name(f"stage_{int(time.time()*1000)}_{j['id']}_f{n}.png")
        tmp.rename(dst)
        return flask.jsonify(staged=dst.name, frame=n,
                             preview="/api/stage/" + dst.name)


@app.get("/api/stage/<name>")
def api_stage(name):
    if not _staged_ok(STAGE / name):
        return "not a staged file", 404
    return flask.send_from_directory(STAGE, name)


@app.get("/api/asset/<jid>/<name>")
def api_asset(jid, name):
    if not jid_ok(jid):
        return "no such asset", 404
    p = HERE / "jobs" / jid / Path(name).name
    if not p.is_file():
        return "no such asset", 404
    return flask.send_file(p)


@app.post("/api/stage_from/<jid>/<name>")
def api_stage_from(jid, name):
    """Copy a past job's input file into the staging area for re-attachment."""
    if not jid_ok(jid):
        return flask.jsonify(error="no such asset"), 404
    src = HERE / "jobs" / jid / Path(name).name
    if not src.is_file():
        return flask.jsonify(error="no such asset"), 404
    STAGE.mkdir(parents=True, exist_ok=True)
    dst = STAGE / f"stage_{int(time.time()*1000)}_{Path(name).name}"
    import shutil as _sh
    _sh.copyfile(src, dst)
    return flask.jsonify(staged=dst.name, preview="/api/stage/" + dst.name)


@app.post("/api/delete/<jid>")
def api_delete(jid):
    import shutil as _sh
    if not jid_ok(jid):
        return flask.jsonify(error="no such job"), 404
    d = (flask.request.get_json(force=True, silent=True) or {})
    with contextlib.closing(ltxq.db()) as con:
        j = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if not j:
            return flask.jsonify(error="no such job"), 404
        if j["status"] in ("queued", "uploading", "running", "collecting",
                           "suspect", "cancelling"):
            return flask.jsonify(error="job is still active — cancel it first"), 400
        if d.get("output") and j["local_out"]:
            _sh.rmtree(j["local_out"], ignore_errors=True)
        _sh.rmtree(HERE / "jobs" / jid, ignore_errors=True)
        con.execute("DELETE FROM jobs WHERE id=?", (jid,)); con.commit()
    EVENTS.publish("job_removed", {"id": jid})
    return flask.jsonify(ok=True)


@app.post("/api/cancel/<jid>")
def api_cancel(jid):
    if not jid_ok(jid):
        return flask.jsonify(ok=False, error="no such job"), 404
    out, err = _capture(ltxq.cmd_cancel, argparse.Namespace(id=jid))
    return flask.jsonify(ok=not err, error=err, out=out), (400 if err else 200)


@app.post("/api/queue/<alias>/pause")
def api_queue_pause(alias):
    """Pause dispatch to a host.  Optional body: {"release_at": <epoch_int>}"""
    d = flask.request.get_json(force=True, silent=True) or {}
    release_at = d.get("release_at")
    if release_at is not None:
        try:
            release_at = int(release_at)
        except (TypeError, ValueError):
            return flask.jsonify(error="release_at must be an integer epoch"), 400
    STATE["host_paused"].setdefault(alias, {}).update(
        paused=True, release_at=release_at)
    msg = (f"paused; scheduled release at epoch {release_at}"
           if release_at else "paused indefinitely")
    print(f"[queue] {alias}: {msg}")
    _publish_hosts()
    return flask.jsonify(ok=True, alias=alias, paused=True, release_at=release_at)


@app.post("/api/queue/<alias>/resume")
def api_queue_resume(alias):
    """Resume dispatch to a host immediately, clearing any scheduled release."""
    STATE["host_paused"].setdefault(alias, {}).update(paused=False, release_at=None)
    print(f"[queue] {alias}: manually resumed")
    _publish_hosts()
    return flask.jsonify(ok=True, alias=alias, paused=False)


@app.post("/api/hosts/reload")
def api_hosts_reload():
    """Validate hosts.yaml on disk and apply it (re-sync the hosts table).
    Returns 200 with the effective path + host summary, or 400 with the
    parse/validation error and the file untouched."""
    try:
        raw = ltxq.CONF_PATH.read_text()
    except OSError as e:
        return flask.jsonify(error=f"cannot read hosts.yaml: {e}",
                             path=str(ltxq.CONF_PATH)), 400
    try:
        cfg = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        return flask.jsonify(error=f"YAML parse error: {e}",
                             path=str(ltxq.CONF_PATH)), 400
    if cfg is None:
        cfg = {}
    if not isinstance(cfg, dict):
        return flask.jsonify(error="hosts.yaml must be a YAML mapping",
                             path=str(ltxq.CONF_PATH)), 400
    hosts = cfg.get("hosts") or []
    if not isinstance(hosts, list):
        return flask.jsonify(error="`hosts:` must be a list",
                             path=str(ltxq.CONF_PATH)), 400
    aliases = []
    for h in hosts:
        if not isinstance(h, dict) or not h.get("alias"):
            return flask.jsonify(
                error="every entry under `hosts:` needs at least an `alias:`",
                path=str(ltxq.CONF_PATH)), 400
        aliases.append(h["alias"])
    dupes = [a for a in set(aliases) if aliases.count(a) > 1]
    if dupes:
        return flask.jsonify(error=f"duplicate host alias(es): {', '.join(sorted(dupes))}",
                             path=str(ltxq.CONF_PATH)), 400
    with contextlib.closing(ltxq.db()) as con:
        try:
            ltxq.sync_hosts(con)
        except Exception as e:
            con.rollback()
            return flask.jsonify(error=f"reload failed: {e!r}",
                                 path=str(ltxq.CONF_PATH)), 400
        summary = [{"alias": h["alias"], "dest": h["dest"], "enabled": bool(h["enabled"])}
                   for h in con.execute("SELECT alias, dest, enabled FROM hosts ORDER BY alias")]
    print(f"[config] hosts.yaml reloaded via API from {ltxq.CONF_PATH} "
          f"({len(summary)} host(s))")
    _publish_hosts()
    return flask.jsonify(path=str(ltxq.CONF_PATH), hosts=summary)


@app.post("/api/regen/<jid>")
def api_regen(jid):
    if not jid_ok(jid):
        return flask.jsonify(error="no such job"), 404
    f = flask.request.form or {}
    ns = argparse.Namespace(
        id=jid, model=f.get("model") or None,
        config_json=f.get("config_json") or None, host=f.get("host") or None,
        name=f.get("name") or None,
        seed=int(f["seed"]) if f.get("seed") else None,
        frames=int(f["frames"]) if f.get("frames") else None,
        new_seed=bool(f.get("new_seed")), ext=f.get("ext") or "mov",
        backend=f.get("backend") or None, batch=f.get("batch") or None)
    out, err = _capture(ltxq.cmd_regen, ns)
    if err:
        return flask.jsonify(error=err), 400
    jid = out.splitlines()[-1].split()[-1] if out else None
    if jid:
        _publish_job(jid)
    return flask.jsonify(jid=jid)


@app.get("/api/models/<alias>")
def api_models(alias):
    with contextlib.closing(ltxq.db()) as con:
        c = ltxq.conf()
        out, err = _capture(ltxq.cmd_models,
                            argparse.Namespace(alias=alias, catalog=False))
        rows = [{"model": r["model"], "name": r["name"], "downloaded": r["downloaded"]}
                for r in con.execute("SELECT model,name,downloaded FROM registry "
                                     "WHERE host=? AND downloaded=1 ORDER BY name", (alias,))]
        return flask.jsonify(models=rows, error=err, raw=out)


@app.get("/api/job/<jid>")
def api_job(jid):
    if not jid_ok(jid):
        return flask.jsonify(error="no such job"), 404
    with contextlib.closing(ltxq.db()) as con:
        j = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if not j:
            return flask.jsonify(error="no such job"), 404
        return flask.jsonify(job=jrow(j))


# --------------------------------------------------------------- merge tray
# One implicit ordered playlist of finished jobs' collected videos ("merge
# tray"), concatenated locally with ffmpeg on demand. Stream-copy concat only
# (all outputs share the engine's per-host video-format preset, so the common
# case is lossless and seconds-fast); a local subprocess that never touches
# the engine loop, engine.lock, or any remote host — safe to run mid-queue.

MERGE_RUN = {"running": False, "name": None, "out": None, "pct": 0,
             "error": None, "finished_at": None, "results": []}
_MERGE_LOCK = threading.Lock()        # serializes run attempts

# v1 policy: refuse clips whose elementary streams differ instead of
# re-encoding (re-encode is CPU-heavy and would fight the queue).
def _merge_probe(path):
    """(duration_s, stream signature) of a video via ffprobe."""
    r = subprocess.run([ltxq.ff_tool("ffprobe"), "-v", "error",
                        "-select_streams", "v:0", "-show_entries",
                        "stream=codec_name,width,height,r_frame_rate:"
                        "format=duration", "-of", "json", str(path)],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError("ffprobe failed: " + (r.stderr or "").strip()[-200:])
    d = json.loads(r.stdout or "{}")
    st = (d.get("streams") or [{}])[0]
    sig = (st.get("codec_name"), st.get("width"), st.get("height"),
           st.get("r_frame_rate"))
    return float((d.get("format") or {}).get("duration") or 0), sig


def merge_view(con):
    """Tray rows resolved against the jobs table (deleted jobs / missing
    video files are flagged, not silently dropped — the UI offers removal)."""
    out = []
    for it in con.execute("SELECT pos, jid FROM merge_items ORDER BY pos"):
        j = con.execute("SELECT * FROM jobs WHERE id=?",
                        (it["jid"],)).fetchone()
        v = ltxq.video_of(j) if j and j["status"] == "done" else None
        out.append({"pos": it["pos"], "jid": it["jid"],
                    "name": j["name"] if j else None,
                    "gone": j is None, "video": v})
    return out


def _merge_publish():
    with contextlib.closing(ltxq.db()) as con:
        EVENTS.publish("merge", {"merge": merge_view(con),
                                 "merge_run": dict(MERGE_RUN)})


def _merge_renumber(con):
    for i, row in enumerate(con.execute(
            "SELECT id FROM merge_items ORDER BY pos"), 1):
        con.execute("UPDATE merge_items SET pos=? WHERE id=?", (i, row["id"]))


@app.post("/api/merge/add")
def api_merge_add():
    d = flask.request.get_json(force=True, silent=True) or {}
    jid = d.get("jid", "")
    if not jid_ok(jid):
        return flask.jsonify(error="no such job"), 404
    with contextlib.closing(ltxq.db()) as con:
        j = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if not j or j["status"] != "done":
            return flask.jsonify(error="job is not done / not found"), 400
        if not ltxq.video_of(j):
            return flask.jsonify(error="no video file found in output dir"), 400
        n = con.execute("SELECT COUNT(*) FROM merge_items").fetchone()[0]
        con.execute("INSERT INTO merge_items(pos, jid, added_at) VALUES(?,?,?)",
                    (n + 1, jid, int(time.time())))
        con.commit()
        view = merge_view(con)
    _merge_publish()
    return flask.jsonify(ok=True, merge=view)


@app.post("/api/merge/remove")
def api_merge_remove():
    d = flask.request.get_json(force=True, silent=True) or {}
    with contextlib.closing(ltxq.db()) as con:
        cur = con.execute("DELETE FROM merge_items WHERE pos=?",
                          (int(d.get("pos") or 0),))
        _merge_renumber(con)
        con.commit()
        view = merge_view(con)
    if not cur.rowcount:
        return flask.jsonify(error="no such tray position"), 404
    _merge_publish()
    return flask.jsonify(ok=True, merge=view)


@app.post("/api/merge/move")
def api_merge_move():
    d = flask.request.get_json(force=True, silent=True) or {}
    pos, direction = int(d.get("pos") or 0), d.get("dir")
    if direction not in ("up", "down"):
        return flask.jsonify(error="dir must be 'up' or 'down'"), 400
    with contextlib.closing(ltxq.db()) as con:
        row = con.execute("SELECT id, pos FROM merge_items WHERE pos=?",
                          (pos,)).fetchone()
        other = con.execute("SELECT id, pos FROM merge_items WHERE pos=?",
                            (pos - 1 if direction == "up" else pos + 1,)).fetchone()
        if not row or not other:
            return flask.jsonify(error="cannot move further"), 400
        con.execute("UPDATE merge_items SET pos=? WHERE id=?",
                    (other["pos"], row["id"]))
        con.execute("UPDATE merge_items SET pos=? WHERE id=?",
                    (row["pos"], other["id"]))
        con.commit()
        view = merge_view(con)
    _merge_publish()
    return flask.jsonify(ok=True, merge=view)


@app.post("/api/merge/clear")
def api_merge_clear():
    with contextlib.closing(ltxq.db()) as con:
        con.execute("DELETE FROM merge_items"); con.commit()
        view = merge_view(con)
    _merge_publish()
    return flask.jsonify(ok=True, merge=view)


def _merge_worker(paths, total_s, out_path):
    ffmpeg = ltxq.ff_tool("ffmpeg")
    lst = STAGE / f"merge_{int(time.time()*1000)}.txt"
    STAGE.mkdir(parents=True, exist_ok=True)
    lst.write_text("".join(
        "file '" + p.replace("'", "'\\''") + "'\n" for p in paths))
    try:
        with tempfile.TemporaryFile(mode="w+") as errf:
            proc = subprocess.Popen(
                [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "error",
                 "-progress", "pipe:1", "-f", "concat", "-safe", "0",
                 "-i", str(lst), "-c", "copy", "-movflags", "+faststart",
                 "-y", str(out_path)],
                stdout=subprocess.PIPE, stderr=errf, text=True)
            for line in proc.stdout:
                k, _, v = line.strip().partition("=")
                if k in ("out_time_us", "out_time_ms") and total_s > 0:
                    try:                          # both fields are microseconds
                        MERGE_RUN["pct"] = min(99, int(int(v) / 1e6 / total_s * 100))
                    except ValueError:
                        pass
            proc.wait()
            if proc.returncode == 0:
                MERGE_RUN.update(running=False, out=str(out_path), finished_at=int(time.time()))
                MERGE_RUN["results"].insert(0, {
                    "name": out_path.name, "path": str(out_path),
                    "bytes": out_path.stat().st_size,
                    "finished_at": MERGE_RUN["finished_at"]})
                del MERGE_RUN["results"][5:]
                print(f"[merge] wrote {out_path}")
            else:
                errf.seek(0)
                MERGE_RUN.update(running=False, finished_at=int(time.time()),
                                 error="ffmpeg failed: "
                                       + (errf.read() or "").strip()[-300:])
                print(f"[merge] FAILED: {MERGE_RUN['error']}")
    except Exception as e:
        MERGE_RUN.update(running=False, finished_at=int(time.time()),
                         error=f"{type(e).__name__}: {e}")
        print("[merge] error:", repr(e))
    finally:
        lst.unlink(missing_ok=True)
    with contextlib.closing(ltxq.db()) as con:
        _merge_publish()


@app.post("/api/merge/run")
def api_merge_run():
    d = flask.request.get_json(force=True, silent=True) or {}
    with _MERGE_LOCK:
        if MERGE_RUN["running"]:
            return flask.jsonify(error="a merge is already running"), 400
        with contextlib.closing(ltxq.db()) as con:
            view = merge_view(con)
        if not view:
            return flask.jsonify(error="merge tray is empty"), 400
        missing = [r for r in view if not r["video"]]
        if missing:
            return flask.jsonify(
                error="remove the missing entries first ("
                      + ", ".join(r["jid"] for r in missing) + ")"), 400
        paths = [r["video"] for r in view]
        try:
            durs, sigs = zip(*(_merge_probe(p) for p in paths))
        except (RuntimeError, OSError, ValueError) as e:
            return flask.jsonify(error=str(e)), 400
        for i, sig in enumerate(sigs[1:], 2):
            if sig != sigs[0]:
                return flask.jsonify(
                    error=f"clip {i} differs from clip 1 "
                          f"(codec/size/fps {sig} vs {sigs[0]}) — merging needs "
                          "identical streams (v1 has no re-encode fallback)"), 400
        name = ltxq.slug(d.get("name") or "") or f"merge-{int(time.time())}"
        out_dir = Path(ltxq.conf()["movies_dir"]).expanduser() / "merges"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{name}{Path(paths[0]).suffix or '.mp4'}"
        for i in range(2, 100):                   # never overwrite a prior merge
            if not out.exists():
                break
            out = out_dir / f"{name}-{i}{Path(paths[0]).suffix or '.mp4'}"
        MERGE_RUN.update(running=True, name=out.name, out=None, pct=0,
                         error=None, finished_at=None)
        threading.Thread(target=_merge_worker,
                         args=(paths, sum(durs), out), daemon=True).start()
    return flask.jsonify(ok=True, name=out.name)


@app.get("/api/merge/file/<name>")
def api_merge_file(name):
    d = Path(ltxq.conf()["movies_dir"]).expanduser() / "merges"
    p = d / Path(name).name
    if not p.is_file():
        return "no such merge output", 404
    return flask.send_file(p)


def run_ui(a):
    STATE["port"] = a.port
    if not STATIC.exists():
        STATIC.mkdir()
    if not (a.no_engine or STATE["engine"]):
        lock = ltxq.engine_lock()
        if not lock:
            raise SystemExit("another engine is running (run or ui) — cannot start")
        threading.Thread(target=engine_loop, daemon=True).start()
    print(f"ui: http://127.0.0.1:{a.port}")
    app.run(host="127.0.0.1", port=a.port, threaded=True,
            use_reloader=False)
