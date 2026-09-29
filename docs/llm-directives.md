# LLM selection & directives — how it works

One page describing how ltxq decides **which LLM server, which model, and
which directive** runs for each LLM call, and what each call sends. Useful
whenever a new generation model or a new LLM server enters the setup.
API-level detail lives in [api.md](api.md) (§`/api/llm/*`, API 1.8); the
shipped directive texts themselves are in `templates/llm_directives/`.

---

## 1. The global LLM card (top of the dashboard)

One selection, three controls, shared by **everything** LLM — pair
synthesis (🪄 / "Describe all pairs"), pair prompt enhancement, and
main-form enhancement:

| control | meaning | remembered where |
|---|---|---|
| **endpoint** | an OpenAI-compatible server from `llm.yaml` (Ollama `:11434`, LM Studio `:1234`, localhost or LAN — plain HTTP, no keys) | `localStorage ltxq.llm_endpoint`, falls back to `active:` in llm.yaml |
| **model** | the vision/instruct model, filled from that endpoint's `GET /v1/models` | `localStorage ltxq.llm_model`, falls back to the endpoint's `model:` in llm.yaml |
| **enhance directive** | which directive the ✨ buttons steer with — `auto (model-matched)` default, or any library entry explicitly | `localStorage ltxq.llm_enh_dir` |

Pair synthesis additionally has a **pair directive** picker in the pairs
panel (synthesis-format variants only — names starting `enhance-` are
filtered out of it).

`llm.yaml` (gitignored, same pattern as hosts.yaml; see
`llm.yaml.example`) holds the named endpoints. A missing file degrades to
one built-in Ollama endpoint, so nothing breaks on a fresh checkout.

## 2. The directive library

Named instruction files; the filename stem is the id shown in the
dropdowns.

- **Shipped** (repo): `templates/llm_directives/*.txt` —
  `ltx-default`, `minimax-h3-fl2va`, `enhance-ltx`, `enhance-minimax-h3`.
- **User-local** (not in git): `llm_directives/*.txt` next to
  `hosts.yaml` (repo root or `~/Library/Application Support/Ltxq/`).
- **Resolution: user-local wins by name.** Saving the editor under a
  shipped name creates a local override (the `*` marker); deleting that
  override restores the shipped text. Shipped files are never deletable
  from the UI.
- Editor controls live in the global card: select → edit → **save**
  (override/new), **new** (save-as under a prompted name, seeded with the
  current text), **delete** (user-local only).
- Naming convention: `enhance-*` = text-only prompt improvement (filtered
  from the synthesis picker). Anything else is treated as a
  synthesis-format directive. A directive that is neither (e.g. a utility
  prompt) appears in both pickers — fine, but the ✨ flow *replaces the
  field text* with the model's reply, so use scratch fields for estimates.

## 3. Selection resolution (the exact order)

For every LLM call, ltxq resolves the directive like this:

1. **Explicit choice wins, verbatim.** The relevant picker's current value
   is sent with the request (`directive` field). An explicit name that
   doesn't exist in the library is a 400 error — never silently swapped.
2. **No explicit choice → the model-matched default for that kind:**
   - pair synthesis: target generation model matches the H3 family (same
     `frame_grid` sniff as the 17n+5 grid: "minimax" or a token-boundary
     `h3`) → `minimax-h3-fl2va`; everything else → `ltx-default`.
   - enhancement: same sniff → `enhance-minimax-h3` (T2VA rewrite format)
     or `enhance-ltx`.
3. **Default vanished?** Only an auto-chosen default falls back
   (`minimax-h3-fl2va` → `ltx-default`, `enhance-minimax-h3` →
   `enhance-ltx`). Explicit names are never second-guessed.
4. The resolved name is looked up in the **merged library — user-local
   first**. So a preset saved under a shipped name (an override) is what
   auto-selection actually runs; a *differently named* user preset is
   never auto-picked, only explicitly selectable.

Pair synthesis also records the directive used in the manifest
(`manifest.llm_directive`) so you can tell why a prompt came out in
MiniMax's three-fields format.

## 4. What each call sends

| call | images? | payload |
|---|---|---|
| 🪄 pair synthesis | **yes — both pair stills** | directive (system) + both staged stills (ffmpeg-downscaled to ≤1024 px jpg when larger, base64 data URLs, order-labeled) + shared prompt as scene/style intent + clip facts (frames/fps/seconds) |
| ✨ enhance (any surface) | **text only by default; per-pair rows attach the pair's two stills when the global card's "attach stills" toggle is on** | directive (system) + the field's draft text (8000-char cap) + optional labeled first/last frame parts; a requested-but-broken attach is an error, never a silent downgrade — and the selected model must be vision-capable |
| model dropdown | — | `GET /v1/models` proxy |

The "attach stills" toggle (`localStorage ltxq.llm_enh_img`) only affects ✨
on per-pair prompts — the pair's staged stills are what get attached; the
shared prompt and the main form have no single pair to attach and stay
text-only.

`{{DUR}}` / `{{FRAMES}}` / `{{FPS}}` tokens in a directive's **output** are
substituted server-side from the resolved view (duration = grid-resolved
frames ÷ fps, two decimals). An unfillable placeholder fails that pair with
a clear error — a literal `{{...}}` can never reach the queue. Enhancement
runs no substitution.

## 5. Adding a new generation model

- Nothing breaks by default: an unknown model name gets the LTX 8n+1 grid
  and the LTX directives.
- To give a new model **family** its own prompt format: add a `FRAME_GRIDS`
  entry / sniff branch in `ltxq.py` (`frame_grid`), write the directive
  (distill from `docs/reference/` — both MiniMax guides are there as source
  material), and extend `llm_default_directive()` to map the family to it.
  Ship it in `templates/llm_directives/` or drop it in your local
  `llm_directives/` to trial.
- Pair synthesis needs a **vision-capable** model on the LLM endpoint
  (qwen2.5-vl, qwen3.5-vl, llava, gemma, minicpm-v…). Enhancement is
  text-only and works with any instruct model.
- Prompting guidance for LTX 2.x lives in
  [reference/ltx-prompting-notes.md](reference/ltx-prompting-notes.md);
  the MiniMax formats in
  [reference/minimax-video-prompt-guide.md](reference/minimax-video-prompt-guide.md)
  and
  [reference/minimax-full-reference-guide.md](reference/minimax-full-reference-guide.md).
