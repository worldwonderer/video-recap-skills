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

- **video** — the source clip(s). In cut mode (with explicit `--source-video`)
  each `clip_plan` entry references the real source range; otherwise a single clip
  spans the rendered input. The clip's `audio` carries the **original-audio ducking
  automation** (continuous bed: dipped under each narration beat; inter-beat gaps shorter
  than `duck_bridge_seconds` stay ducked — no swell back to `base_gain` between sentences;
  only lead-in, lead-out, and genuine gaps >= `duck_bridge_seconds` return to `base_gain`).
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

The exporter is now **schema-driven** rather than a single monolithic JSON
builder. `export_jianying.py` remains the public facade, while the implementation
is split into:

- `jianying_schema.py` — draft version metadata, full `materials` skeleton,
  root/meta skeleton factories, material-category registry, feature capabilities;
- `jianying_model.py` — a thin internal build context where seconds/gains become
  JianYing-local microseconds/gains;
- `jianying_builders.py` — current video/audio/text/speed/KFTypeVolume builders;
- `jianying_tracks.py` — render-index bands and deterministic overlap-safe
  allocation for future overlay/text/image lanes;
- `jianying_writer.py` — collision-safe folder choice, media bundling, path
  rewrite, and atomic write of the three draft files.

The schema metadata is aligned with the duoec/duo-video reference baseline
(`version: 360000`, `new_version: 111.0.0`, `app_version: 5.9.5-beta1`) and the
materials skeleton includes the newer `common_mask` array seen in that baseline.
This makes the exporter newer-schema-friendly than the old minimal
`app_version: 5.9.0` implementation, but it is not a promise that every future
剪映/CapCut release will open drafts without a manual smoke test.

**Media is bundled by default.** The referenced media is copied into the draft's
`materials/` folder and the paths rewritten to those copies. This is **required on
macOS**: 剪映 is sandboxed and cannot read files outside its own data dir, so an
unbundled draft opens with every clip "暂无访问权限 / offline". Drop the draft (with
its `materials/`) into 剪映's drafts root — on this setup
`~/Movies/JianyingPro/User Data/Projects/com.lveditor.draft/` — and it appears in
the 草稿 list. Use `--jianying-no-bundle-media` only when 剪映 can reach the original
paths (e.g. media already under the drafts root). If the requested draft folder already exists and is non-empty, the exporter writes a numbered sibling (for example `recap_demo_2`) rather than overwriting an edited draft.

**Decoupling guarantees** (the core never depends on 剪映):
- the exporter is **lazy-imported** only when an export is requested; importing the
  render path does not import it or any `jianying_*` helper module (enforced by a
  clean-interpreter test);
- it is **stdlib + ffprobe only** — no `pymediainfo`, no vendored library;
- any failure is caught and logged; it never breaks the ffmpeg render.

## Material registry and capabilities

The registry deliberately separates **material categories** from **cross-cutting
features**:

- Supported material categories in milestone 1: `video`, `audio`, `text`,
  `subtitle`, plus JianYing's auxiliary `speed` material.
- Reserved categories, inspired by duo-video but not emitted yet:
  `image`, `sticker`, `sound`, `text_template`, `lut`, `transition`,
  `video_effect`, `face_effect`, `mask`, `style`.
- Feature capabilities are tracked separately: `KFTypeVolume` automation, BGM
  loop splitting, media bundling, bundled-path rewrite, collision-safe writes,
  and lazy export isolation.

Unsupported material categories produce explicit exporter notes and are skipped
instead of silently writing malformed draft JSON.

**Limitations** (documented, not bugs): the draft references the *un-burned* source,
so the source's own hardcoded subtitles show in 剪映 (mask them there if needed);
ffmpeg remains the canonical mix — the 剪映 mix is an editable approximation.

## Manual smoke checklist

When a desktop 剪映/CapCut install is available, generate a bundled draft and
verify:

1. the draft appears in the app's draft list;
2. source clips, narration, BGM, and subtitles are online and editable;
3. ducking is visible/audible as volume automation;
4. no macOS permission/offline-media warnings appear for bundled media.

## Acknowledgements

Draft schema follows [pyJianYingDraft](https://github.com/GuanYixuan/pyJianYingDraft)
and [capcut-mate](https://github.com/Hommy-master/capcut-mate) (both Apache-2.0),
with schema/builder/writer boundaries inspired by duoec/duo-video; no code is
vendored.
