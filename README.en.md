# video-recap-skills

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Claude Code Plugin](https://img.shields.io/badge/Claude%20Code-Plugin-purple)
![Powered by Xiaomi MiMo](https://img.shields.io/badge/AI-Xiaomi%20MiMo-green)

[中文](README.md) · English

**Turn any video into a Chinese-narration recap, from one sentence inside Claude Code.** All it needs locally is `ffmpeg` and one Xiaomi MiMo API key — no GPU, no model downloads, on macOS / Linux / Windows.

## Demo

https://github.com/user-attachments/assets/92698ec6-0d23-4f9f-8825-c3684ef57aff

Beyond the rendered MP4, you can export a **剪映/JianYing draft** to keep editing by hand — original clips, narration, BGM, and subtitles each on their own track:

<img alt="Exported 剪映 draft: original clips, narration, BGM, and subtitles each on their own track" src="docs/jianying-export.png" width="70%">

## What is it?

```mermaid
flowchart LR
    research["Story research"] --> understand
    video(["Video"]) --> understand["Understand<br/>scenes·ASR·VLM"] --> script["Script<br/>agent"] --> voiceover["Voiceover<br/>MiMo TTS"] --> assemble["Assemble<br/>mux·subtitles"] --> output(["Recap"])
    understand -. cut mode: cut first .-> cut["Cut<br/>render first"] -.-> script
    classDef io fill:#eef6ff,stroke:#4f86c6,color:#1f2937;
    classDef opt fill:#f3f4f6,stroke:#9ca3af,color:#374151;
    class video,output io;
    class research,cut opt;
```

## Why use it?

- **One key, runs anywhere.** ASR, VLM, and TTS all go through [Xiaomi MiMo](https://platform.xiaomimimo.com); `ffmpeg` is the only local dependency.
- **Research when it matters.** When the title/story context is known or the brief says the substrate is thin, put character relationships and plot background in `background_research.json` so the VLM knows who's who; skip it when network/context is unavailable.
- **Narration in blocks, original in blocks.** Narration plays in connected blocks, each voiced in one pass; in the gaps between, the original audio plays at full volume — roughly 7:3.
- **Cut first, no drift.** `--edit-mode cut` renders the cut first, then you narrate against that timeline, so picture and voice stay in sync; an LLM pass reviews the draft before TTS.
- **Keep editing in 剪映.** Optionally export a multi-track 剪映 draft — original, narration, BGM, and subtitles each on a track; the core render needs only `ffmpeg`.

## Installation

**① Install the plugin** — ask Claude Code:

```text
Install this plugin: https://github.com/worldwonderer/video-recap-skills
```

**② Install ffmpeg** (the pipeline needs no `pip install` — just the standard library and `ffmpeg` on `PATH`, Python 3.10+):

```bash
brew install ffmpeg                        # macOS
sudo apt install ffmpeg                     # Debian/Ubuntu
choco install ffmpeg                        # Windows (or scoop / winget install ffmpeg)
```

Subtitles are burned into the picture by default, which needs an ffmpeg built with **libass (the `subtitles` filter)** — the packages above include it in almost all cases. If yours lacks libass, the run fails fast at the start with a clear message (or pass `--no-burn-subtitles` to keep the MP4 unmasked and subtitles as a sidecar `.srt`). Run `python3 scripts/recap.py --doctor` to self-check.

**③ Set your MiMo API key** (one key powers ASR / VLM / TTS — keep it in an env var, never in the repo):

```bash
export MIMO_API_KEY=your-mimo-key
# tp-* Token-Plan keys auto-connect to a cluster: cn | sgp | ams
export MIMO_TOKEN_PLAN_CLUSTER=cn
```

Pay-as-you-go `sk-*` keys default to `https://api.xiaomimimo.com/v1`. Everything else has a default; to change a model, the voice, loudness, subtitles, or set a key/URL per capability, see the
[config playbook](skills/video-recap/references/config-playbook.md).

## Usage

Point it at a video and give it whatever story context you have:

```text
Make a recap of /path/to/video.mp4. It's 庆余年 episode 1; the lead is 范闲.
```

It analyzes the video, writes the narration against that context, and produces `recap_<name>.mp4` with subtitles. Ask for variations the same way:

```text
Turn /path/to/long.mp4 into a ~10-minute cut-down recap and burn the subtitles in.
```

Behind the scenes the orchestrator chains the stages, pausing so the agent can write the narration (cut mode pauses twice: first write `clip_plan.json` to pick the footage, then — once the cut is rendered — write `narration.json` against that output). Before the first run, check your setup:

```bash
python3 skills/video-recap/scripts/recap.py --doctor
```

## Architecture

| Skill | Does | In → Out (the `work_dir` contract) |
|---|---|---|
| **video-understanding** | scene detect · frame extract · ASR (`mimo-v2.5-asr`) · VLM (`mimo-v2.5`) · fuse timeline · build brief (`--consolidate` index on by default) | `video` → `scenes / asr_result / vlm_analysis / silence_periods / timeline_fusion / agent_narration_brief.md` |
| **video-script** | writing rules (SKILL.md) + review (LLM-as-judge) + lint/validate | `brief + index` → `narration.json` |
| **video-cut** | clip plan → render the cut (cut-first/narrate-second; narration is written on the output timeline, no remap) | `clip_plan.json + video` → `edited_source.mp4` |
| **video-voiceover** | synthesize narration audio (MiMo TTS, `mimo-v2.5-tts`) | `narration.json` → `tts_segments/ + tts_meta.json` |
| **video-assemble** | mux · duck original audio · render subtitles · multi-track timeline (optional 剪映 export) | `video + tts_meta` → `recap_<name>.mp4 + subtitles.srt/.ass + timeline.json` |
| **video-recap** | orchestrator + `--doctor` | `video` → `recap_<name>.mp4` |

## Output

- `recap_<video>.mp4`: the final recap; a stable output alias overwritten in place on every run, so iterating on the narration refreshes the same file. `subtitles.srt` (plus `subtitles.ass`; subtitle burn-in is on by default, `--no-burn-subtitles` to disable)
- `work_dir/narration.json`: the narration script (`narration_lint.json` timing diagnostics, `narration_review.md` review notes)
- `work_dir/agent_narration_brief.md`: timing and scene brief for the agent
- `work_dir/vlm_analysis.json` · `asr_result.json` · `silence_periods.json` · `timeline_fusion.json`: understanding artifacts
- `work_dir/clip_plan.json` · `edited_source.mp4` · `recap_phase.json`: cut-mode artifacts (narration is written on the output timeline; `recap_phase.json` records cut/narrate progress for deterministic resume)
- `work_dir/timeline.json` · `work_dir/assembly_manifest.json` · `tts_segments/` · `tts_meta.json`: multi-track timeline, slim render record, and TTS audio

## Bring your own original-dialogue subtitles (optional, more accurate)

During the original-audio gaps between narration blocks, the original dialogue is burned as a subtitle (wrapped in `「」` to set it apart from narration). By default that text is agent-proofread with an ASR fallback — but ASR timing is coarse and can drift from the audio. For accurate timing, drop a subtitle file into `work_dir`; it becomes the **preferred source**:

- `work_dir/user_subtitles.json`: `[{"start": s, "end": s, "text": "line"}]` on the **output** timeline, used as-is; or wrap it as `{"timeline": "source", "lines": [...]}` to give **source**-timeline subs that are auto-mapped onto the cut via the clip plan.
- `work_dir/user_subtitles.srt` / `.ass`: parsed as **source**-timeline by default and mapped onto the cut.

Priority: **your file › the agent-proofread `original_subtitles.json` › ASR fallback**. When the source is accurate, each line is clipped precisely into its gap instead of being placed by a coarse midpoint estimate.

## References

- Per-skill contracts: each `skills/<skill>/SKILL.md` (the writing rules are in video-script's SKILL.md)
- [Data schema](skills/video-recap/references/data-schema.md) · [Config playbook](skills/video-recap/references/config-playbook.md) · [Multi-track timeline / 剪映 export](skills/video-recap/references/timeline-and-jianying.md)
- [Background research guide](skills/video-understanding/references/research-guide.md) · [VLM prompt templates](skills/video-understanding/references/prompt-templates.md)

## Acknowledgements

- [linux.do](https://linux.do)
- The 剪映 draft export follows the schema of [pyJianYingDraft](https://github.com/GuanYixuan/pyJianYingDraft) and [capcut-mate](https://github.com/Hommy-master/capcut-mate) (both Apache-2.0).

## License

MIT, see [LICENSE](LICENSE).
