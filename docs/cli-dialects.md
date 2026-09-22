# Engine-CLI dialects (`cli_dialect`)

ltxq talks to whatever engine CLI a render host runs. That surface differs per
binary, so *how to speak to a binary* is now explicit, versioned data instead of
hardcoded assumptions: a **dialect rulebook**. `cli_path` says *where the binary
is*; `cli_dialect` says *what it understands*. They are separate axes — a host
can point `cli_path` at a differently-named binary of the same dialect, or keep
the old path and switch dialects.

## The rulebook

A dialect declares (see `DIALECTS` in `ltxq.py`):

| Field | Meaning |
|---|---|
| `label` | human-readable name for notes/docs/UI |
| `generate_cmd` | the generate subcommand (`generate`) |
| `serve` | does the binary have the `serve` warm-worker subcommand? |
| `fflf_preflight` | does it accept `generate … --fflf-preflight`? |
| `flags` | semantic name → CLI spelling, for every flag the scheduler itself emits |

A semantic that is **absent** from `flags` means *unsupported in this dialect*:
ltxq will route the job elsewhere or refuse it loudly — it never emits a flag
the binary does not know.

## The two dialects today

| | `dtcustom` (default) | `dtofficial` |
|---|---|---|
| Binary | `tod-dt-cli` (DrawOtherThings CustomCLI) | upstream `draw-things-cli` |
| Serve backend | yes (`serve` worker) | **no** (subcommands: generate/auth/models/train/completion) |
| `--fflf-preflight` | yes | no |
| LTX asset flags | `--first-frame`, `--middle-frame`, `--last-frame`, `--input-video`, `--keyframe*` | **not supported** — only `--image`, `--audio` |
| Shared core | `--model`, `--config-file`, `--prompt-file`, `--output`, `--models-dir`, `--video-format`, `--no-download-missing`, `--disable-preview`, `--offline`, `--seed`, `--frames` | same |
| Log/progress shape | percent + `Wrote:` (verified) | same — one shared parser, no dialect-specific scraping |

Both dialects emit `generate` as the subcommand, so the runner template only
takes its spelling from the rulebook.

## Frames vs canvas/moodboard (why `dtofficial` has no frame slots)

Upstream `draw-things-cli` has no `--first-frame` / `--middle-frame` /
`--last-frame` / `--keyframe*`. Images are **canvas** features in Draw Things
terms: there is no way to package them into `--config-file` / `--config-json`,
and no persistent canvas or layers are exposed. Its `--image` is just
repeatable:

| Position | Meaning |
|---|---|
| `--image` #1 | `canvas_active` — the active canvas / primary input |
| `--image` #2..n | moodboard members, in the order given — *references*, not endpoints |
| `--avc` | accepts exactly one `--image` (documented upstream; never exercised by ltxq) |

A frame slot therefore has **no** upstream destination: `--last-frame` /
`--middle-frame` would become moodboard references, and
`--keyframe path:index:strength` would lose the frame index and the strength.
ltxq keeps them unsupported on `dtofficial` and refuses loudly (with the
rulebook's `cap_hints` text explaining this) rather than passing a
semantically wrong command that would run and succeed while not doing what was
asked.

The equivalent upstream affordance is the **Image slot**: `--image` #1 is the
canvas image and any extra `image` assets are the ordered moodboard — which
ltxq already maps 1:1 (its `--image` slot is repeatable). So a job that seeds a
render on a `dtofficial` host should attach the image to *Image* (plus extra
images), not to a frame slot. The frame/keyframe form fields show a hint when
an explicitly selected host's dialect lacks frame roles.

## Binding

`cli_dialect` resolves exactly like `cli_path` / `video_format`:

```
per-host cli_dialect  >  global cli_dialect  >  dtcustom (DEFAULT_DIALECT)
```

An empty string counts as "not set"; an unknown name fails loudly
(`UnknownDialect`) naming the host and the value — no silent fallback. The
value is stored per host in the `hosts.cli_dialect` column (added by the usual
nullable-column migration; existing `hosts.yaml` files without the key behave
exactly as before).

Example:

```yaml
cli_path: ~/tod-dt-cli
cli_dialect: dtcustom          # global default
hosts:
  - alias: dt-community
    cli_path: ~/draw-things-cli
    cli_dialect: dtofficial    # upstream binary on the same machine
```

## Capability gate and routing

Each job derives the capabilities it needs from its own data (`job_caps`):

- a `serve`-backend job needs `serve`;
- every asset flag it carries (and every `--keyframe*` token in `extra_args`)
  needs the matching rulebook entry;
- a job carrying **both** `--first-frame` and `--last-frame` needs
  `fflf_preflight` (that is exactly when dispatch runs the probe).

Enforcement happens at three levels:

1. **Routing** — `next_dispatchable_job(con, alias, c, h)` skips queued jobs
   the candidate host's dialect cannot run, so the host walk
   (top-to-bottom preference, `max_jobs`, batch-chain ordering) falls through
   to a capable host.
2. **Launch** — `launch()` re-checks before any upload/remote work and fails
   the job with a clear note if the host still cannot run it (this is the path
   a job *pinned* to an incapable host takes). `launch_serve()` likewise
   refuses a serve job on a serve-less dialect.
3. **Visibility** — a queued job runnable on **no** enabled host gets a
   non-spamming `unroutable: …` note (written only when the text changes and
   never clobbering a job's own diagnostic note; cleared again once the job
   becomes routable). It is visible via `ltxq check <id>` and the dashboard.

Raw `extra_arg` tokens stay the escape hatch: a flag ltxq does not recognise is
the engine's business. Recognised ones (`--image`, `--keyframe*`, …) are gated
like first-class flags, because on a dialect that lacks them they would be a
hard `ArgumentParser` error at runtime. Model names are **never** translated
between zoos — model availability per dialect is a catalog concern
(`ltxq models`, `ltxq stage` fail fast).

## Drift: `ltxq flags` is per dialect

`./venv/bin/python ltxq.py flags [--update]` groups hosts by their dialect and
diffs each group against its committed snapshot:

- `docs/generate_flags.dtcustom.txt`
- `docs/generate_flags.dtofficial.txt` (created on first `--update`)

Within a dialect, `NEW`/`GONE` and *host skew* mean exactly what they always
did (a release shipped something, or one host runs an older binary). Hosts on
**different** dialects are never reported as skew against each other; their
option differences are printed once as informational. Exit code is 1 on any
within-dialect drift, so it still gates a pre-flight script.

When a drift report lands: classify the changed arguments (see
[cli-mapping.md](cli-mapping.md)'s promotion policy), update the rulebook's
`flags`/feature booleans if needed, and refresh the snapshot with `--update`.
That is a data edit, not a code hunt.

## Adding a third dialect

1. Add an entry to `DIALECTS` in `ltxq.py` with `label`, `generate_cmd`,
   `serve`, `fflf_preflight` and the full `flags` map (omit semantics the
   binary does not support).
2. Run `./venv/bin/python ltxq.py flags --update` on a host running that
   binary to create `docs/generate_flags.<name>.txt`.
3. Set `cli_dialect: <name>` globally or per host.
4. Extend `tests/test_dialects.py` if the dialect needs its own parity/gate
   coverage, and add a row to the table above.
