import asyncio
import contextlib
import io
import json
import os
import logging
import re
import sys
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
import douban_monitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

VIDEO_ROOT = Path(os.environ.get("VIDEO_ROOT", "/volume1/video"))
PORT = int(os.environ.get("PORT", "19030"))
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".wmv", ".ts", ".m2ts", ".flv", ".webm"}
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa"}
CONFIG_FILE = Path(__file__).parent / "config.json"
QUEUE_DATA_FILE = Path(__file__).parent / "queue_data.json"

DEFAULT_CONFIG = {
    "video_root": str(VIDEO_ROOT),
    "radarr_url": "",
    "radarr_api_key": "",
    "radarr_path_mappings": [],
    "sonarr_url": "",
    "sonarr_api_key": "",
    "sonarr_path_mappings": [],
    "auto_download": False,
    "auto_download_lang": "zho_chs",
    "subtitle_provider": "zimuku",
    "concurrent_search": True,
    "enabled_providers": ["zimuku", "subhd", "assrt", "opensubtitles"],
    "provider_fallback_order": ["zimuku", "subhd", "assrt", "opensubtitles"],
    "max_download_count": 2,
    "score_threshold_pct": 10,
    "good_score_threshold": 72,
    "chinese_score_english_fallback": 72,
    "chinese_scarce_count": 1,
    "chinese_scarce_buffer": 15,
    "min_score_pct": 5,
    "baidu_ocr_api_key": "",
    "baidu_ocr_secret_key": "",
    "assrt_api_token": "",
    "opensubtitles_api_key": "",
    "opensubtitles_username": "",
    "opensubtitles_password": "",
    "ai_api_url": "https://api.openai.com/v1",
    "ai_api_key": "",
    "ai_model": "gpt-4o-mini",
    "ai_target_lang": "Chinese",
    "ai_source_lang": "English",
    "ai_bilingual": True,
    "ai_batch_size": 50,
    "ai_max_retries": 3,
    "ai_retry_delay": 2.0,
    "ai_temperature": 0.3,
    "ai_max_output_tokens": 4096,
    "ai_system_prompt": "",
    "ai_bilingual_prompt": "",
    "ai_glossary": "",
    "ai_context_lines": 2,
    "ai_output_mode": "text",
    "ai_streaming": True,
    "ai_concurrency": 2,
    "ai_review_enabled": True,
    "ai_review_model": "",
    "ai_review_timeout": 300,
    "ai_review_prompt": "",
    "queue_max_size": 100,
    "douban_enabled": False,
    "douban_user_id": "140463388",
    "douban_check_interval_hours": 12,
    "tmdb_api_key": "",
    "radarr_quality_profile_en": 7,
    "radarr_quality_profile_other": 9,
    "radarr_root_folder_path": "/video/Movies",
    "dingtalk_enabled": False,
    "dingtalk_webhook_url": "",
    "douban_baseline": {"movie_name": "", "add_date": ""},
    "douban_last_check": "",
    "douban_history": [],
}

app_config = dict(DEFAULT_CONFIG)
download_queue: asyncio.Queue = asyncio.Queue()
download_items: dict[str, dict] = {}
download_completed: deque = deque(maxlen=app_config.get("queue_max_size", 100))

_search_result_cache: dict[str, list] = {}
download_log: deque = deque(maxlen=100)
app_log: deque = deque(maxlen=500)
_subtitle_status_cache: dict = {}
SUBTITLE_CACHE_FILE = Path(__file__).parent / "subtitle_status_cache.json"

_douban_last_result: dict = {}
_douban_history: deque = deque(maxlen=50)

def _get_cached_status(cache_key: str) -> str:
    entry = _subtitle_status_cache.get(cache_key)
    if entry is None:
        return "none"
    if isinstance(entry, str):
        return entry
    return entry.get("status", "none")


def _set_cached_status(cache_key: str, status: str, video_path: str = "",
                       title: str = "", year=None, last_checked: str = ""):
    _STATUS_PRIORITY = {"zh": 3, "en": 2, "none": 1, "no_file": 0}
    old = _subtitle_status_cache.get(cache_key)
    old_status = "none"
    if old is not None:
        if isinstance(old, str):
            old_status = old
        else:
            old_status = old.get("status", "none")
    if _STATUS_PRIORITY.get(status, 0) < _STATUS_PRIORITY.get(old_status, 0):
        status = old_status
    entry = _subtitle_status_cache.get(cache_key, {})
    if isinstance(entry, str):
        entry = {"status": entry}
    entry["status"] = status
    if video_path:
        entry["video_path"] = video_path
    if title:
        entry["title"] = title
    if year is not None:
        entry["year"] = year
    if last_checked:
        entry["last_checked"] = last_checked
    _subtitle_status_cache[cache_key] = entry
translate_queue: asyncio.Queue = asyncio.Queue()
translate_items: dict[str, dict] = {}
translate_completed: deque = deque(maxlen=app_config.get("queue_max_size", 100))
subtitle_lang_map: dict[str, str] = {}


class _TeeStdout:
    def __init__(self, original):
        self._orig = original

    def write(self, text):
        self._orig.write(text)
        self._orig.flush()
        stripped = text.rstrip("\n")
        if stripped:
            from datetime import datetime
            app_log.appendleft({
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "message": stripped,
            })

    def flush(self):
        self._orig.flush()

    def isatty(self):
        return self._orig.isatty()

    def __getattr__(self, name):
        return getattr(self._orig, name)


sys.stdout = _TeeStdout(sys.stdout)
sys.stderr = _TeeStdout(sys.stderr)

_arr_cache: dict[str, tuple] = {}
_arr_data_cache: dict = {}  # Persisted processed data: {"movies": [...], "series": [...]}
_ARR_DATA_FILE = Path(__file__).parent / "arr_data_cache.json"
app = FastAPI(title="Subtitle Downloader")


def _load_config():
    global app_config, VIDEO_ROOT
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            merged = dict(DEFAULT_CONFIG)
            merged.update(saved)
            app_config = merged
        except Exception:
            app_config = dict(DEFAULT_CONFIG)
    else:
        app_config = dict(DEFAULT_CONFIG)
    VIDEO_ROOT = Path(app_config.get("video_root", str(VIDEO_ROOT)))

    if "nas_path_prefix" in app_config and not app_config.get("radarr_path_mappings"):
        nas_prefix = app_config.pop("nas_path_prefix", "")
        if nas_prefix:
            app_config["radarr_path_mappings"] = [{"from": nas_prefix, "to": str(VIDEO_ROOT)}]


def _save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(app_config, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def _load_subtitle_cache():
    global _subtitle_status_cache
    if SUBTITLE_CACHE_FILE.exists():
        try:
            with open(SUBTITLE_CACHE_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            normalized = {}
            for k, v in raw.items():
                key = str(k)
                if isinstance(v, str):
                    normalized[key] = {"status": v}
                elif isinstance(v, dict):
                    normalized[key] = v
            _subtitle_status_cache = normalized
        except Exception:
            _subtitle_status_cache = {}


def _save_subtitle_cache():
    try:
        with open(SUBTITLE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_subtitle_status_cache, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def _load_arr_data_cache():
    global _arr_data_cache
    if _ARR_DATA_FILE.exists():
        try:
            with open(_ARR_DATA_FILE, "r", encoding="utf-8") as f:
                _arr_data_cache = json.load(f)
        except Exception:
            _arr_data_cache = {}


def _save_arr_data_cache():
    try:
        with open(_ARR_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(_arr_data_cache, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def _load_queue_data():
    global download_completed, translate_completed
    maxlen = app_config.get("queue_max_size", 100)
    if QUEUE_DATA_FILE.exists():
        try:
            with open(QUEUE_DATA_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            dl = raw.get("download_completed", [])
            tl = raw.get("translate_completed", [])
            download_completed = deque(dl[:maxlen], maxlen=maxlen)
            translate_completed = deque(tl[:maxlen], maxlen=maxlen)
        except Exception:
            download_completed = deque(maxlen=maxlen)
            translate_completed = deque(maxlen=maxlen)


def _save_queue_data():
    try:
        with open(QUEUE_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "download_completed": list(download_completed),
                "translate_completed": list(translate_completed),
            }, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def _detect_subtitle_lang(path: Path) -> str:
    try:
        # Fast path: check filename for language markers
        name_lower = path.name.lower()
        zh_markers = (
            ".zh.", "_zh.", ".chs.", "_chs.", ".chi.", "_chi.",
            ".cht.", "_cht.", ".zht.", "_zht.",
            ".cn.", "_cn.",
            "chs&", "chi&", "&chs", "&chi",
            "简体", "繁体", "简中", "繁中", "中文", "双语",
            "简.", ".简.", "繁.", ".繁.",
        )
        bilingual_markers = (".zh+en.", ".zht+en.", ".chs.eng.", ".cht.eng.")
        en_markers = (".en.", "_en.", ".eng.", "_eng.")
        for m in bilingual_markers:
            if m in name_lower:
                return "zh+en"
        for m in zh_markers:
            if m in name_lower:
                return "zh"
        for m in en_markers:
            if m in name_lower:
                return "en"

        # Fallback: scan file content (100 lines enough to handle ASS headers)
        # Detect encoding from BOM: UTF-16 LE (\\xff\\xfe), UTF-16 BE (\\xfe\\xff), UTF-8 BOM (\\xef\\xbb\\xbf)
        encoding = "utf-8"
        with open(path, "rb") as f:
            bom = f.read(3)
            raw_start = bom + f.read(200)  # peek ahead for null byte detection
        if bom[:2] == b"\xff\xfe":
            encoding = "utf-16-le"
        elif bom[:2] == b"\xfe\xff":
            encoding = "utf-16-be"
        elif bom[:3] == b"\xef\xbb\xbf":
            encoding = "utf-8-sig"
        elif b"\x00" in raw_start:
            # No BOM but contains null bytes → likely UTF-16 LE without BOM
            encoding = "utf-16-le"

        lines = []
        with open(path, "r", encoding=encoding, errors="replace") as f:
            for i, line in enumerate(f):
                if i >= 100:
                    break
                lines.append(line)
        text = "".join(lines)
        has_cjk = bool(re.search(r"[\u4e00-\u9fff\u3400-\u4dbf]", text))
        has_latin = bool(re.search(r"[a-zA-Z]{3,}", text))
        if has_cjk and has_latin:
            return "zh+en"
        elif has_cjk:
            return "zh"
        elif has_latin:
            return "en"
        return "unknown"
    except Exception:
        return "unknown"


def _has_subtitle(video_path: Path) -> bool:
    """Check if a video file already has a matching subtitle file."""
    try:
        if not video_path.exists() or not video_path.is_file():
            return False
    except PermissionError:
        return False
    stem = video_path.stem
    parent = video_path.parent
    for sub_ext in SUBTITLE_EXTENSIONS:
        for sub_file in parent.glob(f"{stem}*{sub_ext}"):
            if sub_file.is_file() and sub_file != video_path:
                return True
    return False


_EPISODE_RE = re.compile(r'[sS](\d{1,2})[eE](\d{1,3})')


def _extract_episode_key(stem: str) -> Optional[str]:
    """Extract normalized SxxExx key from a filename stem.

    Returns a lowercase normalized string like 's1e10' (int-padded) so that
    'S01E10' and 'S1E10' both map to the same key.  Returns None when the
    stem contains no episode identifier (e.g. movies).
    """
    m = _EPISODE_RE.search(stem)
    if m:
        return f"s{int(m.group(1))}e{int(m.group(2))}"
    return None


def _get_subtitle_status(video_path: Path) -> str:
    """Return subtitle status: 'zh' (has Chinese), 'en' (English only), 'none', 'no_file'."""
    try:
        if not video_path.exists() or not video_path.is_file():
            return "no_file"
    except PermissionError:
        return "no_file"
    stem = video_path.stem
    parent = video_path.parent
    found_zh = False
    found_en = False
    video_count = sum(1 for f in parent.iterdir() if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS)
    # First pass: match by video stem (fast, handles standard naming)
    for sub_ext in SUBTITLE_EXTENSIONS:
        for sub_file in parent.glob(f"{stem}*{sub_ext}"):
            if sub_file.is_file() and sub_file != video_path:
                lang = _detect_subtitle_lang(sub_file)
                if lang in ("zh", "zh+en"):
                    found_zh = True
                elif lang == "en":
                    found_en = True
    if found_zh:
        return "zh"
    # Second pass: relaxed stem matching (catches case mismatch, extra group tags,
    # slight naming variations) — but still require partial stem overlap to avoid
    # matching subtitles belonging to completely different videos in same directory.
    if not found_zh:
        stem_lower = stem.lower()
        vid_ep = _extract_episode_key(stem_lower)
        for sub_ext in SUBTITLE_EXTENSIONS:
            for sub_file in parent.glob(f"*{sub_ext}"):
                if sub_file.is_file() and sub_file != video_path:
                    sub_stem = sub_file.stem.lower()
                    sub_ep = _extract_episode_key(sub_stem)
                    # Match only if the subtitle name starts with the full video
                    # stem, or both share the same SxxExx episode identifier.
                    # This prevents E01's subtitle from matching E10 just because
                    # they share the show name as the first dot-segment.
                    if sub_stem.startswith(stem_lower) or (vid_ep and sub_ep and vid_ep == sub_ep):
                        lang = _detect_subtitle_lang(sub_file)
                        if lang in ("zh", "zh+en"):
                            found_zh = True
                            break
            if found_zh:
                break
    if not found_zh and video_count == 1:
        for sub_ext in SUBTITLE_EXTENSIONS:
            for sub_file in parent.glob(f"*{sub_ext}"):
                if sub_file.is_file() and sub_file != video_path:
                    lang = _detect_subtitle_lang(sub_file)
                    if lang in ("zh", "zh+en"):
                        found_zh = True
                        break
            if found_zh:
                break
    if found_zh:
        return "zh"
    if not found_en and video_count == 1:
        for sub_ext in SUBTITLE_EXTENSIONS:
            for sub_file in parent.glob(f"*{sub_ext}"):
                if sub_file.is_file() and sub_file != video_path:
                    lang = _detect_subtitle_lang(sub_file)
                    if lang == "en":
                        found_en = True
                        break
            if found_en:
                break
    if found_en:
        return "en"
    return "none"


def _arr_get(base_url: str, api_key: str, path: str, use_cache: bool = True):
    if not base_url or not api_key:
        return None
    cache_key = f"{base_url}:{path}"
    now = time.time()
    if use_cache and cache_key in _arr_cache:
        cached_data, cached_time = _arr_cache[cache_key]
        if now - cached_time < 30:
            return cached_data
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    headers = {"X-Api-Key": api_key}
    try:
        import requests
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        _arr_cache[cache_key] = (data, now)
        return data
    except Exception:
        return None


def _try_remap_path(path_str: str) -> Optional[Path]:
    p = Path(path_str)
    try:
        if p.exists():
            return p
    except PermissionError:
        pass

    path_norm = path_str.replace("\\", "/")

    mappings = []
    for m in app_config.get("radarr_path_mappings", []):
        mappings.append(m)
    for m in app_config.get("sonarr_path_mappings", []):
        mappings.append(m)

    for m in mappings:
        remote = m.get("from", "").replace("\\", "/").rstrip("/")
        local = m.get("to", "").replace("\\", "/").rstrip("/")
        if remote and path_norm.startswith(remote + "/"):
            local_path_str = local + path_norm[len(remote):]
            local_path = Path(local_path_str)
            try:
                if local_path.exists():
                    return local_path
            except PermissionError:
                continue

    return None


def _scan_subtitle_lang():
    global subtitle_lang_map
    count = 0
    if VIDEO_ROOT.is_dir():
        for ext in VIDEO_EXTENSIONS:
            for video in VIDEO_ROOT.rglob(f"*{ext}"):
                video_key = str(video)
                sub_found = []
                for sub_ext in SUBTITLE_EXTENSIONS:
                    stem = video.stem
                    parent = video.parent
                    for sub_file in parent.glob(f"{stem}*{sub_ext}"):
                        if sub_file.is_file():
                            lang = _detect_subtitle_lang(sub_file)
                            sub_found.append({"path": str(sub_file), "lang": lang})
                if sub_found:
                    subtitle_lang_map[video_key] = sub_found[-1]["lang"]
                    count += 1
                else:
                    subtitle_lang_map.pop(video_key, None)
    lang_file = Path(__file__).parent / "subtitle_lang.json"
    try:
        with open(lang_file, "w", encoding="utf-8") as f:
            json.dump(subtitle_lang_map, f, ensure_ascii=False)
    except Exception:
        pass
    return count


def _validate_path(relative_path: str) -> Path:
    resolved_root = VIDEO_ROOT.resolve()
    target = (VIDEO_ROOT / relative_path).resolve()
    if not str(target).startswith(str(resolved_root) + "/") and target != resolved_root:
        raise HTTPException(status_code=403, detail="Path traversal not allowed")
    if not target.exists():
        raise HTTPException(status_code=404, detail="Directory not found")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="Not a directory")
    return target


def _validate_video_path(relative_path: str) -> Path:
    resolved_root = VIDEO_ROOT.resolve()
    target = (VIDEO_ROOT / relative_path).resolve()
    if not str(target).startswith(str(resolved_root) + "/") and target != resolved_root:
        raise HTTPException(status_code=403, detail="Path traversal not allowed")
    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="Not a file")
    if target.suffix.lower() not in VIDEO_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Not a video file")
    return target


def _resolve_video_path(body: dict) -> tuple[Path, str]:
    relative_path = body.get("path", "")
    absolute_path = body.get("absolute_path", "")
    if absolute_path:
        target = Path(absolute_path).resolve()
        remapped = _try_remap_path(absolute_path)
        if remapped and remapped.is_file() and remapped.suffix.lower() in VIDEO_EXTENSIONS:
            return remapped, absolute_path
        if target.is_file() and target.suffix.lower() in VIDEO_EXTENSIONS:
            return target, absolute_path
        raise HTTPException(status_code=400, detail="Not a valid video file")
    video_path = _validate_video_path(relative_path)
    return video_path, relative_path


def _mask_sensitive(cfg: dict) -> dict:
    masked = dict(cfg)
    sensitive_keys = [
        "radarr_api_key", "sonarr_api_key", "baidu_ocr_api_key",
        "baidu_ocr_secret_key", "assrt_api_token", "opensubtitles_api_key",
        "opensubtitles_password", "ai_api_key", "tmdb_api_key",
        "dingtalk_webhook_url",
    ]
    for key in sensitive_keys:
        val = masked.get(key, "")
        if val and len(str(val)) > 4:
            masked[key] = "*" * (len(str(val)) - 4) + str(val)[-4:]
        elif val:
            masked[key] = "****"
    return masked


def _get_provider_kwargs(provider_name: str) -> dict:
    kwargs = {}
    if provider_name == "zimuku":
        if app_config.get("baidu_ocr_api_key"):
            kwargs["ocr_api_key"] = app_config["baidu_ocr_api_key"]
        if app_config.get("baidu_ocr_secret_key"):
            kwargs["ocr_secret_key"] = app_config["baidu_ocr_secret_key"]
    elif provider_name == "subhd":
        if app_config.get("baidu_ocr_api_key"):
            kwargs["ocr_api_key"] = app_config["baidu_ocr_api_key"]
        if app_config.get("baidu_ocr_secret_key"):
            kwargs["ocr_secret_key"] = app_config["baidu_ocr_secret_key"]
    elif provider_name == "assrt":
        if app_config.get("assrt_api_token"):
            kwargs["api_token"] = app_config["assrt_api_token"]
    elif provider_name == "opensubtitles":
        if app_config.get("opensubtitles_api_key"):
            kwargs["api_key"] = app_config["opensubtitles_api_key"]
        if app_config.get("opensubtitles_username"):
            kwargs["username"] = app_config["opensubtitles_username"]
        if app_config.get("opensubtitles_password"):
            kwargs["password"] = app_config["opensubtitles_password"]
    return kwargs


def _build_search_keyword(video_info: dict, video_path: Path) -> str:
    if video_info:
        imdb_id = video_info.get("imdb_id", "")
        if imdb_id:
            return imdb_id
        if video_info.get("scene_name"):
            return video_info["scene_name"]
        title = video_info.get("title", "") or video_info.get("plex_title", "")
        year = video_info.get("year")
        if title and year:
            return f"{title} {year}"
        if title:
            return title
    return video_path.stem


def _select_eligible_results(results: list, lang: str = "zho_chs",
                             video_type: str = "movie") -> list:
    if not results:
        return []

    max_count = app_config.get("max_download_count", 2)
    score_threshold_pct = app_config.get("score_threshold_pct", 10)

    _ZH_SIM = {"zho_chs", "zho", "chi", "chs", "zho_chs+eng"}
    _ZH_TRA = {"zho_cht", "cht", "zho_cht+eng"}
    _ZH_ALL = _ZH_SIM | _ZH_TRA
    _EN = {"eng", "en", "zho_chs+eng", "zho_cht+eng"}

    min_score_pct = float(app_config.get("min_score_pct", 5))

    def _tier_filter(tier_langs):
        return sorted(
            [r for r in results if getattr(r, "language", "") in tier_langs],
            key=lambda r: getattr(r, "score", 0) or 0,
            reverse=True,
        )

    def _threshold_pick(candidates):
        if not candidates:
            return []
        top_score = max(getattr(r, "score", 0) or 0 for r in candidates)
        if top_score == 0:
            return []
        top_pct = _score_to_pct(top_score, video_type)
        if top_pct < min_score_pct:
            return []
        threshold = top_score * (score_threshold_pct / 100.0)
        eligible = [r for r in candidates if hasattr(r, "score") and r.score >= threshold]
        if len(eligible) >= 2:
            diff_pct = ((eligible[0].score - eligible[1].score) / eligible[0].score * 100) if eligible[0].score > 0 else 100
            if diff_pct > score_threshold_pct:
                eligible = eligible[:1]
        return eligible[:max_count]

    if lang in _ZH_SIM:
        tier1 = _threshold_pick(_tier_filter(_ZH_SIM))
        if tier1:
            return tier1
        return _threshold_pick(_tier_filter(_ZH_TRA))

    if lang in _ZH_TRA:
        tier1 = _threshold_pick(_tier_filter(_ZH_TRA))
        if tier1:
            return tier1
        return _threshold_pick(_tier_filter(_ZH_SIM))

    if lang in _EN:
        return _threshold_pick(_tier_filter(_EN))

    return _threshold_pick(_tier_filter(_ZH_ALL))


def _select_en_supplement(results: list, video_type: str = "movie") -> list:
    if not results:
        return []

    max_count = app_config.get("max_download_count", 2)
    score_threshold_pct = app_config.get("score_threshold_pct", 10)
    min_score_pct = float(app_config.get("min_score_pct", 5))

    _EN = {"eng", "en", "zho_chs+eng", "zho_cht+eng"}
    en_results = sorted(
        [r for r in results if getattr(r, "language", "") in _EN],
        key=lambda r: getattr(r, "score", 0) or 0,
        reverse=True,
    )
    if not en_results:
        return []

    top_score = max(getattr(r, "score", 0) or 0 for r in en_results)
    if top_score == 0:
        return []
    top_pct = _score_to_pct(top_score, video_type)
    if top_pct < min_score_pct:
        return []
    threshold = top_score * (score_threshold_pct / 100.0)
    eligible = [r for r in en_results if hasattr(r, "score") and r.score >= threshold]
    if len(eligible) >= 2:
        diff_pct = ((eligible[0].score - eligible[1].score) / eligible[0].score * 100) if eligible[0].score > 0 else 100
        if diff_pct > score_threshold_pct:
            eligible = eligible[:1]
    return eligible[:max_count]


def _score_to_pct(score: float, video_type: str = "movie") -> float:
    from subtitle_providers.utils import MOVIE_MAX_SCORE, EPISODE_MAX_SCORE
    max_possible = EPISODE_MAX_SCORE if video_type == "episode" else MOVIE_MAX_SCORE
    return round(score / max_possible * 100, 2) if max_possible > 0 and score > 0 else 0.0


async def _search_providers(keyword: str, video_info: dict = None,
                            season: int = None, episode: int = None,
                            providers: list = None,
                            good_score_threshold: float = None) -> list:
    from subtitle_providers import get_provider, SubtitleResult

    if good_score_threshold is None:
        good_score_threshold = float(app_config.get("good_score_threshold", 72))

    fallback_order = app_config.get("provider_fallback_order", [])
    enabled = providers or app_config.get("enabled_providers", ["zimuku"])
    if not enabled:
        enabled = ["zimuku"]

    async def _search_one(pname: str):
        try:
            kwargs = _get_provider_kwargs(pname)
            provider = get_provider(pname, **kwargs)
            results = await asyncio.to_thread(
                provider.search, keyword, video_info, season, episode
            )
            return results
        except Exception:
            return []

    if app_config.get("concurrent_search") and len(enabled) > 1:
        tasks = [_search_one(p) for p in enabled]
        all_results = await asyncio.gather(*tasks)
        combined = []
        for results in all_results:
            combined.extend(results)
    else:
        ordered = []
        for p in fallback_order:
            if p in enabled:
                ordered.append(p)
        for p in enabled:
            if p not in ordered:
                ordered.append(p)

        combined = []
        video_type = video_info.get("type", "movie") if video_info else "movie"
        for pname in ordered:
            results = await _search_one(pname)
            combined.extend(results)
            max_score = max((r.score for r in results if hasattr(r, "score") and r.score), default=0)
            if max_score > 0 and _score_to_pct(max_score, video_type) >= good_score_threshold:
                break

    if video_info:
        from subtitle_providers.utils import compute_match_score
        video_type = video_info.get('type', 'movie')
        search_tokens = set(keyword.lower().replace(".", " ").replace("-", " ").split())
        for r in combined:
            if r.score > 0:
                continue
            score, _matched = compute_match_score(video_info, r.title, video_type)
            if score == 0 and search_tokens:
                title_lower = r.title.lower().replace(".", " ").replace("-", " ")
                title_tokens = set(title_lower.split())
                overlap = search_tokens & title_tokens
                if overlap:
                    ratio = len(overlap) / len(search_tokens)
                    score = max(10, int(40 * ratio))
            r.score = float(score)

    combined.sort(key=lambda r: r.score if hasattr(r, "score") and r.score else 0, reverse=True)
    return combined


def _has_active_download(video_path_str: str) -> bool:
    """Check if there's already an active (queued/downloading) task for this video."""
    for t in download_items.values():
        if t.get("video_path") == video_path_str and t.get("status") in ("queued", "downloading"):
            return True
    return False


async def _download_from_provider(provider_name: str, result, output_dir: Path,
                                  preferred_lang: str = "zho_chs",
                                  video_filename: str = "") -> tuple:
    from subtitle_providers import get_provider
    kwargs = _get_provider_kwargs(provider_name)
    provider = get_provider(provider_name, **kwargs)
    downloaded = await asyncio.to_thread(
        provider.download, result, output_dir, preferred_lang, video_filename
    )
    original_name = result.extra.get("_original_name", "") if hasattr(result, "extra") else ""
    return downloaded, original_name


def _cleanup_archives(output_dir: Path):
    for archive_ext in [".zip", ".rar", ".7z"]:
        for f in output_dir.glob(f"*{archive_ext}"):
            if f.is_file():
                try:
                    f.unlink()
                except OSError:
                    pass


@app.on_event("startup")
async def startup():
    _load_config()
    _load_subtitle_cache()
    _load_arr_data_cache()
    _load_queue_data()
    global VIDEO_ROOT
    VIDEO_ROOT = Path(app_config.get("video_root", str(VIDEO_ROOT)))

    lang_file = Path(__file__).parent / "subtitle_lang.json"
    if lang_file.exists():
        try:
            with open(lang_file, "r", encoding="utf-8") as f:
                global subtitle_lang_map
                subtitle_lang_map = json.load(f)
        except Exception:
            pass

    asyncio.create_task(_download_worker())
    asyncio.create_task(_translate_worker())

    if app_config.get("douban_enabled", False):
        asyncio.create_task(_douban_monitor_loop())


@app.get("/favicon.ico")
async def favicon():
    ico_path = Path(__file__).parent / "favicon.ico"
    if ico_path.exists():
        return FileResponse(ico_path, media_type="image/x-icon")
    return Response(status_code=404)


@app.get("/favicon-dark.ico")
async def favicon_dark():
    ico_path = Path(__file__).parent / "favicon-dark.ico"
    if ico_path.exists():
        return FileResponse(ico_path, media_type="image/x-icon")
    return Response(status_code=404)


@app.get("/")
async def index():
    html_path = Path(__file__).parent / "templates" / "index.html"
    try:
        content = html_path.read_text(encoding="utf-8")
        return HTMLResponse(content=content)
    except Exception:
        return HTMLResponse("<h1>Template not found</h1>", status_code=500)


@app.get("/api/config")
async def get_config():
    resolved_root = VIDEO_ROOT.resolve()
    roots = []
    if resolved_root.is_dir():
        for entry in sorted(resolved_root.iterdir()):
            if entry.is_dir() and not entry.name.startswith(".") and entry.name != "@eaDir":
                rel = str(entry.relative_to(resolved_root))
                roots.append({"name": entry.name, "path": rel})
    return {"video_root": str(VIDEO_ROOT), "roots": roots}


@app.get("/api/settings")
async def get_settings():
    return _mask_sensitive(app_config)


@app.put("/api/settings")
async def update_settings(request: Request):
    global VIDEO_ROOT
    body = await request.json()

    bool_keys = {"auto_download", "concurrent_search", "ai_bilingual", "ai_streaming", "ai_review_enabled"}
    int_keys = {"max_download_count", "ai_batch_size", "ai_max_retries", "ai_max_output_tokens", "queue_max_size", "ai_review_timeout"}
    float_keys = {"ai_retry_delay", "ai_temperature", "score_threshold_pct", "good_score_threshold", "chinese_score_english_fallback", "chinese_scarce_buffer", "min_score_pct"}
    int_keys = int_keys | {"chinese_scarce_count"}

    sensitive_keys = [
        "radarr_api_key", "sonarr_api_key", "baidu_ocr_api_key",
        "baidu_ocr_secret_key", "assrt_api_token", "opensubtitles_api_key",
        "opensubtitles_password", "ai_api_key", "tmdb_api_key",
        "dingtalk_webhook_url",
    ]

    for key, value in body.items():
        if key in sensitive_keys and isinstance(value, str) and value.startswith("****"):
            continue
        if key in bool_keys and isinstance(value, str):
            value = value.lower() in ("true", "1", "yes")
        elif key in int_keys and isinstance(value, str):
            try:
                value = int(value)
            except ValueError:
                continue
        elif key in float_keys and isinstance(value, str):
            try:
                value = float(value)
            except ValueError:
                continue
        app_config[key] = value

    if "douban_check_interval_hours" in body:
        try:
            app_config["douban_check_interval_hours"] = int(body["douban_check_interval_hours"])
        except (ValueError, TypeError):
            pass

    if "video_root" in body:
        VIDEO_ROOT = Path(app_config["video_root"])

    if "subtitle_provider" in body or "enabled_providers" in body:
        default = app_config.get("subtitle_provider", "zimuku")
        enabled = app_config.get("enabled_providers", ["zimuku"])
        new_order = [default] if default in enabled else []
        for p in enabled:
            if p not in new_order:
                new_order.append(p)
        app_config["provider_fallback_order"] = new_order

    _save_config()

    if "queue_max_size" in body:
        global download_completed, translate_completed
        new_max = app_config["queue_max_size"]
        download_completed = deque(list(download_completed)[:new_max], maxlen=new_max)
        translate_completed = deque(list(translate_completed)[:new_max], maxlen=new_max)
        _save_queue_data()

    return _mask_sensitive(app_config)


@app.get("/api/browse/{path:path}")
async def browse(path: str):
    target = _validate_path(path)
    resolved_root = VIDEO_ROOT.resolve()

    entries = []
    for entry in target.iterdir():
        entry_resolved = entry.resolve()
        name = entry.name

        if name.startswith(".") or name == "@eaDir" or name == ".@__thumb":
            continue

        if not str(entry_resolved).startswith(str(resolved_root) + "/") and entry_resolved != resolved_root:
            continue

        if entry_resolved.is_dir():
            entries.append({
                "name": name,
                "type": "directory",
            })
        elif entry_resolved.is_file():
            ext = entry_resolved.suffix.lower()
            if ext not in VIDEO_EXTENSIONS and ext not in SUBTITLE_EXTENSIONS:
                continue
            sub_lang = None
            if ext in SUBTITLE_EXTENSIONS:
                sub_lang = subtitle_lang_map.get(str(entry_resolved), _detect_subtitle_lang(entry_resolved))
            entries.append({
                "name": name,
                "type": "file",
                "size": entry_resolved.stat().st_size,
                "extension": ext,
                "has_subtitle": ext in SUBTITLE_EXTENSIONS,
                "subtitle_lang": sub_lang,
            })

    entries.sort(key=lambda e: (0 if e["type"] == "directory" else 1, e["name"].lower()))

    rel_path = str(target.relative_to(resolved_root))
    if rel_path == ".":
        rel_path = ""

    parent = None
    if target != resolved_root:
        parent_rel = str(target.parent.relative_to(resolved_root))
        parent = None if parent_rel == "." else parent_rel

    return {
        "path": rel_path,
        "parent": parent,
        "entries": entries,
    }


@app.get("/api/movies")
async def get_movies(hide_deleted: bool = True):
    movies = _arr_data_cache.get("movies", [])
    if hide_deleted:
        movies = [m for m in movies if m.get("hasFile")]
    return movies


@app.get("/api/series")
async def get_series():
    return _arr_data_cache.get("series", [])


@app.get("/api/series/{series_id}/episodes")
async def get_episodes(series_id: int, season: int = None):
    sonarr_url = app_config.get("sonarr_url", "")
    sonarr_key = app_config.get("sonarr_api_key", "")

    efile_data = _arr_get(sonarr_url, sonarr_key, f"/api/v3/episodefile?seriesId={series_id}")
    efile_map = {}
    if efile_data:
        for ef in efile_data:
            efile_map[ef.get("id")] = ef

    ep_path = f"/api/v3/episode?seriesId={series_id}"
    if season is not None:
        ep_path += f"&seasonNumber={season}"
    data = _arr_get(sonarr_url, sonarr_key, ep_path, use_cache=False)
    if data is None:
        return []
    result = []
    for ep in data:
        if season is not None and ep.get("seasonNumber") != season:
            continue
        ef_id = ep.get("episodeFileId", 0)
        ef = efile_map.get(ef_id, {}) if ef_id else {}
        ep_id = ep.get("id")
        cache_key = f"ep_{ep_id}"
        sub_status = _get_cached_status(cache_key)

        result.append({
            "title": ep.get("title", ""),
            "seasonNumber": ep.get("seasonNumber"),
            "episodeNumber": ep.get("episodeNumber"),
            "hasFile": ep.get("hasFile", False),
            "monitored": ep.get("monitored", False),
            "airDate": ep.get("airDate", ""),
            "id": ep_id,
            "path": ef.get("path", ""),
            "sceneName": ef.get("sceneName", ""),
            "subtitleStatus": sub_status,
        })
    return result


@app.post("/api/search")
async def search_subtitles(request: Request):
    body = await request.json()
    keyword = body.get("keyword", "")
    video_info = body.get("video_info")
    season = body.get("season")
    episode = body.get("episode")
    providers = body.get("providers")

    if not keyword and video_info:
        title = video_info.get("title", "")
        year = video_info.get("year")
        if title and year:
            keyword = f"{title} {year}"
        elif title:
            keyword = title

    if not keyword:
        return {"results": [], "error": "No keyword provided"}

    try:
        results = await _search_providers(keyword, video_info, season, episode, providers)
    except Exception as e:
        return {"results": [], "error": str(e)}

    serialized = []
    for r in results:
        item = {
            "title": r.title,
            "language": r.language,
            "download_url": r.download_url,
            "provider": r.provider,
            "score": r.score,
            "page_url": r.page_url,
            "extra": r.extra if hasattr(r, "extra") else {},
        }
        serialized.append(item)

    return {"results": serialized}


@app.post("/api/search/video")
async def search_subtitles_by_video(request: Request):
    try:
        body = await request.json()
    except Exception:
        return {"results": [], "error": "Invalid JSON body"}

    video_path_str = body.get("video_path", "")
    if not video_path_str:
        return {"results": [], "error": "No video_path provided"}

    try:
        remapped = _try_remap_path(video_path_str)
        video_path = remapped if remapped else Path(video_path_str)
        if not video_path.is_file() or video_path.suffix.lower() not in VIDEO_EXTENSIONS:
            return {"results": [], "error": "Not a valid video file: " + video_path_str}

        from subtitle_providers.utils import parse_filename
        video_info = parse_filename(video_path.name)

        if body.get("title"):
            video_info["plex_title"] = body["title"]
        year_val = body.get("year")
        if year_val is not None and year_val != "":
            try:
                video_info["year"] = int(year_val)
            except (ValueError, TypeError):
                pass
        if body.get("imdb_id"):
            video_info["imdb_id"] = body["imdb_id"]
        if body.get("scene_name"):
            video_info["scene_name"] = body["scene_name"]
        season_val = body.get("season")
        if season_val is not None and season_val != "":
            try:
                video_info["season"] = int(season_val)
                video_info["type"] = "episode"
            except (ValueError, TypeError):
                pass
        episode_val = body.get("episode")
        if episode_val is not None and episode_val != "":
            try:
                video_info["episode"] = int(episode_val)
                video_info["type"] = "episode"
            except (ValueError, TypeError):
                pass

        keyword = _build_search_keyword(video_info, video_path)
        season = body.get("season")
        episode = body.get("episode")
        providers = body.get("providers")

        results = await _search_providers(keyword, video_info, season, episode, providers)

        video_type = video_info.get("type", "movie") if video_info else "movie"

        if video_info:
            from subtitle_providers.utils import compute_match_score
            search_tokens = set(keyword.lower().replace(".", " ").replace("-", " ").split())
            for r in results:
                score, _matched = compute_match_score(video_info, r.title, video_type)
                if score == 0 and search_tokens:
                    title_lower = r.title.lower().replace(".", " ").replace("-", " ")
                    title_tokens = set(title_lower.split())
                    overlap = search_tokens & title_tokens
                    if overlap:
                        ratio = len(overlap) / len(search_tokens)
                        score = max(10, int(40 * ratio))
                r.score = float(score)
            results.sort(key=lambda r: r.score if hasattr(r, "score") and r.score else 0, reverse=True)

        search_id = str(uuid.uuid4())
        _search_result_cache[search_id] = list(results)
        if len(_search_result_cache) > 50:
            oldest = list(_search_result_cache.keys())[:25]
            for k in oldest:
                _search_result_cache.pop(k, None)

        serialized = []
        for i, r in enumerate(results):
            safe_extra = {}
            if hasattr(r, "extra") and isinstance(r.extra, dict):
                for k, v in r.extra.items():
                    try:
                        json.dumps(v)
                        safe_extra[k] = v
                    except (TypeError, ValueError):
                        pass
            item = {
                "index": i,
                "title": r.title,
                "language": r.language,
                "download_url": r.download_url,
                "provider": r.provider,
                "score": r.score,
                "score_pct": _score_to_pct(r.score, video_type),
                "page_url": r.page_url,
                "extra": safe_extra,
            }
            serialized.append(item)

        return {"search_id": search_id, "results": serialized, "keyword": keyword, "video_type": video_type}
    except Exception as e:
        return {"results": [], "error": str(e)}


@app.post("/api/download/selected")
async def download_selected_subtitle(request: Request):
    try:
        body = await request.json()
    except Exception:
        return {"status": "error", "error": "Invalid JSON body"}

    video_path_str = body.get("video_path", "")
    search_id = body.get("search_id", "")
    result_index = body.get("result_index")
    if not video_path_str or not search_id or result_index is None:
        return {"status": "error", "error": "Missing video_path, search_id, or result_index"}

    cached_results = _search_result_cache.get(search_id)
    if cached_results is None:
        return {"status": "error", "error": "Search results expired, please search again"}

    try:
        result_index = int(result_index)
    except (ValueError, TypeError):
        return {"status": "error", "error": "Invalid result_index"}

    if result_index < 0 or result_index >= len(cached_results):
        return {"status": "error", "error": "Result index out of range"}

    try:
        result = cached_results[result_index]

        remapped = _try_remap_path(video_path_str)
        video_path = remapped if remapped else Path(video_path_str)
        if not video_path.is_file() or video_path.suffix.lower() not in VIDEO_EXTENSIONS:
            return {"status": "error", "error": "Not a valid video file: " + video_path_str}

        output_dir = video_path.parent
        provider_name = result.provider or "zimuku"
        lang = getattr(result, "language", "") or body.get("lang", app_config.get("auto_download_lang", "zho_chs"))

        downloaded, original_name = await _download_from_provider(
            provider_name, result, output_dir, lang, video_path.name
        )
        now_iso = datetime.now().isoformat()
        if downloaded and downloaded.suffix in SUBTITLE_EXTENSIONS:
            _cleanup_archives(output_dir)
            manual_stem = downloaded.stem + ".manual"
            manual_path = downloaded.with_name(manual_stem + downloaded.suffix)
            try:
                manual_path.write_bytes(downloaded.read_bytes())
                downloaded.unlink()
                downloaded = manual_path
            except OSError:
                pass
            new_status = _get_subtitle_status(video_path)
            movie_id = body.get("movie_id")
            if movie_id:
                _set_cached_status(str(movie_id), new_status, video_path=str(video_path))
                for m in _arr_data_cache.get("movies", []):
                    if m.get("id") == movie_id:
                        m["subtitleStatus"] = new_status
                        break
            ep_id = body.get("episode_id")
            if ep_id:
                _set_cached_status(f"ep_{ep_id}", new_status, video_path=str(video_path))
                for s in _arr_data_cache.get("series", []):
                    for seas in s.get("seasons", []):
                        for ep in seas.get("episodes", []):
                            if ep.get("id") == ep_id:
                                ep["subtitleStatus"] = new_status
                                break
            _save_subtitle_cache()
            video_type = "episode" if ep_id else "movie"
            score_pct = _score_to_pct(result.score, video_type)
            task_id = f"dl_{int(time.time()*1000)}_{id(body)}"
            task = {
                "id": task_id,
                "video_path": str(video_path),
                "display_path": video_path_str,
                "lang": lang,
                "movie_id": movie_id,
                "episode_id": ep_id,
                "status": "success",
                "created_at": now_iso,
                "completed_at": now_iso,
                "subtitle_path": str(downloaded),
                "provider": provider_name,
                "provider_results": [{"provider": provider_name, "score": score_pct, "status": "success", "reason": "", "original_name": original_name}],
                "source": "manual-search",
            }
            download_items[task_id] = task
            download_completed.appendleft(dict(task))
            download_log.appendleft({
                "timestamp": now_iso,
                "path": video_path_str,
                "status": "success",
                "subtitle_path": str(downloaded),
                "error": "",
                "duration_ms": 0,
                "provider": provider_name,
            })
            _save_queue_data()
            return {"status": "success", "subtitle_path": str(downloaded), "subtitle_status": new_status, "task_id": task_id}
        elif not downloaded:
            return {"status": "failed", "error": f"下载失败: {provider_name} 无法获取字幕文件（可能是网络连接问题）"}
        else:
            _cleanup_archives(output_dir)
            return {"status": "failed", "error": f"下载失败: {provider_name} 返回了 {downloaded.suffix} 文件，无法提取字幕"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/api/download")
async def download_subtitle(request: Request):
    body = await request.json()
    try:
        video_path, display_path = _resolve_video_path(body)
    except HTTPException as e:
        return {"status": "error", "error": e.detail}

    lang = body.get("lang", app_config.get("auto_download_lang", "zho_chs"))
    video_info = body.get("video_info")

    task_id = f"dl_{int(time.time()*1000)}_{id(body)}"
    task = {
        "id": task_id,
        "video_path": str(video_path),
        "display_path": display_path,
        "lang": lang,
        "video_info": video_info,
        "movie_id": body.get("movie_id"),
        "episode_id": body.get("episode_id"),
        "status": "queued",
        "created_at": datetime.now().isoformat(),
    }
    download_items[task_id] = task
    await download_queue.put(task_id)

    return {"task_id": task_id, "status": "queued"}


@app.post("/api/download/top")
async def download_top(request: Request):
    body = await request.json()
    video_path_str = body.get("video_path", "")
    lang = body.get("lang", app_config.get("auto_download_lang", "zho_chs"))

    if not video_path_str:
        return {"status": "error", "error": "No video_path provided"}

    remapped = _try_remap_path(video_path_str)
    video_path = remapped if remapped else Path(video_path_str)
    if not video_path.is_file() or video_path.suffix.lower() not in VIDEO_EXTENSIONS:
        return {"status": "error", "error": "Not a valid video file"}

    if _get_subtitle_status(video_path) == "zh" and not body.get("force"):
        return {"status": "skipped", "error": "Chinese subtitle already exists", "tasks": []}

    from subtitle_providers.utils import parse_filename
    video_info = parse_filename(video_path.name)

    task_id = f"dl_{int(time.time()*1000)}_{id(body)}"
    task = {
        "id": task_id,
        "video_path": str(video_path),
        "display_path": video_path_str,
        "lang": lang,
        "video_info": video_info,
        "movie_id": body.get("movie_id"),
        "episode_id": body.get("episode_id"),
        "force": body.get("force", False),
        "status": "queued",
        "created_at": datetime.now().isoformat(),
    }
    download_items[task_id] = task
    await download_queue.put(task_id)

    return {"status": "queued", "task_id": task_id}


@app.get("/api/download/status")
async def download_status():
    active = [t for t in download_items.values() if t.get("status") in ("queued", "downloading")]
    completed = list(download_completed)
    return {"active": active, "completed": completed}


@app.post("/api/translate")
async def translate_subtitle(request: Request):
    body = await request.json()
    video_path_str = body.get("video_path", "")
    subtitle_path_str = body.get("subtitle_path", "")
    source_lang = body.get("source_lang", "")

    if not video_path_str and not subtitle_path_str:
        return {"status": "error", "error": "No video_path or subtitle_path provided"}

    sub_path = None
    if subtitle_path_str:
        sub_path = Path(subtitle_path_str)
        if not sub_path.exists():
            remapped = _try_remap_path(subtitle_path_str)
            if remapped:
                sub_path = remapped
        if not sub_path.exists():
            return {"status": "error", "error": "Subtitle file not found"}
    else:
        try:
            from subtitle_translator import find_subtitle_for_video
            video_p = Path(video_path_str)
            remapped = _try_remap_path(video_path_str)
            if remapped:
                video_p = remapped
            prefer = source_lang if source_lang else None
            found = await asyncio.to_thread(find_subtitle_for_video, str(video_p), prefer)
            if found:
                sub_path = found
        except Exception:
            pass

    if not sub_path:
        return {"status": "error", "error": "No subtitle found for video"}

    if not source_lang:
        source_lang = _detect_subtitle_lang(sub_path)
        if source_lang in ("zh", "zh+en"):
            source_lang = "Chinese"
        elif source_lang == "en":
            source_lang = "English"
        else:
            source_lang = app_config.get("ai_source_lang", "English")

    task_id = f"tr_{int(time.time()*1000)}_{id(body)}"
    task = {
        "id": task_id,
        "subtitle_path": str(sub_path),
        "video_path": video_path_str,
        "source_lang": source_lang,
        "status": "queued",
        "created_at": datetime.now().isoformat(),
    }
    translate_items[task_id] = task
    await translate_queue.put(task_id)

    return {"task_id": task_id, "status": "queued"}


@app.get("/api/translate/status")
async def translate_status():
    active = [t for t in translate_items.values() if t.get("status") in ("queued", "translating")]
    completed = list(translate_completed)
    return {"active": active, "completed": completed}


@app.get("/api/video/subtitles")
async def video_subtitles(path: str = ""):
    if not path:
        return {"subtitles": []}
    try:
        from subtitle_translator import list_subtitles_for_video
        video_p = Path(path)
        remapped = _try_remap_path(path)
        if remapped:
            video_p = remapped
        subs = await asyncio.to_thread(list_subtitles_for_video, str(video_p))
        return {"subtitles": subs}
    except Exception as e:
        return {"subtitles": [], "error": str(e)}


@app.post("/api/scan-subtitle-lang")
async def scan_subtitle_lang():
    try:
        count = await asyncio.to_thread(_scan_subtitle_lang)
        return {"status": "ok", "videos_with_subtitles": count}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/api/refresh")
async def refresh_data(request: Request):
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    target = body.get("target", "all")
    new_movies = 0
    new_episodes = 0
    removed_movies = 0
    removed_episodes = 0

    if target in ("all", "movies"):
        radarr_url = app_config.get("radarr_url", "")
        radarr_key = app_config.get("radarr_api_key", "")
        data = _arr_get(radarr_url, radarr_key, "/api/v3/movie", use_cache=False)
        if data is not None:
            old_ids = {m["id"] for m in _arr_data_cache.get("movies", [])}
            current_ids = set()
            result = []
            for m in data:
                has_file = m.get("hasFile", False)
                movie_id = m.get("id")
                current_ids.add(movie_id)
                poster_url = ""
                for img in m.get("images", []):
                    if img.get("coverType") == "poster":
                        poster_url = img.get("remoteUrl", img.get("url", ""))
                        break
                mf = m.get("movieFile", {}) or {}
                sub_status = _get_cached_status(str(movie_id))
                entry = _subtitle_status_cache.get(str(movie_id))
                needs_path = False
                if isinstance(entry, dict) and not entry.get("video_path"):
                    needs_path = True
                if str(movie_id) not in _subtitle_status_cache and has_file:
                    needs_path = True
                if needs_path and has_file:
                    file_path = mf.get("path", "")
                    remapped = _try_remap_path(file_path) if file_path else None
                    effective = str(remapped) if remapped else file_path
                    if effective:
                        vp = Path(effective)
                        if vp.exists():
                            old_status = sub_status
                            sub_status = _get_subtitle_status(vp)
                            _set_cached_status(str(movie_id), sub_status,
                                                video_path=effective,
                                                title=m.get("title", ""),
                                                year=m.get("year"),
                                                last_checked=datetime.now().isoformat())
                            if old_status == "none" and sub_status in ("zh", "en"):
                                new_movies += 1
                result.append({
                    "title": m.get("title", ""),
                    "year": m.get("year"),
                    "path": m.get("path", ""),
                    "filePath": mf.get("path", ""),
                    "hasFile": has_file,
                    "monitored": m.get("monitored", False),
                    "status": m.get("status", ""),
                    "id": movie_id,
                    "poster": poster_url,
                    "dateAdded": mf.get("dateAdded", m.get("added", "")),
                    "releaseDate": (m.get("physicalRelease") or m.get("digitalRelease") or m.get("inCinemas") or "")[:10],
                    "imdbId": m.get("imdbId", ""),
                    "tmdbId": m.get("tmdbId"),
                    "sceneName": mf.get("sceneName", ""),
                    "subtitleStatus": sub_status,
                    "overview": m.get("overview", ""),
                    "genres": m.get("genres", []),
                })
            result.sort(key=lambda x: x.get("dateAdded") or "", reverse=True)
            removed_movie_ids = old_ids - current_ids
            for rid in removed_movie_ids:
                _subtitle_status_cache.pop(str(rid), None)
                removed_movies += 1
            _arr_data_cache["movies"] = result

    if target in ("all", "episodes"):
        sonarr_url = app_config.get("sonarr_url", "")
        sonarr_key = app_config.get("sonarr_api_key", "")
        sdata = _arr_get(sonarr_url, sonarr_key, "/api/v3/series", use_cache=False)
        if sdata is not None:
            old_series_ids = {s["id"] for s in _arr_data_cache.get("series", [])}
            current_series_ids = set()
            series_result = []
            for s in sdata:
                poster_url = ""
                for img in s.get("images", []):
                    if img.get("coverType") == "poster":
                        poster_url = img.get("remoteUrl", img.get("url", ""))
                        break
                stats = s.get("statistics", {})
                seasons_info = []
                for season in s.get("seasons", []):
                    sn = season.get("seasonNumber", 0)
                    sstats = season.get("statistics", {})
                    seasons_info.append({
                        "seasonNumber": sn,
                        "episodeCount": sstats.get("episodeCount", 0),
                        "episodeFileCount": sstats.get("episodeFileCount", 0),
                        "monitored": season.get("monitored", False),
                    })
                series_id = s.get("id")
                current_series_ids.add(series_id)
                series_result.append({
                    "title": s.get("title", ""),
                    "year": s.get("year"),
                    "path": s.get("path", ""),
                    "monitored": s.get("monitored", False),
                    "status": s.get("status", ""),
                    "seasonCount": stats.get("seasonCount", 0),
                    "episodeCount": stats.get("episodeCount", 0),
                    "episodeFileCount": stats.get("episodeFileCount", 0),
                    "id": series_id,
                    "poster": poster_url,
                    "imdbId": s.get("imdbId", ""),
                    "tvdbId": s.get("tvdbId"),
                    "seasons": seasons_info,
                    "overview": s.get("overview", ""),
                    "genres": s.get("genres", []),
                })
                efile_data = _arr_get(sonarr_url, sonarr_key, f"/api/v3/episodefile?seriesId={series_id}", use_cache=False)
                if efile_data:
                    for ef in efile_data:
                        file_path = ef.get("path", "")
                        remapped = _try_remap_path(file_path) if file_path else None
                        effective = str(remapped) if remapped else file_path
                        if not effective:
                            continue
                        vp = Path(effective)
                        if not vp.exists():
                            continue
                        ef_id = ef.get("id")
                        cache_key = f"ep_{ef_id}"
                        ep_entry = _subtitle_status_cache.get(cache_key)
                        needs_path = False
                        if isinstance(ep_entry, dict) and not ep_entry.get("video_path"):
                            needs_path = True
                        if cache_key not in _subtitle_status_cache:
                            needs_path = True
                        if needs_path:
                            sub_status = _get_subtitle_status(vp)
                            _set_cached_status(cache_key, sub_status,
                                                video_path=effective,
                                                last_checked=datetime.now().isoformat())
                            if cache_key not in _subtitle_status_cache:
                                new_episodes += 1
            removed_series_ids = old_series_ids - current_series_ids
            for _rid in removed_series_ids:
                removed_episodes += 1
            _arr_data_cache["series"] = series_result

    _save_subtitle_cache()
    _save_arr_data_cache()
    return {
        "status": "ok",
        "new_movies": new_movies,
        "new_episodes": new_episodes,
        "removed_movies": removed_movies,
        "removed_episodes": removed_episodes,
    }


@app.post("/api/sync-subtitles")
async def sync_subtitles(request: Request):
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    target = body.get("target", "all")
    force = body.get("force", False)

    results = {"movies": [], "episodes": []}
    now_iso = datetime.now().isoformat()

    if target in ("all", "movies"):
        for cache_key, entry in list(_subtitle_status_cache.items()):
            if cache_key.startswith("ep_"):
                continue
            status = entry.get("status", "none") if isinstance(entry, dict) else entry
            if status in ("zh", "en") and not force:
                continue
            video_path = entry.get("video_path", "") if isinstance(entry, dict) else ""
            if not video_path:
                continue
            vp = Path(video_path)
            if not vp.exists():
                continue
            sub_status = _get_subtitle_status(vp)
            _set_cached_status(cache_key, sub_status,
                               last_checked=now_iso)
            sub_status = _get_cached_status(cache_key)
            for m in _arr_data_cache.get("movies", []):
                if str(m.get("id")) == cache_key:
                    m["subtitleStatus"] = sub_status
                    break
            if sub_status == "none":
                results["movies"].append({
                    "id": int(cache_key) if cache_key.isdigit() else cache_key,
                    "title": entry.get("title", "") if isinstance(entry, dict) else "",
                    "year": entry.get("year") if isinstance(entry, dict) else None,
                    "path": video_path,
                    "subtitleStatus": sub_status,
                })

    if target in ("all", "episodes"):
        for cache_key, entry in list(_subtitle_status_cache.items()):
            if not cache_key.startswith("ep_"):
                continue
            status = entry.get("status", "none") if isinstance(entry, dict) else entry
            if status in ("zh", "en") and not force:
                continue
            video_path = entry.get("video_path", "") if isinstance(entry, dict) else ""
            if not video_path:
                continue
            vp = Path(video_path)
            if not vp.exists():
                continue
            sub_status = _get_subtitle_status(vp)
            _set_cached_status(cache_key, sub_status,
                               last_checked=now_iso)
            sub_status = _get_cached_status(cache_key)
            if sub_status == "none":
                results["episodes"].append({
                    "cache_key": cache_key,
                    "path": video_path,
                    "subtitleStatus": sub_status,
                })

    total = len(results["movies"]) + len(results["episodes"])
    _save_subtitle_cache()
    return {"status": "ok", "total": total, "results": results}


@app.get("/api/logs")
async def get_logs():
    return list(download_log)


@app.get("/api/app-logs")
async def get_app_logs():
    return list(app_log)


@app.delete("/api/app-logs")
async def clear_app_logs():
    app_log.clear()
    return {"status": "cleared", "deleted": True}


@app.delete("/api/logs")
async def clear_logs():
    download_log.clear()
    return {"status": "cleared", "deleted": True}


@app.post("/api/test-connection")
async def test_connection(request: Request):
    body = await request.json()
    service = body.get("service", "")
    if service == "radarr":
        url = app_config.get("radarr_url", "").rstrip("/")
        key = app_config.get("radarr_api_key", "")
    else:
        url = app_config.get("sonarr_url", "").rstrip("/")
        key = app_config.get("sonarr_api_key", "")
    if not url or not key:
        return {"ok": False, "error": "请先保存 URL 和 API Key"}
    test_url = f"{url}/api/v3/system/status?apikey={key}"
    try:
        import urllib.request, urllib.error
        resp = await asyncio.to_thread(urllib.request.urlopen, test_url, timeout=10)
        data = json.loads(resp.read())
        return {"ok": True, "version": data.get("version", "")}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/api/test-ai")
async def test_ai_connection(request: Request):
    body = await request.json()
    api_url = body.get("api_url", "") or app_config.get("ai_api_url", "https://api.openai.com/v1")
    api_key = body.get("api_key", "") or app_config.get("ai_api_key", "")
    model = body.get("model", "") or app_config.get("ai_model", "gpt-4o-mini")

    if not api_url or not api_key:
        return {"ok": False, "error": "请先填写 API URL 和 API Key"}

    url = f"{api_url.rstrip('/')}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": "Hello, reply with just OK."},
        ],
        "max_tokens": 10,
        "stream": False,
    }
    try:
        import requests as req
        resp = await asyncio.to_thread(
            req.post, url, headers=headers, json=payload, timeout=30
        )
        if resp.status_code >= 400:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text[:500]
            return {"ok": False, "error": f"HTTP {resp.status_code}: {detail}"}
        try:
            data = resp.json()
        except Exception:
            data = None
        if not data or not isinstance(data, dict):
            return {"ok": False, "error": f"HTTP {resp.status_code}: 响应非 JSON (body: {resp.text[:300]})"}
        reply = ""
        if "choices" in data and data["choices"]:
            reply = (data["choices"][0].get("message") or {}).get("content", "")
        return {"ok": True, "model": model, "reply": reply.strip(), "raw_status": resp.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/webhook/radarr")
async def webhook_radarr(request: Request):
    if not app_config.get("auto_download", False):
        return {"status": "ignored", "reason": "auto_download disabled"}

    body = await request.json()
    event_type = body.get("eventType", "")
    if event_type not in ("Download", "MovieFileImported"):
        return {"status": "ignored", "event": event_type}

    movie_file = body.get("movieFile", {}) or body.get("movie", {}).get("movieFile", {})
    file_path = movie_file.get("path", "") or body.get("movie", {}).get("path", "")
    if not file_path:
        return {"status": "ignored", "reason": "no file path"}

    video_path = Path(file_path)
    try:
        path_exists = video_path.exists()
    except PermissionError:
        path_exists = False
    if not path_exists:
        remapped = _try_remap_path(file_path)
        if remapped:
            video_path = remapped
        else:
            return {"status": "ignored", "reason": "file not found"}

    if video_path.suffix.lower() not in VIDEO_EXTENSIONS:
        return {"status": "ignored", "reason": "not a video file"}

    if _get_subtitle_status(video_path) == "zh":
        return {"status": "skipped", "reason": "Chinese subtitle already exists"}

    if _has_active_download(str(video_path)):
        return {"status": "ignored", "reason": "download already in progress"}

    from subtitle_providers.utils import parse_filename
    video_info = parse_filename(video_path.name)

    movie = body.get("movie", {})
    remote = body.get("remoteMovie", {})
    video_info["imdb_id"] = remote.get("imdbId") or movie.get("imdbId") or ""
    video_info["tmdb_id"] = remote.get("tmdbId") or movie.get("tmdbId")
    if movie.get("title"):
        video_info["title"] = movie["title"]
    if movie.get("year"):
        video_info["year"] = movie["year"]
    scene_name = movie_file.get("sceneName", "")
    if scene_name:
        video_info["scene_name"] = scene_name
    task_id = f"dl_{int(time.time()*1000)}_{id(body)}"
    task = {
        "id": task_id,
        "video_path": str(video_path),
        "display_path": file_path,
        "lang": app_config.get("auto_download_lang", "zho_chs"),
        "video_info": video_info,
        "movie_id": movie.get("id"),
        "status": "queued",
        "created_at": datetime.now().isoformat(),
        "source": "radarr_webhook",
    }
    download_items[task_id] = task
    await download_queue.put(task_id)

    return {"status": "queued", "task_id": task_id, "title": body.get("movie", {}).get("title", "")}


@app.post("/api/webhook/sonarr")
async def webhook_sonarr(request: Request):
    if not app_config.get("auto_download", False):
        return {"status": "ignored", "reason": "auto_download disabled"}

    body = await request.json()
    event_type = body.get("eventType", "")
    if event_type not in ("Download", "EpisodeFileImported"):
        return {"status": "ignored", "event": event_type}

    ep_file = body.get("episodeFile", {}) or {}
    file_path = ep_file.get("path", "")
    if not file_path:
        return {"status": "ignored", "reason": "no file path"}

    video_path = Path(file_path)
    try:
        path_exists = video_path.exists()
    except PermissionError:
        path_exists = False
    if not path_exists:
        remapped = _try_remap_path(file_path)
        if remapped:
            video_path = remapped
        else:
            return {"status": "ignored", "reason": "file not found"}

    if video_path.suffix.lower() not in VIDEO_EXTENSIONS:
        return {"status": "ignored", "reason": "not a video file"}

    if _get_subtitle_status(video_path) == "zh":
        return {"status": "skipped", "reason": "Chinese subtitle already exists"}

    if _has_active_download(str(video_path)):
        return {"status": "ignored", "reason": "download already in progress"}

    from subtitle_providers.utils import parse_filename
    video_info = parse_filename(video_path.name)

    series = body.get("series", {})
    season_num = body.get("episodes", [{}])[0].get("seasonNumber") if body.get("episodes") else None
    episode_num = body.get("episodes", [{}])[0].get("episodeNumber") if body.get("episodes") else None
    if season_num:
        video_info["season"] = season_num
    if episode_num:
        video_info["episode"] = episode_num
    video_info["type"] = "episode"
    if series.get("title"):
        video_info["title"] = series["title"]
    video_info["imdb_id"] = series.get("imdbId") or ""
    video_info["tmdb_id"] = series.get("tmdbId")
    if series.get("year"):
        video_info["year"] = series["year"]
    scene_name = ep_file.get("sceneName", "")
    if scene_name:
        video_info["scene_name"] = scene_name

    task_id = f"dl_{int(time.time()*1000)}_{id(body)}"
    episodes = body.get("episodes", [{}])
    ep_id = episodes[0].get("id") if episodes else None
    task = {
        "id": task_id,
        "video_path": str(video_path),
        "display_path": file_path,
        "lang": app_config.get("auto_download_lang", "zho_chs"),
        "video_info": video_info,
        "season": season_num,
        "episode": episode_num,
        "episode_id": ep_id,
        "status": "queued",
        "created_at": datetime.now().isoformat(),
        "source": "sonarr_webhook",
    }
    download_items[task_id] = task
    await download_queue.put(task_id)

    return {"status": "queued", "task_id": task_id, "series": series.get("title", "")}



@app.get("/api/douban/status")
async def douban_status():
    """Get Douban monitoring status."""
    return {
        "enabled": app_config.get("douban_enabled", False),
        "user_id": app_config.get("douban_user_id", ""),
        "check_interval_hours": app_config.get("douban_check_interval_hours", 12),
        "last_check": app_config.get("douban_last_check", ""),
        "baseline": app_config.get("douban_baseline", {"movie_name": "", "add_date": ""}),
        "last_result": _douban_last_result,
    }


@app.post("/api/douban/check")
async def douban_check():
    """Manually trigger a Douban check."""
    if not app_config.get("douban_user_id"):
        return {"status": "error", "error": "请先配置豆瓣用户ID"}
    
    try:
        result = await asyncio.to_thread(
            douban_monitor.check_once, app_config
        )
        _douban_last_result.update(result)
        
        if result.get("added_movies"):
            for movie in result["added_movies"]:
                _douban_history.appendleft({
                    "timestamp": datetime.now().isoformat(),
                    "action": "added",
                    "movie_name": movie.get("name", ""),
                    "original_title": movie.get("original_title", ""),
                    "year": movie.get("year", ""),
                })
        
        if result.get("failed_movies"):
            for movie in result["failed_movies"]:
                _douban_history.appendleft({
                    "timestamp": datetime.now().isoformat(),
                    "action": "failed",
                    "movie_name": movie.get("name", ""),
                    "reason": movie.get("reason", ""),
                })
        
        _save_config()
        return result
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/api/douban/history")
async def douban_history():
    """Get Douban monitoring history."""
    return list(_douban_history)


async def _download_worker():
    while True:
        task_id = await download_queue.get()
        task = download_items.get(task_id)
        if not task:
            continue

        task["status"] = "downloading"
        task["started_at"] = datetime.now().isoformat()
        start_time = time.time()

        try:
            video_path = Path(task["video_path"])

            if not video_path.exists():
                task["status"] = "error"
                task["error"] = "Video file no longer exists"
                task["duration_ms"] = 0
                task["completed_at"] = datetime.now().isoformat()
                task["provider_results"] = [{"provider": "", "score": 0, "status": "error", "reason": "视频文件已不存在"}]
                log_entry = {
                    "timestamp": datetime.now().isoformat(),
                    "path": task.get("display_path", ""),
                    "status": "error",
                    "subtitle_path": "",
                    "error": "Video file no longer exists",
                    "duration_ms": 0,
                    "provider": "",
                }
                download_log.appendleft(log_entry)
                download_completed.appendleft(dict(task))
                _save_queue_data()
                download_items.pop(task_id, None)
                continue

            if _get_subtitle_status(video_path) == "zh" and not task.get("force"):
                task["status"] = "skipped"
                task["subtitle_path"] = ""
                duration_ms = 0
                task["duration_ms"] = duration_ms
                task["completed_at"] = datetime.now().isoformat()
                task["provider_results"] = [{"provider": "", "score": 0, "status": "skipped", "reason": "已有中文字幕"}]
                log_entry = {
                    "timestamp": datetime.now().isoformat(),
                    "path": task.get("display_path", ""),
                    "status": "skipped",
                    "subtitle_path": "",
                    "error": "Chinese subtitle already exists",
                    "duration_ms": duration_ms,
                    "provider": task.get("provider", ""),
                }
                download_log.appendleft(log_entry)
                download_completed.appendleft(dict(task))
                _save_queue_data()
                download_items.pop(task_id, None)
                continue

            lang = task.get("lang", "zho_chs")
            video_info = task.get("video_info")

            if not video_info:
                from subtitle_providers.utils import parse_filename
                video_info = parse_filename(video_path.name)

            existing_sub_status = _get_subtitle_status(video_path)
            has_en_sub = existing_sub_status in ("en", "zh", "zh+en")

            keyword = _build_search_keyword(video_info, video_path)
            provider_results = []

            enabled = app_config.get("enabled_providers", ["zimuku"])
            concurrent = app_config.get("concurrent_search", True)

            downloaded = None
            all_downloaded = []  # Track all successfully downloaded files
            zh_eligible_count = 0
            if concurrent:
                results = await _search_providers(
                    keyword, video_info,
                    video_info.get("season") if video_info else None,
                    video_info.get("episode") if video_info else None,
                    enabled,
                )
                video_type = video_info.get("type", "movie") if video_info else "movie"
                eligible = _select_eligible_results(results, lang, video_type)
                zh_eligible_count = len(eligible)
                output_dir = video_path.parent
                max_dl = app_config.get("max_download_count", 2)
                success_count = 0
                while eligible and success_count < max_dl:
                    r = eligible[0]
                    provider_name = r.provider or "zimuku"
                    r_lang = getattr(r, "language", "") or lang
                    is_en = r_lang in ("eng", "en")
                    score_pct = _score_to_pct(r.score, video_type)
                    if is_en and has_en_sub:
                        provider_results.append({"provider": provider_name, "score": score_pct, "language": r.language, "status": "skipped", "reason": "已有英文字幕"})
                        eligible = eligible[1:]
                        continue
                    provider_results.append({"provider": provider_name, "score": score_pct, "language": r.language, "status": "downloading"})
                    dl, original_name = await _download_from_provider(
                        provider_name, r, output_dir, r_lang, video_path.name
                    )
                    if dl and dl.suffix in SUBTITLE_EXTENSIONS:
                        downloaded = dl
                        all_downloaded.append(dl)
                        provider_results[-1]["status"] = "success"
                        provider_results[-1]["original_name"] = original_name
                        success_count += 1
                        eligible = eligible[1:]
                    else:
                        provider_results[-1]["status"] = "failed"
                        eligible = eligible[1:]
                        if eligible:
                            eligible = _select_eligible_results(
                                eligible, lang, video_type
                            )
                if all_downloaded:
                    _cleanup_archives(output_dir)
            else:
                default_engine = app_config.get("subtitle_provider", "zimuku")
                ordered = [default_engine] + [p for p in enabled if p != default_engine]
                max_dl = app_config.get("max_download_count", 2)
                success_count = 0
                for provider_name in ordered:
                    if success_count >= max_dl:
                        break
                    provider_results.append({"provider": provider_name, "score": 0, "language": "", "status": "searching"})
                    try:
                        results = await _search_providers(
                            keyword, video_info,
                            video_info.get("season") if video_info else None,
                            video_info.get("episode") if video_info else None,
                            [provider_name],
                        )
                    except Exception:
                        provider_results[-1]["status"] = "error"
                        continue

                    if not results:
                        provider_results[-1]["status"] = "no_results"
                        continue

                    video_type = video_info.get("type", "movie") if video_info else "movie"
                    eligible = _select_eligible_results(results, lang, video_type)
                    zh_eligible_count += len(eligible)
                    if not eligible:
                        best = max(results, key=lambda x: x.score) if results else None
                        provider_results[-1]["score"] = _score_to_pct(best.score, video_type) if best else 0
                        provider_results[-1]["language"] = best.language if best else ""
                        provider_results[-1]["status"] = "no_eligible"
                        continue

                    provider_results.pop()
                    output_dir = video_path.parent
                    for r in eligible:
                        if success_count >= max_dl:
                            break
                        p_name = r.provider or provider_name
                        r_lang = getattr(r, "language", "") or lang
                        is_en = r_lang in ("eng", "en")
                        score_pct = _score_to_pct(r.score, video_type)
                        if is_en and has_en_sub:
                            provider_results.append({"provider": p_name, "score": score_pct, "language": r.language, "status": "skipped", "reason": "已有英文字幕"})
                            continue
                        provider_results.append({"provider": p_name, "score": score_pct, "language": r.language, "status": "downloading"})
                        dl, original_name = await _download_from_provider(
                            p_name, r, output_dir, r_lang, video_path.name
                        )
                        if dl and dl.suffix in SUBTITLE_EXTENSIONS:
                            downloaded = dl
                            all_downloaded.append(dl)
                            provider_results[-1]["status"] = "success"
                            provider_results[-1]["original_name"] = original_name
                            success_count += 1
                        else:
                            provider_results[-1]["status"] = "failed"

                    if all_downloaded:
                        _cleanup_archives(output_dir)
                        break

            zh_best_pct = 0
            for pr in provider_results:
                if pr.get("language", "") not in ("eng", "en"):
                    zh_best_pct = max(zh_best_pct, pr.get("score", 0))

            zh_downloaded = any(
                pr.get("status") == "success" and pr.get("language", "") not in ("eng", "en")
                for pr in provider_results
            )

            existing_sub_status = _get_subtitle_status(video_path)
            has_en_sub = existing_sub_status in ("en", "zh", "zh+en")

            en_threshold = float(app_config.get("chinese_score_english_fallback", 72))
            scarce_count = int(app_config.get("chinese_scarce_count", 1))
            scarce_buffer = float(app_config.get("chinese_scarce_buffer", 15))

            need_en = (
                # A: 零中文结果
                zh_best_pct == 0
                # B: 中文质量不足 (existing logic)
                or (0 < zh_best_pct < en_threshold)
                # C: 中文稀缺 + 勉强过关
                or (zh_eligible_count <= scarce_count and zh_best_pct < en_threshold + scarce_buffer)
                # D: 中文下载全失败
                or (zh_best_pct > 0 and not zh_downloaded)
            )

            if need_en and not has_en_sub:
                en_results_raw = []
                if concurrent:
                    en_results_raw = await _search_providers(
                        keyword, video_info,
                        video_info.get("season") if video_info else None,
                        video_info.get("episode") if video_info else None,
                        enabled,
                    )
                else:
                    default_engine = app_config.get("subtitle_provider", "zimuku")
                    ordered = [default_engine] + [p for p in enabled if p != default_engine]
                    for en_pname in ordered:
                        try:
                            en_partial = await _search_providers(
                                keyword, video_info,
                                video_info.get("season") if video_info else None,
                                video_info.get("episode") if video_info else None,
                                [en_pname],
                            )
                            en_results_raw.extend(en_partial)
                            _EN_LANGS = {"eng", "en", "zho_chs+eng", "zho_cht+eng"}
                            en_max = max((r.score for r in en_partial if hasattr(r, "score") and r.score and getattr(r, "language", "") in _EN_LANGS), default=0)
                            if en_max > 0 and _score_to_pct(en_max, video_type) >= en_threshold:
                                break
                        except Exception:
                            continue

                en_eligible = _select_en_supplement(en_results_raw, video_type)
                en_max_dl = app_config.get("max_download_count", 2)
                en_success = 0
                for er in en_eligible:
                    if en_success >= en_max_dl:
                        break
                    ep_name = er.provider or "zimuku"
                    score_pct = _score_to_pct(er.score, video_type)
                    provider_results.append({"provider": ep_name, "score": score_pct, "language": "eng", "status": "downloading"})
                    dl_en, en_orig = await _download_from_provider(
                        ep_name, er, output_dir, "eng", video_path.name
                    )
                    if dl_en and dl_en.suffix in SUBTITLE_EXTENSIONS:
                        downloaded = dl_en
                        all_downloaded.append(dl_en)
                        provider_results[-1]["status"] = "success"
                        provider_results[-1]["original_name"] = en_orig
                        en_success += 1
                    else:
                        provider_results[-1]["status"] = "failed"
                if en_success > 0:
                    _cleanup_archives(output_dir)

            if not provider_results and not downloaded:
                provider_results.append({"provider": "", "score": 0, "language": "", "status": "no_results"})

            task["provider_results"] = provider_results

            if downloaded and downloaded.suffix in SUBTITLE_EXTENSIONS:
                _cleanup_archives(video_path.parent)
                new_status = "none"
                for dl_file in all_downloaded:
                    sub_lang = _detect_subtitle_lang(dl_file)
                    if sub_lang in ("zh", "zh+en"):
                        new_status = "zh"
                        break
                    elif sub_lang == "en" and new_status == "none":
                        new_status = "en"
                try:
                    display = str(downloaded.relative_to(VIDEO_ROOT.resolve()))
                except ValueError:
                    display = str(downloaded)
                task["status"] = "success"
                task["subtitle_path"] = display
                movie_id = task.get("movie_id")
                if movie_id:
                    _set_cached_status(str(movie_id), new_status, video_path=str(video_path))
                    for m in _arr_data_cache.get("movies", []):
                        if m.get("id") == movie_id:
                            m["subtitleStatus"] = new_status
                            break
                ep_id = task.get("episode_id")
                if ep_id:
                    _set_cached_status(f"ep_{ep_id}", new_status, video_path=str(video_path))
                    for s in _arr_data_cache.get("series", []):
                        for seas in s.get("seasons", []):
                            for ep in seas.get("episodes", []):
                                if ep.get("id") == ep_id:
                                    ep["subtitleStatus"] = new_status
                                    break
                _save_subtitle_cache()
            else:
                all_skipped = all(pr.get("status") == "skipped" for pr in provider_results)
                if all_skipped:
                    task["status"] = "skipped"
                    task["error"] = "已有英文字幕，未找到中文字幕"
                else:
                    task["status"] = "failed"
                    task["error"] = "Download failed or extraction unsuccessful"

        except Exception as e:
            task["status"] = "error"
            task["error"] = str(e)

        duration_ms = int((time.time() - start_time) * 1000)
        task["duration_ms"] = duration_ms
        task["completed_at"] = datetime.now().isoformat()

        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "path": task.get("display_path", ""),
            "status": task["status"],
            "subtitle_path": task.get("subtitle_path"),
            "error": task.get("error"),
            "duration_ms": duration_ms,
            "provider": task.get("provider", ""),
        }
        download_log.appendleft(log_entry)

        completed = dict(task)
        download_completed.appendleft(completed)
        _save_queue_data()
        download_items.pop(task_id, None)


async def _translate_worker():
    while True:
        task_id = await translate_queue.get()
        task = translate_items.get(task_id)
        if not task:
            continue

        task["status"] = "translating"
        task["started_at"] = datetime.now().isoformat()
        start_time = time.time()

        try:
            from subtitle_translator import translate_srt_file, _parse_glossary

            sub_path = Path(task["subtitle_path"])
            source_lang = task.get("source_lang", "English")
            target_lang = app_config.get("ai_target_lang", "Chinese")
            bilingual = app_config.get("ai_bilingual", True)

            media_context = None
            video_path = task.get("video_path", "") or str(sub_path)
            for movie in _arr_data_cache.get("movies", []):
                mp = movie.get("filePath", "") or movie.get("path", "")
                if mp and (video_path.startswith(mp) or str(sub_path).startswith(mp)):
                    media_context = {
                        "title": movie.get("title", ""),
                        "year": movie.get("year"),
                        "genres": movie.get("genres", []),
                        "overview": movie.get("overview", ""),
                    }
                    break
            if not media_context:
                for series in _arr_data_cache.get("series", []):
                    sp = series.get("path", "")
                    if sp and video_path.startswith(sp):
                        media_context = {
                            "title": series.get("title", ""),
                            "year": series.get("year"),
                            "genres": series.get("genres", []),
                            "overview": series.get("overview", ""),
                        }
                        break

            glossary_raw = app_config.get("ai_glossary", "")
            glossary = _parse_glossary(glossary_raw) if glossary_raw else None

            result_path = await asyncio.to_thread(
                translate_srt_file,
                str(sub_path),
                source_lang,
                target_lang,
                bilingual,
                app_config.get("ai_api_url", "https://api.openai.com/v1"),
                app_config.get("ai_api_key", ""),
                app_config.get("ai_model", "gpt-4o-mini"),
                app_config.get("ai_batch_size", 50),
                app_config.get("ai_max_retries", 3),
                app_config.get("ai_retry_delay", 2.0),
                app_config.get("ai_temperature", 0.3),
                app_config.get("ai_max_output_tokens", 4096),
                app_config.get("ai_system_prompt", ""),
                app_config.get("ai_bilingual_prompt", ""),
                media_context,
                glossary,
                context_lines=app_config.get("ai_context_lines", 2),
                output_mode=app_config.get("ai_output_mode", "text"),
                streaming=app_config.get("ai_streaming", True),
                concurrency=app_config.get("ai_concurrency", 2),
                ai_review_enabled=app_config.get("ai_review_enabled", True),
                ai_review_model=app_config.get("ai_review_model", ""),
                ai_review_timeout=app_config.get("ai_review_timeout", 300),
                ai_review_prompt=app_config.get("ai_review_prompt", ""),
            )

            task["status"] = "success"
            task["output_path"] = str(result_path)

        except Exception as e:
            task["status"] = "error"
            task["error"] = str(e)

        duration_ms = int((time.time() - start_time) * 1000)
        task["duration_ms"] = duration_ms
        task["completed_at"] = datetime.now().isoformat()

        completed = dict(task)
        translate_completed.appendleft(completed)
        _save_queue_data()
        translate_items.pop(task_id, None)


async def _douban_monitor_loop():
    while True:
        try:
            interval_hours = app_config.get("douban_check_interval_hours", 12)
            if not app_config.get("douban_enabled", False):
                await asyncio.sleep(60)
                continue

            logger.info("Douban monitor: starting check...")
            result = await asyncio.to_thread(
                douban_monitor.check_once, app_config
            )

            _douban_last_result.update(result)

            if result.get("added_movies"):
                for movie in result["added_movies"]:
                    _douban_history.appendleft({
                        "timestamp": datetime.now().isoformat(),
                        "action": "added",
                        "movie_name": movie.get("name", ""),
                        "original_title": movie.get("original_title", ""),
                        "year": movie.get("year", ""),
                    })

            if result.get("failed_movies"):
                for movie in result["failed_movies"]:
                    _douban_history.appendleft({
                        "timestamp": datetime.now().isoformat(),
                        "action": "failed",
                        "movie_name": movie.get("name", ""),
                        "reason": movie.get("reason", ""),
                    })

            _save_config()

            logger.info(f"Douban monitor: check complete. Added: {result.get('new_movies_count', 0)}")

        except Exception as e:
            logger.error(f"Douban monitor loop error: {e}")

        interval_hours = app_config.get("douban_check_interval_hours", 12)
        await asyncio.sleep(interval_hours * 3600)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Subtitle Downloader Web UI")
    parser.add_argument("--video-root", default=None, help="Root directory for video files")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None, help="Bind port (default: 19030)")
    args = parser.parse_args()

    if args.video_root:
        VIDEO_ROOT = Path(args.video_root)
        app_config["video_root"] = str(VIDEO_ROOT)
    if args.port:
        PORT = args.port

    print(f"VIDEO_ROOT={VIDEO_ROOT}  PORT={PORT}")
    uvicorn.run(app, host=args.host, port=PORT)
