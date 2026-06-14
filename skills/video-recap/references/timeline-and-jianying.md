# Multi-track timeline (`timeline.json`) + optional 剪映 export

`video-assemble` always emits **`timeline.json`** in the `work_dir`: a small,
backend-neutral model of the finished recap as tracks — like a cut-tool project.
The canonical renderer is still ffmpeg; `timeline.json` is what it writes for
inspection and what the **optional** 剪映 exporter consumes. Times are plain
**seconds**, volumes are plain **gains** (0–1), so the file is backend-agnostic.

## `timeline.json` schema

```jsonc
{
  "schema_version": 1,
  "canvas": {"width": 1280, "height": 720, "fps": 25},
  "duration": 315.0,                       // seconds
  "tracks": [
    { "kind": "video", "name": "video", "clips": [
        { "source_path": "/orig.mp4",
          "source_start": 243.0, "source_end": 268.0,   // trim in the source
          "timeline_start": 0.0, "timeline_end": 25.0,  // position on the output
          "audio": { "role": "original", "base_gain": 0.85,
                     "volume_keyframes": [ {"t": 0.0, "gain": 0.85},
                                           {"t": 2.19, "gain": 0.2}, ... ] } } ] },
    { "kind": "audio", "name": "narration", "role": "narration", "segments": [
        { "source_path": "/_spd_0.wav", "timeline_start": 2.19, "timeline_end": 5.9,
          "gain": 1.0, "text": "…", "overlaps_speech": true } ] },
    { "kind": "audio", "name": "bgm", "role": "bgm", "loop": true, "segments": [
        { "source_path": "/bgm.mp3", "timeline_start": 0.0, "timeline_end": 315.0,
          "gain": 0.18, "volume_keyframes": [ … ] } ] },
    { "kind": "text", "name": "subtitle", "segments": [
        { "text": "…", "timeline_start": 2.19, "timeline_end": 5.9 } ] }
  ]
}
```

- **video** — the source clip(s). In cut mode (with `--source-video` / `SOURCE_VIDEO`)
  each `clip_plan` entry references the real source range; otherwise a single clip
  spans the rendered input. The clip's `audio` carries the **original-audio ducking
  automation** (gap-fill: held at `base_gain` between sentences, dipped under each
  narration window).
- **narration** — the placed TTS beats, one segment each.
- **bgm** — present only with `BGM_PATH`; a looped bed with its own ducking automation.
- **subtitle** — the narration lines.
- `volume_keyframes` are timeline-absolute `{t (s), gain}` points with linear ramps.

## Optional 剪映 / JianYing export

`--export-jianying` (or `EXPORT_JIANYING=1`) runs `export_jianying.py`, which maps
`timeline.json` to a 剪映 draft folder (`draft_content.json` + `draft_info.json` +
`draft_meta_info.json`) under `--jianying-out` / `JIANYING_DRAFT_DIR` (default the
work_dir):

- seconds → **integer microseconds** at the single `_us()` boundary;
- each `volume_keyframes` list → a native `KFTypeVolume` keyframe list (the ducking
  becomes editable volume automation);
- video on the main track, narration/BGM as their own audio tracks (higher
  `render_index`), subtitles on a text track.

**Media is bundled by default.** The referenced media is copied into the draft's
`materials/` folder and the paths rewritten to those copies. This is **required on
macOS**: 剪映 is sandboxed and cannot read files outside its own data dir, so an
unbundled draft opens with every clip "暂无访问权限 / offline". Drop the draft (with
its `materials/`) into 剪映's drafts root — on this setup
`~/Movies/JianyingPro/User Data/Projects/com.lveditor.draft/` — and it appears in
the 草稿 list. Use `--jianying-no-bundle-media` only when 剪映 can reach the original
paths (e.g. media already under the drafts root).

**Decoupling guarantees** (the core never depends on 剪映):
- the exporter is **lazy-imported** only when an export is requested; importing the
  render path does not import it (enforced by a test);
- it is **stdlib + ffprobe only** — no `pymediainfo`, no vendored library;
- any failure is caught and logged; it never breaks the ffmpeg render.

**Limitations** (documented, not bugs): the draft references the *un-burned* source,
so the source's own hardcoded subtitles show in 剪映 (mask them there if needed); a
BGM shorter than the recap is not auto-looped in the draft (copy it to extend);
ffmpeg remains the canonical mix — the 剪映 mix is an editable approximation.

## Acknowledgements

Draft schema follows [pyJianYingDraft](https://github.com/GuanYixuan/pyJianYingDraft)
and [capcut-mate](https://github.com/Hommy-master/capcut-mate) (both Apache-2.0); no code vendored.
