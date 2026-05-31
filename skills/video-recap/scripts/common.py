import json
import re
import subprocess
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from config import CONFIG, PROMPTS_DIR, normalize_api_url

# ── 工具函数 ──────────────────────────────────────────────────────────

def log(msg):
    print(f"[video-recap] {msg}", flush=True)


def run_cmd(cmd, **kwargs):
    """运行命令，返回 CompletedProcess"""
    if isinstance(cmd, list):
        display_parts = []
        for part in cmd:
            text = str(part)
            display_parts.append(text if len(text) <= 240 else text[:237] + "...")
        display = " ".join(display_parts)
    else:
        display = str(cmd)
        if len(display) > 2000:
            display = display[:1997] + "..."
    log(f"运行: {display}")
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def get_video_duration(video_path):
    """获取视频时长（秒）"""
    cmd = ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
           "-of", "csv=p=0", str(video_path)]
    result = run_cmd(cmd)
    if result.returncode != 0:
        return 0.0
    try:
        return float(result.stdout.strip())
    except (TypeError, ValueError):
        return 0.0


def _retry_after_seconds(value, fallback):
    """Parse Retry-After seconds or HTTP-date; return fallback on malformed input."""
    if not value:
        return fallback
    try:
        return max(fallback, max(0, int(value)))
    except (TypeError, ValueError):
        pass
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(fallback, max(0, int((retry_at - datetime.now(timezone.utc)).total_seconds())))
    except (TypeError, ValueError, IndexError, OverflowError):
        return fallback


def _provider_uses_mimo(api_provider=None, api_url=None):
    """Return True when the active API endpoint should use Xiaomi MiMo conventions."""
    provider = str(api_provider if api_provider is not None else CONFIG.get("api_provider") or "").strip().lower()
    url = str(api_url if api_url is not None else CONFIG.get("api_url") or "")
    return provider == "mimo" or "xiaomimimo.com" in url


def _api_headers(api_provider=None, api_url=None, api_key=None):
    """Build auth headers for OpenAI-compatible providers and Xiaomi MiMo."""
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "video-recap/1.0",
    }
    key = CONFIG.get("api_key", "") if api_key is None else api_key
    if _provider_uses_mimo(api_provider=api_provider, api_url=api_url):
        headers["api-key"] = key
    else:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _prepare_api_payload(payload, api_provider=None, api_url=None):
    """Normalize payload fields for the active OpenAI-compatible provider."""
    normalized = dict(payload)
    uses_mimo = _provider_uses_mimo(api_provider=api_provider, api_url=api_url)
    if uses_mimo and "max_tokens" in normalized and "max_completion_tokens" not in normalized:
        normalized["max_completion_tokens"] = normalized.pop("max_tokens")
    model = str(normalized.get("model") or "")
    if (
        uses_mimo
        and CONFIG.get("mimo_disable_thinking", True)
        and not model.endswith("-tts")
        and "thinking" not in normalized
    ):
        # MiMo V2.5 may spend small max_completion_tokens budgets on
        # reasoning_content. The recap pipeline needs visible scene text, so
        # disable thinking by default unless the caller explicitly sets it.
        normalized["thinking"] = {"type": "disabled"}
    return normalized


def _mimo_api_key():
    """Return the MiMo key, falling back to the main key for legacy MiMo-only runs."""
    if CONFIG.get("mimo_api_key"):
        return CONFIG.get("mimo_api_key", "")
    if _provider_uses_mimo():
        return CONFIG.get("api_key", "")
    return ""


def _mimo_endpoint(kind):
    """Return per-capability MiMo endpoint settings for video understanding or TTS."""
    if kind == "video":
        return {
            "api_url": CONFIG.get("mimo_video_api_url") or CONFIG.get("mimo_api_url"),
            "api_key": CONFIG.get("mimo_video_api_key") or _mimo_api_key(),
            "api_key_source": CONFIG.get("mimo_video_api_key_source", "MIMO_VIDEO_API_KEY"),
        }
    if kind == "tts":
        return {
            "api_url": CONFIG.get("mimo_tts_api_url") or CONFIG.get("mimo_api_url"),
            "api_key": CONFIG.get("mimo_tts_api_key") or _mimo_api_key(),
            "api_key_source": CONFIG.get("mimo_tts_api_key_source", "MIMO_TTS_API_KEY"),
        }
    raise ValueError(f"Unsupported MiMo endpoint kind: {kind}")


def _call_mimo_endpoint(kind, payload, max_retries=5):
    settings = _mimo_endpoint(kind)
    return api_call(
        payload,
        max_retries=max_retries,
        api_provider="mimo",
        api_url=settings["api_url"],
        api_key=settings["api_key"],
        api_key_source=settings["api_key_source"],
    )


def mimo_video_api_call(payload, max_retries=5):
    """Call the MiMo video-understanding endpoint."""
    return _call_mimo_endpoint("video", payload, max_retries=max_retries)


def mimo_tts_api_call(payload, max_retries=5):
    """Call the MiMo TTS endpoint."""
    return _call_mimo_endpoint("tts", payload, max_retries=max_retries)


def api_call(payload, max_retries=5, *, api_provider=None, api_url=None, api_key=None, api_key_source=None):
    """调用 OpenAI-compatible API，带重试"""
    endpoint = normalize_api_url(api_url if api_url is not None else CONFIG["api_url"])
    headers = _api_headers(api_provider=api_provider, api_url=endpoint, api_key=api_key)
    data = json.dumps(_prepare_api_payload(payload, api_provider=api_provider, api_url=endpoint)).encode("utf-8")

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(endpoint, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=300) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return result
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:500]
            wait = 2 ** attempt
            if e.code == 429:
                retry_after = e.headers.get("Retry-After")
                wait = _retry_after_seconds(retry_after, wait)
                log(f"API 速率限制 (尝试 {attempt+1}/{max_retries}), 等待 {wait}s")
            elif e.code == 401:
                key_name = api_key_source or CONFIG.get("api_key_source", "OPENAI_API_KEY")
                raise RuntimeError(f"API 认证失败 (401)。请检查 {key_name} 和 API URL 是否匹配。")
            elif e.code == 403:
                hint = "API 访问被拒绝 (403)。"
                if "1010" in body or "cloudflare" in body.lower():
                    hint += "IP 被 Cloudflare 限流，请等待几分钟后重试。"
                    raise RuntimeError(hint)
                hint += "请检查 API key 权限和 API URL 设置。"
                raise RuntimeError(hint)
            elif e.code == 405:
                raise RuntimeError("API 端点不可用 (405)，可能被 WAF 拦截。请检查 OPENAI_API_URL 或稍后重试。")
            elif e.code == 503:
                log(f"API 服务暂不可用 (503)，等待 {wait}s (尝试 {attempt+1}/{max_retries})")
            elif e.code == 524:
                # Cloudflare 超时：服务端处理超时，需要更长退避
                wait = max(wait, 4 * (attempt + 1))
                log(f"API 超时 (524)，等待 {wait}s (尝试 {attempt+1}/{max_retries})")
            else:
                log(f"API 调用失败 (尝试 {attempt+1}/{max_retries}): HTTP {e.code} — {body}")
            if attempt < max_retries - 1:
                time.sleep(wait)
            else:
                raise RuntimeError(f"API 调用失败 {max_retries} 次: HTTP {e.code} — {body}")
        except (urllib.error.URLError, Exception) as e:
            wait = 2 ** attempt
            log(f"API 调用失败 (尝试 {attempt+1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                log(f"等待 {wait}s 后重试...")
                time.sleep(wait)
            else:
                raise RuntimeError(f"API 调用失败 {max_retries} 次: {e}")


def load_prompt(name):
    """加载 prompt 模板"""
    path = PROMPTS_DIR / "prompt-templates.md"
    if not path.exists():
        return None
    content = path.read_text()
    # 用 ### NAME 和 ### 分隔提取对应 prompt
    pattern = rf"### {name}\s*\n(.*?)(?=\n### |\Z)"
    m = re.search(pattern, content, re.DOTALL)
    return m.group(1).strip() if m else None
