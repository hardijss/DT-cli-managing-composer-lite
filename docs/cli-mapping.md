# How ltxq reaches the engine CLI's `generate` — and what to do when it changes

ltxq never reimplements generation; every render is a `draw-things-cli generate`
invocation on some host (or the `serve` worker equivalent). This doc records the
*mechanism* by which UI/API settings become CLI arguments — that mechanism is
stable, while the engine's own option list changes with every draw-things-cli
release. When a new engine argument appears, nothing in ltxq breaks and nothing
needs to be written: the argument is already reachable, the only question is
whether it deserves promotion to a first-class control.

## The four channels

Every user-facing setting reaches `generate` through one of exactly four
channels (all assembled in `cmd_add` in `ltxq.py`, rendered by `make_runner`
for the oneshot backend, or by `serve_args` for the serve backend):

1. **Media slots (`FLAGMAP`)** — the six file-accepting flags, each mapped 1:1
   to an upload slot in the UI and `/api/add`:
   `--image`, `--audio`, `--first-frame`, `--middle-frame`, `--last-frame`,
   `--input-video`. Repeatable/parameterised siblings (`--keyframe`,
   `--keyframe-strength`, `--keyframe-attention-strength`) ride the same
   upload path but are emitted as `extra_arg` tokens with `@file@` references.
2. **Content overrides (`--seed`, `--frames`)** — the two scalar flags the UI
   exposes as dedicated fields; appended after the config, so they override it
   (engine precedence: recommended settings < `--config-json`/`--config-file`
   < explicit flags).
3. **Config layering (`--config-json` on top of `--config-file`)** — the
   engine's native `JSGenerationConfiguration` passthrough. ltxq always
   supplies a base `--config-file` (per-model template in `templates/`, else
   the last completed job's config) and merges the UI's "Config JSON" textarea
   on top via `--config-json`. Anything expressible in that JSON format —
   steps, guidance, dimensions, negative prompt, samplers, LoRAs — is
   therefore already scriptable without any ltxq change.
4. **Raw escape hatch (`extra_arg`)** — verbatim CLI tokens, one per line,
   accepted by `/api/add` (and `ltxq add --extra-arg`). `@filename@`
   placeholders are rewritten to the file's remote path at launch
   (`resolve_extra`). Any engine flag ltxq has never heard of works here on
   day one.

Independently of all four, ltxq *owns* a set of flags and never surfaces them:
`--model` / `--models-dir` (model resolution and per-host `models_dir`),
`--output` / `--prompt-file` / `--config-file` (job-dir conventions),
`--no-download-missing` / `--disable-preview` / `--offline` (hosts.yaml
`download_missing` / `disable_preview` / `offline` keys), and the entire
remote/cloud backend group (`--remote*`, `--cloud-compute`, `--api-key`,
`--cloud-api-base-url` — dispatch to hosts is ltxq's whole job). The
terminal-image flags are meaningless in a queue.

## Promotion policy

When a new engine argument shows up (or an old one gets a UI request), classify
it:

- **Per-run content knob** (changes the *output*, varies job to job) →
  promote to a dedicated form field + `/api/add` field, following the
  `--frames` pattern (`ltxq.py` argparse on `add`/`regen`, `server.py` form
  passthrough, `static/index.html` input, history Load round-trip via
  `extra_args`). Examples already promoted: seed, frames, keyframes.
- **Config-level setting** (constant across a batch/experiment) → no UI work;
  document that it belongs in the Config JSON textarea or a
  `templates/<model>.json`.
- **Queue/environment concern** → ltxq-owned; extend `hosts.yaml` /
  `conf()` defaults instead (the `--offline` family).
- **Everything else** → `extra_arg` is the supported path; it requires no code.

## Drift check: `ltxq flags`

`./venv/bin/python ltxq.py flags [alias ...] [--update]` runs
`generate --help` on each enabled host (or the named ones) over the existing
ssh/local transport, extracts the option list, and diffs it against the
committed snapshot [generate_flags.txt](generate_flags.txt):

- **new** — options the hosts report that the snapshot lacks (a new
  draw-things-cli release shipped something; re-run this doc's classification),
- **gone** — snapshot options no host reported (candidates for removal from
  `FLAGMAP` / docs),
- **host skew** — hosts disagreeing with each other (a host running an older
  engine CLI than the rest).

`--update` rewrites the snapshot from what the hosts report. Exit code 1 on any
drift or unreachable host, so it can gate a cron or pre-flight script. Note the
help probe is a real CLI invocation: a stub binary that ignores `--help` and
starts generating will do exactly that — real draw-things-cli builds print help
and exit.

## Appendix: inventory as of 2026-09-06 (draw-things-cli / tod-dt-cli, LTX-era build)

Point-in-time reference — **expected to age**; `ltxq flags` is the authoritative
diff. Grouped as the engine's help groups them:

| Engine flag(s) | Reachable via | First-class UI? |
|---|---|---|
| `--model`, `--models-dir` | ltxq-owned | model dropdown; models_dir via hosts.yaml |
| `--prompt`, `--prompt-file` | ltxq-owned (prompt textarea → `--prompt-file`) | yes |
| `--negative-prompt(-file)` | config JSON key | no — config only |
| `--steps`, `--cfg`, `--width`, `--height`, `--strength` | config JSON keys | no — config only |
| `-s/--seed` | channel 2 | yes (+ new-seed checkbox) |
| `--frames` | channel 2 | yes |
| `--stage2-steps`, `--stage2-cfg`, `--stage2-shift` | config JSON (some stored there) / extra_arg | no |
| `--config-json`, `--config-file` | channel 3 (managed) | Config JSON textarea |
| `--image`, `--audio`, `--input-video`, `--first/-middle/-last-frame` | channel 1 | yes — dropzones |
| `--keyframe` (repeatable), `--keyframe-strength`, `--keyframe-attention-strength` | channel 1 + extra_arg | yes — keyframe rows |
| `--ltx-stage2-only-video`, `--ltx-video-extension`, `--extension-prefix-frames`, `--extension-prefix-strength` | extra_arg | no |
| `--avc`, `--segment-frames`, `--cond-frames` | extra_arg | no |
| `--audio-file`, `--audio-encoder-file`, `--audio-start-time`, `--audio-latents`, `--export-swift-audio-latents` | extra_arg (audio itself: dropzone) | no |
| `--prompt-relay`, `--prompt-relay-file` | extra_arg | no |
| `--nag-scale`, `--nag-tau`, `--nag-alpha` | extra_arg | no |
| `-o/--output`, `--video-format` | output path ltxq-owned; ext (mov/mp4/png) is a UI select; codec is a hosts.yaml preset (`video_format`, default `hevc`, per-host override, `""` disables) | ext only |
| `--terminal-image`, `--terminal-image-protocol` | n/a in a queue | never |
| `--download-missing/--no-download-missing`, `--disable-preview`, `--offline` | ltxq-owned (hosts.yaml) | settings pane |
| `--remote`, `--remote-url/-port/-tls/-shared-secret`, `--cloud-compute`, `--api-key`, `--cloud-api-base-url` | ltxq-owned (dispatch) | never |
| `--version`, `-h/--help` | n/a | n/a |

Most-wanted promotions at the time of writing (cheap, content-knob shaped):
`--negative-prompt` and an `extra_arg` passthrough box in the form (the API
already accepts it; the HTML form does not). Two promotions since landed:
`--video-format` as a hosts.yaml preset (a codec is a per-host choice, not a
per-job knob) and `--fflf-preflight` as an automatic dispatch-time check for
oneshot jobs carrying both first and last frames — chain-resolved frames
included, which a form-time button could never cover (the frame doesn't exist
until the job's turn in the queue).
