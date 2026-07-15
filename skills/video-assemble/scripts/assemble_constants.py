"""Shared constants for the self-contained video-assemble skill."""

SUBTITLE_RENDER_VERSION = 8
SUBTITLE_TEXT_NORMALIZE_VERSION = 1
ASSEMBLY_MANIFEST = "assembly_manifest.json"
ASSEMBLY_QC = "assembly_qc.json"
VISUAL_QC = "visual_qc.json"
VISUAL_OVERLAYS = "visual_overlays.json"
SEGMENT_AUDIO_SCHEMA_VERSION = 1
FILTER_SCRIPT_THRESHOLD_BYTES = 8000

# The default subtitle metrics were tuned in this reference canvas.
SUBTITLE_STYLE_REF_W = 1280
SUBTITLE_STYLE_REF_H = 720
_SUBTITLE_TERMINAL_PUNCTUATION = "。！？!?…."
_SUBTITLE_CLOSING_QUOTES = "」』”’）)]】》〉\"'"

_MIN_GAP_TO_SUBTITLE = 0.8
_MIN_READABLE_SECONDS = 0.3
_MIN_ASR_CLIP_OVERLAP = 0.05
_MAX_ORIGINAL_READ_CPS = 9.0
_AUTO_ORIGINAL_READ_CPS = 6.0

_VISUAL_DELIVERY_FORBIDDEN_KEYS = {
    "video_encode_passes",
    "reencode_reason",
    "audio_sample_rate",
    "final_compat_notes",
    "double_encode",
    "delivery_compatibility",
    "loudness_mode",
    "loudnorm_measurement",
}
_SUPPORTED_VISUAL_OVERLAY_TYPES = {"top_title", "inline_label_or_callout"}
