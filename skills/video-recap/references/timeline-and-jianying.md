# Multi-track timeline (`timeline.json`) + optional 剪映 export

`video-assemble` always emits `timeline.json`: a backend-neutral description of
the finished recap. ffmpeg remains the canonical renderer; the optional 剪映
exporter consumes the same timeline to make an editable sidecar draft. Timeline
times are seconds and volumes are gains, so this file has no 剪映-only units.

## `timeline.json` schema v2

```jsonc
{
  "schema_version": 2,
  "canvas": {"width": 1280, "height": 720, "fps": 25},
  "duration": 315.0,
  "tracks": [
    {"kind": "video", "name": "video", "clips": [
      {"source_path": "/orig.mp4",
       "source_start": 243.0, "source_end": 268.0,
       "timeline_start": 0.0, "timeline_end": 25.0,
       "audio": {"role": "original", "base_gain": 0.85,
                 "volume_keyframes": [{"t": 0.0, "gain": 0.85},
                                       {"t": 2.19, "gain": 0.2}]}}
    ]},
    {"kind": "audio", "name": "narration", "role": "narration", "segments": [
      {"source_path": "/_placed_0000.wav", "timeline_start": 2.19,
       "timeline_end": 5.9, "gain": 1.0, "text": "…",
       "overlaps_speech": true,
       "source_duck_end": 6.1, "source_restore_at": 6.35,
       "source_handoff_status": "sentence_boundary"}
    ]},
    {"kind": "audio", "name": "bgm", "role": "bgm", "loop": true,
     "segments": [{"source_path": "/bgm.mp3", "timeline_start": 0.0,
                    "timeline_end": 315.0, "gain": 0.18,
                    "volume_keyframes": []}]},
    {"kind": "text", "name": "subtitle", "segments": [
      {"text": "…", "timeline_start": 2.19, "timeline_end": 5.9}
    ]},
    {"kind": "image", "name": "image", "segments": [
      {"source_path": "/card.png", "timeline_start": 3.0,
       "timeline_end": 7.0, "opacity": 0.8, "rotation_degrees": 0,
       "scale": {"x": 0.5, "y": 0.5},
       "position": {"x": 0.25, "y": -0.2},
       "flip": {"horizontal": false, "vertical": false}}
    ]}
  ]
}
```

- **video** — source clips plus editable original-audio volume automation. In
  cut mode, explicit `--source-video` keeps references on the real source ranges.
- **narration** — placed TTS segments. `source_path` points to the exact complete
  per-segment PCM used by the canonical mix (`_placed_*.wav`), never a longer
  pre-fit file that an editor would trim at `timeline_end`.
- **bgm** — optional looped music with its own volume automation.
- **subtitle** — display-ready narration subtitles.
- **image** — optional local photo overlays. Transforms use normalized
  canvas-center coordinates with positive Y upward. `build_timeline(...,
  image_segments=[...])` is currently the programmatic entrypoint; recap does
  not invent image overlays automatically.
- `volume_keyframes` are timeline-absolute `{t, gain}` points with linear ramps.
  Original-audio handoff keyframes stay low through the final source phoneme
  (`source_duck_end`) and release only inside the measured sentence pause, reaching
  idle at `source_restore_at`; ffmpeg and the editable timeline share this shape.

Schema v2 also has additive JianYing authoring fields. Existing v1 timelines
are copied and migrated to v2 at the exporter boundary; an unknown future
schema version is rejected instead of being guessed:

- video clips may add `speed`, `reverse`, `reverse_path`, `opacity`,
  `rotation_degrees`, `scale`, `position`, `flip`, `transition`, `mask`, `lut`,
  `compound`, `chroma`, and `green_background`;
- audio/image segments may add constant `speed`; image segments may also add
  `transition`, `mask`, and `lut`;
- text segments may add `style`, `style_id`, `words`, and the same visual
  transforms. `words[].index` / `length` are UTF-16 code-unit ranges, matching
  JianYing and Java rather than Python code-point indexes;
- top-level `style_presets` is a name-to-style object;
- top-level `resource_packages` is a name-to-offline-package object;
- `build_timeline(..., extra_tracks=[...])` appends explicit `sound`, `sticker`,
  `text_template`, `video_effect`, or `face_effect` tracks without making the
  normal recap pipeline invent proprietary effects.

For a video clip with constant speed, the authored ranges must satisfy
`source_end - source_start == (timeline_end - timeline_start) * speed`; an
inconsistent clip is rejected. Image and resource segments derive that source
duration automatically. This keeps source selection unambiguous.

## Optional 剪映 / JianYing export

`--export-jianying` / `EXPORT_JIANYING=1` maps the timeline into a folder with
`draft_content.json`, `draft_info.json`, and `draft_meta_info.json`. The current
adapter aligns its local-draft contract with
[`duoec/duo-video`](https://github.com/duoec/duo-video) commit
`ef4eb46c823910553f901649f2f13fd7575e748f`:

- root schema profile `version: 360000`, `new_version: 111.0.0`, and template
  `app_version: 5.9.5-beta1`, including `common_mask` in the material skeleton;
- all segment times converted to integer microseconds at one boundary;
- narration interval ends are rounded outward before that conversion, and export
  probes bundled PCM duration; this prevents timestamp rounding from shaving even
  a few samples from the final word;
- narration and BGM as audio tracks, video/photo as video tracks, subtitles as
  text tracks; regular segments use the schema `render_index` /
  `track_render_index` value `2`;
- subtitle materials use `type: subtitle`, with a zero-based full
  `source_timerange` and timeline placement in `target_timerange`;
- native `KFTypeVolume` keyframes and split BGM loops remain editable;
- true half-open interval allocation: adjacent segments reuse a track, while
  overlapping image/video/text material is moved to a numbered sibling track;
- same-type first tracks use `flag: 0`; additional tracks use `flag: 2`.

The upstream README's “verified with 剪映 v10.1.0” is an application smoke-test
claim. The embedded `5.9.5-beta1` value is the upstream JSON template profile;
it is **not** a claim that this project installs or emulates 剪映 5.9.5. We keep
those two facts separate and do not promise compatibility with every desktop
release without a real-app smoke test.

### Portable media bundle

Bundling is on by default because a clone or moved project must open without the
original machine's absolute paths. The writer copies and de-duplicates media to:

```text
Resources/local/video/
Resources/local/audio/
Resources/local/image/
```

Material paths in `draft_content.json` use 剪映's
`##_draftpath_placeholder_…_##/Resources/local/...` form. Type-0 entries in
`draft_meta_info.json` index each copied file with `file_Path`, `metetype`
(`video` / `music` / `photo`), ID, MD5, duration, dimensions, import timestamps,
and rough-cut/sub ranges. Filename collisions are resolved within each resource
kind. Missing media is reported and left as an honest external reference rather
than being recorded as successfully bundled.

`--jianying-no-bundle-media` is only for environments where 剪映 can reach the
original paths. It trades portability for avoiding a copy and is not the
recommended clone-ready workflow. Non-empty draft folders are never overwritten;
a numbered sibling such as `recap_demo_2` is created. The entire folder is staged
and atomically renamed, so a copy/write exception does not publish a half-draft.

### Duo-video capability alignment

The adapter is pinned to `duo-video@ef4eb46`; it deep-copies the upstream MIT
JSON templates before replacing authored values. Core categories use status
`supported`; proprietary-resource categories use
`supported_offline_payload`. The latter means a pre-adapted material/segment
protocol can be emitted from caller data. It does **not** mean this project
ships JianYing's proprietary resource catalog or reconstructs it from an ID.

| Capability | Timeline authoring | Output |
| --- | --- | --- |
| Video / local photo / audio | normal `video`, `image`, `audio` tracks | `materials.videos` / `audios`; photos use `type: photo` |
| Text / subtitle / style | `text` track plus `style`, `style_id`, `words`, `style_presets` | UTF-16 rich-text styles, font/stroke/shadow/background/effect-style payloads |
| Constant speed | `speed > 0` on video/audio/image/resource segments | segment speed plus referenced `materials.speeds`; curve speed is not claimed |
| Reverse | `reverse: true`, optionally `reverse_path` | export generates a local reversed file with ffmpeg when needed, then bundles it; direct `build_draft()` requires `reverse_path` |
| Transform | opacity, rotation, scale, normalized position, flip | editable segment `clip` transform |
| Sound / sticker / video effect / face effect | explicit resource track with one offline material source | `audios`, `stickers`, or `video_effects` plus the matching track |
| Text template | explicit resource track with pre-adapted template/text/effect payloads | `text_templates` plus caller-supplied subordinate `texts` / `effects` |
| Transition / mask / LUT | object or package name on a video/photo segment | referenced `transitions`; legacy + `common_mask`; LUT/skin-tone `effects` |
| Green screen / compound | video clip with `compound`, local `green_background`, and `chroma` | nested `materials.drafts` with recursively bundled foreground/background |

Resource-backed segments must define exactly one of:

```jsonc
{"material": {"type": "sticker", "resource_id": "...", "path": "/local/file"}}
{"resource_config": {
  "resource_id": "...",
  "main_config": {"type": "sticker", "path": "/local/file"},
  "resources": [
    "/local/file",
    {"source_path": "/local/package-dir", "target_path": "package-dir"}
  ],
  "cover_img": "/local/cover.png",
  "texts": [],
  "effects": []
}}
{"resource_package": "named-package"}
```

`resource_package` resolves a top-level `resource_packages` entry with the same
shape as `resource_config`. Both the local snake_case keys above and the real
Jackson `JyResource` keys (`resourceId`, `mainConfig`, `coverImg`) are accepted.
`main_config` / `mainConfig` may be an object or a JSON string;
`resource_config` itself may be an object or a local JSON-file path.

`resources` is an explicit local-file contract, never a filesystem guess. A
string copies that file/directory using its basename. An object may additionally
set `resource_kind` and a safe package-relative `target_path`. ZIP input is
validated against path traversal, extracted under
`Resources/local/<kind>/<archive-stem>`, and keeps its internal layout. Exact
declared path values in material JSON, including rich-text JSON strings, are
rewritten to draft placeholders. Missing declared resources, unsafe targets,
unknown package names, and malformed payloads fail explicitly; semantic strings
that happen to match local filenames are never copied. No network lookup,
embedded demo credential, or silent resource-ID fallback is used.

LUT skin-tone correction additionally requires an offline effect
`main_config` with `lumi_hub_path`; the adapter emits the upstream `version: v3`
skin-tone effect rather than deriving an effect directory from the `.cube`
file's parent.

Text-template **protocol emission** accepts the output of an offline template
adapter in `main_config` / `texts` / `effects`. This repository does not bundle
or download duo-video's separate `jy_text_template_adapter`, so it does not
claim that an arbitrary official resource ID plus replacement strings can be
adapted locally. The same boundary applies to official stickers/effects: callers
must legally supply complete offline package data.

## Isolation and failure behavior

- Export modules are lazy-imported only after `--export-jianying`; the ffmpeg
  render path does not import or depend on them.
- The adapter is Python stdlib + ffprobe only; no vendored package is required.
- Export failure is logged and never invalidates an already-rendered ffmpeg MP4.
- The source clip is un-burned, so hardcoded source subtitles remain visible in
  the editable draft. ffmpeg remains the authoritative final mix.

## Verification boundary and manual smoke checklist

Automated golden tests compare root, video, audio, text, rich-text, and
base-segment templates against the pinned upstream revision. Structural tests
cover compound/meta output and exercise every row in the matrix. On macOS,
JianYing Pro `10.8.7-beta1` detected and registered a generated bundled smoke
draft (`copy_draft_external`, `errno: 0`). Manual verification then opened it,
confirmed online video/narration/BGM/subtitle/photo tracks and a valid preview,
saved it, closed it, and reopened it with the same editable timeline. This does
not prove that every caller-supplied official resource package renders correctly.

AutoJY is a separate desktop-automation/export pipeline in the duo-video
ecosystem, not part of the draft JSON protocol. This repository does not ship or
claim AutoJY UI automation, rendered-file upload, task-status callbacks, or an
automated final-video export smoke test.

With a desktop 剪映 install, generate a bundled draft and verify:

1. it appears in the draft list;
2. video, narration, BGM, subtitles, and photo overlays are online/editable;
3. subtitle semantics, track order/flags, and overlap lanes survive reopening;
4. volume automation is visible/audible;
5. no absolute-path/offline-media warning appears.

## Acknowledgements

The draft schema also follows
[pyJianYingDraft](https://github.com/GuanYixuan/pyJianYingDraft) and
[capcut-mate](https://github.com/Hommy-master/capcut-mate) (Apache-2.0). The
duo-video alignment independently implements the builders and vendors only the
pinned JSON protocol templates under duo-video's MIT license; no upstream
executable code, resource package, adapter binary, or credential is vendored.
