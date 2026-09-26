# LTX 2.x prompting good practices — research notes (2026-09-26)

Collected while designing ltxq's LLM directives; sources at the bottom.
Applies to the LTX-2 / 2.3 / 2.5 line (audio-video DiT models); the older
LTX-Video README guidance is included where it still holds.

## What the model author (Lightricks) recommends

- **Chronological, shot-like descriptions** — describe what happens in
  order: subject/setting first, then actions as they unfold. Write like a
  director's shot description, not a mood summary.
- **Explicit camera work** — name camera angles and movements (dolly-in,
  pan, tracking…). LTX 2.x reacts to precise camera-motion wording.
- **Lighting and colors** — state them; they steer the look more than style
  adjectives do.
- **Changes and sudden events** — note transitions explicitly ("then the
  lights go out", "the door slams").
- **Describe the soundscape** — LTX-2/2.3 generate synchronized audio *and*
  video in one model, so ambience, sound effects, and dialogue belong in the
  prompt when audio is enabled. Silence unmentioned tends to stay silent.
- **English only**; the older LTX-Video README adds "the more elaborate the
  better" — detail reliably helps.

## Length and structure

- Community guides converge on: scene description → setting & lighting →
  subject actions → explicit camera motion → style keywords.
- Rough length ceiling ~200 words for the distilled/fast variants; LTX 2.5
  Pro accepts prompts up to ~5,000 characters. Long-form videos fare better
  split into scene-by-scene prompts (what ltxq's pair/segment composers do
  structurally anyway) than one mega-prompt.
- Test prompt structure at low resolution / fast mode before committing to
  expensive renders.

## Implications for ltxq's shipped directives

- `ltx-default` and `enhance-ltx` currently mandate **1–3 sentences, under
  80 words** — deliberately terse, matching the classic LTX-Video style.
  LTX-2/2.3's official guidance leans the other way: chronological detail
  of a few sentences plus a soundscape line when the job renders audio.
- A v2 of these directives would ask for: chronological subject → motion →
  camera → lighting description (3–5 sentences), plus one soundscape
  sentence when audio is on. Until then, edit the directive in the
  dashboard (saving creates a local override) or drop a v2 file into
  `llm_directives/` next to `hosts.yaml`.

## Sources

- [Lightricks/LTX-2 — GitHub README (prompting guidance)](https://github.com/Lightricks/LTX-2)
- [Lightricks/LTX-2.3 — Hugging Face model card](https://huggingface.co/Lightricks/LTX-2.3)
- [Lightricks/LTX-Video — Hugging Face README (elaborate-prompt guidance,
  resolution/frame limits)](https://huggingface.co/Lightricks/LTX-Video)
- Community guides: AI-Flow LTX-2 Distilled prompting notes; Picsart LTX 2.5
  Pro overview (5,000-char prompts); scene-based vs single-prompt strategy
  write-ups for long-form LTX video.
