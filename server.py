"""ltxq M2 web UI — single process running the engine loop + dashboard API.

Run: ./venv/bin/python ltxq.py ui [--port 8765] [--no-engine]
"""
import argparse, contextlib, gc, io, json, re, shlex, threading, time
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

import flask

import ltxq

HERE = ltxq.HERE
STATIC = HERE / "static"

# job ids are uuid4().hex[:10]; anything else (esp. "..", "/") is not a job
JID_RE = re.compile(r"[0-9a-f]{10}")
MAX_UPLOAD = 4 * 1024**3          # cap multipart bodies (inputs are media files)

STATE = {
    "engine": False,            # engine thread running
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
                job = con.execute("SELECT * FROM jobs WHERE status='queued' AND "
                                  "(host IS NULL OR host=?) ORDER BY created_at, rowid LIMIT 1",
                                  (h["alias"],)).fetchone()
                if job:
                    cur = con.execute("UPDATE jobs SET status='uploading', host=? "
                                      "WHERE id=? AND status='queued'", (h["alias"], job["id"]))
                    con.commit()
                    if cur.rowcount:
                        EVENTS.publish("job", {"job": jrow(con.execute(
                            "SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone())})
                        ltxq.launch(c, con, h, job)
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
                "engine": STATE["engine"], "now": int(time.time())}


@app.get("/")
def index():
    return flask.send_from_directory(STATIC, "index.html")


@app.get("/api/state")
def api_state():
    return flask.jsonify(**state_dict())


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
        new_seed=bool(f.get("new_seed")), ext=f.get("ext") or "mov",
        backend=f.get("backend") or None, chain=chain,
        batch=(f.get("batch") or "").strip() or None,
        **{attr: None for attr, _ in ltxq.FLAGMAP},
        upload=[], extra_arg=[ea for ea in (f.get("extra_arg") or "").splitlines()
                              if ea.strip()])
    for attr, _ in ltxq.FLAGMAP:
        up = files.get(attr)
        if up:
            p = tmp / f"{int(time.time()*1000)}_{safe_upload_name(up.filename)}"
            up.save(p)
            setattr(ns, attr, str(p))
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
    for slot in ("image", "audio", "first_frame", "middle_frame", "last_frame", "input_video"):
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


def run_ui(a):
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
