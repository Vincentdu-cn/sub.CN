#!/usr/bin/env python3
"""Standalone subtitle diagnostic tool — search→score→rank→download chain.

Captures the full subtitle pipeline for 6 test movies across all 4 engines.
Reads config.json directly — NEVER imports from app.py.
"""

import json
import sys
import os
import time
import argparse
import tempfile
import shutil
import re
import threading
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

from subtitle_providers.utils import parse_filename, compute_match_score
from subtitle_providers import get_provider, SubtitleResult

# ── Constants ────────────────────────────────────────────────────────────────

SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa"}

DEFAULT_CONFIG = {
    "video_root": "/video",
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
    "queue_max_size": 100,
}

TEST_MOVIES = [
    {"filename": "Greedy.People.2024.2160p.AMZN.WEB-DL.DDP5.1.x265.10bit-HiDt.mkv", "title": "Greedy People", "year": 2024},
    {"filename": "The.Motive.2017.SPANISH.1080p.BluRay.H264.AAC-VXT.mkv", "title": "The Motive", "year": 2017},
    {"filename": "One.Battle.After.Another.2025.1080p.WEBRip.x265.10bit.AAC5.1-[YTS.MX].mkv", "title": "One Battle After Another", "year": 2025},
    {"filename": "The.Birdcage.1996.1080p.AMZN.WEB-DL.DDP2.0.H.264-FLUX.mkv", "title": "The Birdcage", "year": 1996},
]


# ── Config loading ───────────────────────────────────────────────────────────

def load_test_config(config_path: str) -> dict:
    """Load config.json — same logic as app.py:_load_config but returns dict."""
    config_file = Path(config_path)
    if config_file.exists():
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                saved = json.load(f)
            merged = dict(DEFAULT_CONFIG)
            merged.update(saved)
            return merged
        except Exception:
            return dict(DEFAULT_CONFIG)
    return dict(DEFAULT_CONFIG)


# ── Provider kwargs (copied from app.py:447-469) ────────────────────────────

def _get_provider_kwargs(provider_name: str) -> dict:
    kwargs = {}
    if provider_name == "zimuku":
        if test_config.get("baidu_ocr_api_key"):
            kwargs["ocr_api_key"] = test_config["baidu_ocr_api_key"]
        if test_config.get("baidu_ocr_secret_key"):
            kwargs["ocr_secret_key"] = test_config["baidu_ocr_secret_key"]
    elif provider_name == "subhd":
        if test_config.get("baidu_ocr_api_key"):
            kwargs["ocr_api_key"] = test_config["baidu_ocr_api_key"]
        if test_config.get("baidu_ocr_secret_key"):
            kwargs["ocr_secret_key"] = test_config["baidu_ocr_secret_key"]
    elif provider_name == "assrt":
        if test_config.get("assrt_api_token"):
            kwargs["api_token"] = test_config["assrt_api_token"]
    elif provider_name == "opensubtitles":
        if test_config.get("opensubtitles_api_key"):
            kwargs["api_key"] = test_config["opensubtitles_api_key"]
        if test_config.get("opensubtitles_username"):
            kwargs["username"] = test_config["opensubtitles_username"]
        if test_config.get("opensubtitles_password"):
            kwargs["password"] = test_config["opensubtitles_password"]
    return kwargs


# ── Build search keyword (copied from app.py:472-485) ───────────────────────

def _build_search_keyword(video_info: dict, video_path: Path) -> str:
    if video_info:
        imdb_id = video_info.get("imdb_id", "")
        if imdb_id:
            return imdb_id
        if video_info.get("scene_name"):
            return video_info["scene_name"]
        title = video_info.get("title", "")
        year = video_info.get("year")
        if title and year:
            return f"{title} {year}"
        if title:
            return title
    return video_path.stem


# ── Score to percentage (copied from app.py:568-570) ─────────────────────────

def _score_to_pct(score: float, video_type: str = "movie") -> float:
    from subtitle_providers.utils import MOVIE_MAX_SCORE, EPISODE_MAX_SCORE
    max_possible = EPISODE_MAX_SCORE if video_type == "episode" else MOVIE_MAX_SCORE
    return round(score / max_possible * 100, 2) if max_possible > 0 and score > 0 else 0.0


# ── Select eligible results (copied from app.py:488-537) ─────────────────────

def _select_eligible_results(results: list, lang: str = "zho_chs",
                             video_type: str = "movie") -> list:
    if not results:
        return []

    max_count = test_config.get("max_download_count", 2)
    score_threshold_pct = test_config.get("score_threshold_pct", 10)

    _ZH_SIM = {"zho_chs", "zho", "chi", "chs", "zho_chs+eng"}
    _ZH_TRA = {"zho_cht", "cht", "zho_cht+eng"}
    _ZH_ALL = _ZH_SIM | _ZH_TRA
    _EN = {"eng", "en", "zho_chs+eng", "zho_cht+eng"}

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


# ── Select English supplement (copied from app.py:540-565) ──────────────────

def _select_en_supplement(results: list, video_type: str = "movie") -> list:
    if not results:
        return []

    max_count = test_config.get("max_download_count", 2)
    score_threshold_pct = test_config.get("score_threshold_pct", 10)

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
    threshold = top_score * (score_threshold_pct / 100.0)
    eligible = [r for r in en_results if hasattr(r, "score") and r.score >= threshold]
    if len(eligible) >= 2:
        diff_pct = ((eligible[0].score - eligible[1].score) / eligible[0].score * 100) if eligible[0].score > 0 else 100
        if diff_pct > score_threshold_pct:
            eligible = eligible[:1]
    return eligible[:max_count]


# ── Cleanup archives (copied from app.py:658-665) ───────────────────────────

def _cleanup_archives(output_dir: Path):
    for archive_ext in [".zip", ".rar"]:
        for f in output_dir.glob(f"*{archive_ext}"):
            if f.is_file():
                try:
                    f.unlink()
                except OSError:
                    pass


# ── Search chain ─────────────────────────────────────────────────────────────

def run_search_chain(movie: dict, providers: list) -> dict:
    """Parse filename → build keyword → search all providers → score → sort."""
    filename = movie["filename"]
    video_path = Path(filename)

    # 1. Parse filename
    video_info = parse_filename(filename)
    video_type = video_info.get("type", "movie") if video_info else "movie"

    # 2. Build search keyword
    keyword = _build_search_keyword(video_info, video_path)

    # 3. Search each provider sequentially
    fallback_order = test_config.get("provider_fallback_order", [])
    enabled = providers or test_config.get("enabled_providers", ["zimuku"])
    if not enabled:
        enabled = ["zimuku"]

    # Order providers: fallback_order first, then any extras
    ordered = []
    for p in fallback_order:
        if p in enabled:
            ordered.append(p)
    for p in enabled:
        if p not in ordered:
            ordered.append(p)

    provider_search_data = []
    all_results = []

    for pname in ordered:
        t0 = time.time()
        try:
            kwargs = _get_provider_kwargs(pname)
            provider = get_provider(pname, **kwargs)
            results = provider.search(keyword, video_info,
                                      video_info.get("season") if video_info else None,
                                      video_info.get("episode") if video_info else None)
            duration_ms = round((time.time() - t0) * 1000, 1)

            raw_entries = []
            for r in results:
                raw_entries.append({
                    "title": r.title,
                    "language": r.language,
                    "page_url": r.page_url,
                    "raw_score": r.score,
                    "download_url": r.download_url,
                    "provider": r.provider,
                })
            provider_search_data.append({
                "provider": pname,
                "keyword": keyword,
                "duration_ms": duration_ms,
                "status": "ok",
                "result_count": len(results),
                "raw_results": raw_entries,
            })
            all_results.extend(results)
        except Exception as exc:
            duration_ms = round((time.time() - t0) * 1000, 1)
            provider_search_data.append({
                "provider": pname,
                "keyword": keyword,
                "duration_ms": duration_ms,
                "status": "error",
                "error": str(exc),
                "result_count": 0,
                "raw_results": [],
            })

    # 4. Score results (follow app.py:622-641)
    if video_info:
        search_tokens = set(keyword.lower().replace(".", " ").replace("-", " ").split())
        for r in all_results:
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

    # 5. Sort by score descending
    all_results.sort(key=lambda r: r.score if hasattr(r, "score") and r.score else 0, reverse=True)

    return {
        "filename": filename,
        "video_info": video_info,
        "video_type": video_type,
        "keyword": keyword,
        "provider_search_data": provider_search_data,
        "all_results": all_results,
        "total_results": len(all_results),
    }


# ── Download chain ───────────────────────────────────────────────────────────

def run_download_chain(movie: dict, search_data: dict, output_dir: Path) -> dict:
    """Select eligible results → attempt downloads → record outcomes."""
    lang = test_config.get("auto_download_lang", "zho_chs")
    video_type = search_data.get("video_type", "movie")
    all_results = search_data.get("all_results", [])
    filename = movie["filename"]

    # Select eligible Chinese results
    eligible = _select_eligible_results(all_results, lang, video_type)

    max_dl = test_config.get("max_download_count", 2)
    success_count = 0
    download_records = []

    for rank, r in enumerate(eligible, 1):
        if success_count >= max_dl:
            break

        provider_name = r.provider or "zimuku"
        is_en = getattr(r, "language", "") in ("eng", "en")
        r_lang = "eng" if is_en else lang
        score_pct = _score_to_pct(r.score, video_type)

        record = {
            "rank": rank,
            "provider": provider_name,
            "title": r.title,
            "score": r.score,
            "score_pct": score_pct,
            "language": r.language,
            "status": "downloading",
            "duration_ms": 0,
            "original_name": "",
            "failure_reason": "",
        }

        t0 = time.time()
        try:
            kwargs = _get_provider_kwargs(provider_name)
            provider = get_provider(provider_name, **kwargs)
            downloaded = provider.download(r, output_dir, r_lang, filename)
            duration_ms = round((time.time() - t0) * 1000, 1)
            record["duration_ms"] = duration_ms

            original_name = r.extra.get("_original_name", "") if hasattr(r, "extra") else ""
            record["original_name"] = original_name

            if downloaded and downloaded.suffix in SUBTITLE_EXTENSIONS:
                record["status"] = "success"
                success_count += 1
            else:
                record["status"] = "failed"
                record["failure_reason"] = "download returned no subtitle file"
                if downloaded:
                    record["failure_reason"] = f"download returned {downloaded.suffix} (not a subtitle)"
        except Exception as exc:
            duration_ms = round((time.time() - t0) * 1000, 1)
            record["duration_ms"] = duration_ms
            record["status"] = "failed"
            record["failure_reason"] = str(exc)

        download_records.append(record)

    # Calculate zh_best_pct (app.py:1646-1649)
    zh_best_pct = 0
    for dr in download_records:
        if dr.get("language", "") not in ("eng", "en"):
            zh_best_pct = max(zh_best_pct, dr.get("score_pct", 0))

    # Also check eligible that weren't downloaded (for zh_best_pct from selection)
    if not download_records and eligible:
        for r in eligible:
            if getattr(r, "language", "") not in ("eng", "en"):
                zh_best_pct = max(zh_best_pct, _score_to_pct(r.score, video_type))

    if success_count > 0:
        _cleanup_archives(output_dir)

    return {
        "lang": lang,
        "eligible_count": len(eligible),
        "download_records": download_records,
        "success_count": success_count,
        "zh_best_pct": zh_best_pct,
    }


# ── English supplement chain ────────────────────────────────────────────────

def run_english_supplement_chain(movie: dict, search_data: dict,
                                  zh_best_pct: float, zh_eligible_count: int,
                                  zh_downloaded: bool, providers: list,
                                  output_dir: Path) -> dict:
    """Check activation → search EN → select → download EN → record reasons."""
    video_type = search_data.get("video_type", "movie")
    video_info = search_data.get("video_info")
    keyword = search_data.get("keyword", "")
    filename = movie["filename"]

    en_threshold = float(test_config.get("chinese_score_english_fallback", 72))
    scarce_count = int(test_config.get("chinese_scarce_count", 1))
    scarce_buffer = float(test_config.get("chinese_scarce_buffer", 15))
    has_en_sub = False

    need_en = (
        zh_best_pct == 0
        or (0 < zh_best_pct < en_threshold)
        or (zh_eligible_count <= scarce_count and zh_best_pct < en_threshold + scarce_buffer)
        or (zh_best_pct > 0 and not zh_downloaded)
    )
    activated = need_en and not has_en_sub

    if not activated:
        if zh_best_pct == 0:
            reason = "no Chinese results (score=0%) — should have triggered A"
        elif zh_best_pct >= en_threshold + scarce_buffer or zh_eligible_count > scarce_count:
            reason = f"score sufficient ({zh_best_pct}%, eligible={zh_eligible_count})"
        else:
            reason = f"not activated (zh_best_pct={zh_best_pct}%, eligible={zh_eligible_count})"
        return {
            "activated": False,
            "reason": reason,
            "zh_best_pct": zh_best_pct,
            "zh_eligible_count": zh_eligible_count,
            "threshold": en_threshold,
            "en_search_results": [],
            "en_eligible": [],
            "en_downloads": [],
        }

    # Activated — search for English results
    fallback_order = test_config.get("provider_fallback_order", [])
    enabled = providers or test_config.get("enabled_providers", ["zimuku"])
    if not enabled:
        enabled = ["zimuku"]

    # Order providers
    ordered = []
    for p in fallback_order:
        if p in enabled:
            ordered.append(p)
    for p in enabled:
        if p not in ordered:
            ordered.append(p)

    en_results_raw = []
    en_search_data = []

    for en_pname in ordered:
        t0 = time.time()
        try:
            kwargs = _get_provider_kwargs(en_pname)
            provider = get_provider(en_pname, **kwargs)
            en_partial = provider.search(
                keyword, video_info,
                video_info.get("season") if video_info else None,
                video_info.get("episode") if video_info else None,
            )
            duration_ms = round((time.time() - t0) * 1000, 1)

            # Score EN results
            if video_info:
                search_tokens = set(keyword.lower().replace(".", " ").replace("-", " ").split())
                for r in en_partial:
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

            en_results_raw.extend(en_partial)
            en_search_data.append({
                "provider": en_pname,
                "duration_ms": duration_ms,
                "status": "ok",
                "result_count": len(en_partial),
            })

            # Early stop if good EN score found (app.py:1673-1675)
            _EN = {"eng", "en", "zho_chs+eng", "zho_cht+eng"}
            en_max = max((r.score for r in en_partial if hasattr(r, "score") and r.score and getattr(r, "language", "") in _EN), default=0)
            if en_max > 0 and _score_to_pct(en_max, video_type) >= en_threshold:
                break
        except Exception as exc:
            duration_ms = round((time.time() - t0) * 1000, 1)
            en_search_data.append({
                "provider": en_pname,
                "duration_ms": duration_ms,
                "status": "error",
                "error": str(exc),
                "result_count": 0,
            })

    # Select EN eligible
    en_eligible = _select_en_supplement(en_results_raw, video_type)

    # Download EN results
    en_max_dl = test_config.get("max_download_count", 2)
    en_success = 0
    en_download_records = []

    for er in en_eligible:
        if en_success >= en_max_dl:
            break

        ep_name = er.provider or "zimuku"
        score_pct = _score_to_pct(er.score, video_type)

        record = {
            "provider": ep_name,
            "title": er.title,
            "score": er.score,
            "score_pct": score_pct,
            "language": "eng",
            "status": "downloading",
            "duration_ms": 0,
            "original_name": "",
            "failure_reason": "",
        }

        t0 = time.time()
        try:
            kwargs = _get_provider_kwargs(ep_name)
            provider = get_provider(ep_name, **kwargs)
            dl_en = provider.download(er, output_dir, "eng", filename)
            duration_ms = round((time.time() - t0) * 1000, 1)
            record["duration_ms"] = duration_ms

            en_orig = er.extra.get("_original_name", "") if hasattr(er, "extra") else ""
            record["original_name"] = en_orig

            if dl_en and dl_en.suffix in SUBTITLE_EXTENSIONS:
                record["status"] = "success"
                en_success += 1
            else:
                record["status"] = "failed"
                record["failure_reason"] = "download returned no subtitle file"
                if dl_en:
                    record["failure_reason"] = f"download returned {dl_en.suffix} (not a subtitle)"
        except Exception as exc:
            duration_ms = round((time.time() - t0) * 1000, 1)
            record["duration_ms"] = duration_ms
            record["status"] = "failed"
            record["failure_reason"] = str(exc)

        en_download_records.append(record)

    if en_success > 0:
        _cleanup_archives(output_dir)

    # Serialize eligible for JSON output
    en_eligible_data = []
    for er in en_eligible:
        en_eligible_data.append({
            "title": er.title,
            "language": er.language,
            "score": er.score,
            "score_pct": _score_to_pct(er.score, video_type),
            "provider": er.provider,
        })

    return {
        "activated": True,
        "reason": f"zh_best_pct={zh_best_pct}%, eligible={zh_eligible_count} — triggered by " + (
            "A(零中文)" if zh_best_pct == 0 else
            "B(低分)" if 0 < zh_best_pct < en_threshold else
            "C(稀缺+勉强)" if zh_eligible_count <= scarce_count else
            "D(下载全挂)"
        ),
        "zh_best_pct": zh_best_pct,
        "threshold": en_threshold,
        "en_search_data": en_search_data,
        "en_search_results": en_eligible_data,
        "en_eligible": en_eligible_data,
        "en_downloads": en_download_records,
    }


def generate_html_report(results_store: list, running: bool = False) -> str:
    from datetime import datetime
    import json as _json

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    movie_count = len(results_store)
    total_movies = len(TEST_MOVIES)
    mode_label = "仅搜索" if (results_store and results_store[0].get("dry_run")) else "完整"

    def _fmt_ms(ms):
        if ms is None:
            return "—"
        return f"{ms / 1000:.1f}s"

    def _truncate(s, length=80):
        if not s:
            return ""
        return s

    def _status_badge(status):
        status = (status or "").lower()
        if status in ("success", "ok"):
            return '<span class="badge badge-success">成功</span>'
        elif status in ("failed", "error"):
            return '<span class="badge badge-error">失败</span>'
        elif status in ("skipped",):
            return '<span class="badge badge-gray">跳过</span>'
        else:
            return f'<span class="badge badge-gray">{status or "未知"}</span>'

    def _score_color(pct):
        if pct is None:
            return ""
        if pct >= 72:
            return ' style="color:#28a745;font-weight:bold;"'
        elif pct >= 50:
            return ' style="color:#ffc107;font-weight:bold;"'
        else:
            return ' style="color:#dc3545;font-weight:bold;"'

    def _render_movie(movie, idx):
        title = movie.get("title", "Unknown")
        year = movie.get("year", "")
        filename = movie.get("filename", "")
        search = movie.get("search", {}) or {}
        download = movie.get("download")
        en_supplement = movie.get("en_supplement")
        dry_run = movie.get("dry_run", False)
        duration_ms = movie.get("duration_ms", 0)

        keyword = search.get("keyword", "")
        video_type = search.get("video_type", "movie")
        total_results = search.get("total_results", 0)
        provider_search_data = search.get("provider_search_data", [])
        all_results = search.get("all_results", [])

        provider_sections = []
        for psd in provider_search_data:
            provider = psd.get("provider", "")
            result_count = psd.get("result_count", 0)
            duration = psd.get("duration_ms", 0)
            status = psd.get("status", "")
            error = psd.get("error", "")

            status_html = ""
            if status == "error":
                status_html = f'<span class="badge badge-error">错误</span> <span class="error-text">{_truncate(error, 100)}</span>'
            else:
                status_html = _status_badge(status)

            provider_results = [r for r in all_results if r.get("provider") == provider]
            result_rows = []
            for i, r in enumerate(provider_results, 1):
                score_pct = r.get("score_pct", 0)
                color_attr = _score_color(score_pct)
                result_rows.append(
                    f"<tr>"
                    f'<td>{i}</td>'
                    f'<td>{_truncate(r.get("title", ""), 80)}</td>'
                    f'<td>{r.get("language", "")}</td>'
                    f'<td{color_attr}>{r.get("score", 0)}</td>'
                    f'<td{color_attr}>{score_pct}%</td>'
                    f"</tr>"
                )

            if not result_rows:
                result_rows.append('<tr><td colspan="5" style="text-align:center;color:#6c757d;">0 条结果</td></tr>')

            provider_sections.append(
                f'<details class="provider-details">\n'
                f'<summary>引擎: {provider} ({result_count}条结果, 耗时{_fmt_ms(duration)}) {status_html}</summary>\n'
                f'<table>\n'
                f'<tr><th>#</th><th>标题</th><th>语言</th><th>匹配分</th><th>匹配分%</th></tr>\n'
                f"{''.join(result_rows)}\n"
                f'</table>\n'
                f'</details>\n'
            )

        scoring_rows = []
        sorted_results = sorted(all_results, key=lambda x: x.get("score", 0), reverse=True)
        eligible_keys = set()
        if download and download.get("download_records"):
            for dr in download["download_records"]:
                eligible_keys.add((dr.get("title", ""), dr.get("provider", "")))

        for rank, r in enumerate(sorted_results, 1):
            score_pct = r.get("score_pct", 0)
            color_attr = _score_color(score_pct)
            key = (r.get("title", ""), r.get("provider", ""))
            is_eligible = key in eligible_keys

            if is_eligible:
                eligible_html = '<span class="badge badge-success">是</span>'
                reason = "符合下载条件"
            else:
                eligible_html = '<span class="badge badge-gray">否</span>'
                if score_pct < 72:
                    reason = "分数低于阈值"
                else:
                    reason = "未进入候选"

            scoring_rows.append(
                f"<tr>"
                f'<td>{rank}</td>'
                f'<td>{r.get("provider", "")}</td>'
                f'<td>{_truncate(r.get("title", ""), 80)}</td>'
                f'<td>{r.get("language", "")}</td>'
                f'<td{color_attr}>{score_pct}%</td>'
                f'<td>{eligible_html}</td>'
                f'<td>{reason}</td>'
                f"</tr>"
            )

        download_section = ""
        if dry_run or download is None:
            download_section = '<div class="dry-run-msg">Dry Run — 未执行下载</div>'
        else:
            download_records = download.get("download_records", [])
            dl_rows = []
            for dr in download_records:
                status = dr.get("status", "")
                status_html = _status_badge(status)
                failure_reason = dr.get("failure_reason", "")
                failure_cell = f'<span class="error-text">{_truncate(failure_reason, 100)}</span>' if failure_reason else "—"
                dl_rows.append(
                    f"<tr>"
                    f'<td>{dr.get("rank", "")}</td>'
                    f'<td>{dr.get("provider", "")}</td>'
                    f'<td>{dr.get("score_pct", 0)}%</td>'
                    f'<td>{status_html}</td>'
                    f'<td class="mono">{_truncate(dr.get("original_name", ""), 80)}</td>'
                    f'<td>{_fmt_ms(dr.get("duration_ms"))}</td>'
                    f'<td>{failure_cell}</td>'
                    f"</tr>"
                )
            if not dl_rows:
                dl_rows.append('<tr><td colspan="7" style="text-align:center;color:#6c757d;">无下载记录</td></tr>')

            download_section = (
                f'<table>\n'
                f'<tr><th>排名</th><th>引擎</th><th>分数%</th><th>状态</th><th>原始文件名</th><th>耗时</th><th>失败原因</th></tr>\n'
                f"{''.join(dl_rows)}\n"
                f'</table>\n'
            )

        en_section = ""
        if dry_run or en_supplement is None:
            en_section = '<div class="dry-run-msg">Dry Run — 未执行英文补充</div>'
        else:
            activated = en_supplement.get("activated", False)
            reason = en_supplement.get("reason", "")
            zh_best_pct = en_supplement.get("zh_best_pct", 0)
            zh_eligible_count = en_supplement.get("zh_eligible_count", 0)
            threshold = en_supplement.get("threshold", 72)
            scarce_count = int(test_config.get("chinese_scarce_count", 1))
            scarce_buffer = float(test_config.get("chinese_scarce_buffer", 15))

            cond_a = "✓" if zh_best_pct == 0 else "✗"
            cond_b = "✓" if 0 < zh_best_pct < threshold else "✗"
            cond_c = "✓" if (zh_eligible_count <= scarce_count and zh_best_pct < threshold + scarce_buffer) else "✗"
            cond_d = "✓" if (zh_best_pct > 0 and not any(dr.get("status") == "success" and dr.get("language", "") not in ("eng", "en") for dr in movie.get("download", {}).get("download_records", []))) else "✗"

            if activated:
                status_tag = '<span class="badge badge-blue">英文补充: 已激活</span>'
            else:
                status_tag = '<span class="badge badge-gray">英文补充: 未激活</span>'

            en_search_data = en_supplement.get("en_search_data", [])
            en_search_results = en_supplement.get("en_search_results", [])
            en_downloads = en_supplement.get("en_downloads", [])

            en_search_rows = []
            for i, er in enumerate(en_search_results, 1):
                score_pct = er.get("score_pct", 0)
                color_attr = _score_color(score_pct)
                en_search_rows.append(
                    f"<tr>"
                    f'<td>{i}</td>'
                    f'<td>{er.get("provider", "")}</td>'
                    f'<td>{_truncate(er.get("title", ""), 80)}</td>'
                    f'<td>{er.get("language", "")}</td>'
                    f'<td{color_attr}>{score_pct}%</td>'
                    f"</tr>"
                )

            en_search_table = ""
            if en_search_rows:
                en_search_table = (
                    f'<h4>英文搜索结果</h4>\n'
                    f'<table>\n'
                    f'<tr><th>#</th><th>引擎</th><th>标题</th><th>语言</th><th>分数%</th></tr>\n'
                    f"{''.join(en_search_rows)}\n"
                    f'</table>\n'
                )

            en_dl_rows = []
            for edr in en_downloads:
                status = edr.get("status", "")
                status_html = _status_badge(status)
                failure_reason = edr.get("failure_reason", "")
                failure_cell = f'<span class="error-text">{_truncate(failure_reason, 100)}</span>' if failure_reason else "—"
                en_dl_rows.append(
                    f"<tr>"
                    f'<td>{edr.get("provider", "")}</td>'
                    f'<td>{_truncate(edr.get("title", ""), 80)}</td>'
                    f'<td>{edr.get("score_pct", 0)}%</td>'
                    f'<td>{edr.get("language", "")}</td>'
                    f'<td>{status_html}</td>'
                    f'<td>{_fmt_ms(edr.get("duration_ms"))}</td>'
                    f'<td>{failure_cell}</td>'
                    f"</tr>"
                )

            en_dl_table = ""
            if en_dl_rows:
                en_dl_table = (
                    f'<h4>英文下载结果</h4>\n'
                    f'<table>\n'
                    f'<tr><th>引擎</th><th>标题</th><th>分数%</th><th>语言</th><th>状态</th><th>耗时</th><th>失败原因</th></tr>\n'
                    f"{''.join(en_dl_rows)}\n"
                    f'</table>\n'
                )

            en_section = (
                f'<div class="status-tag">{status_tag}</div>\n'
                f'<div class="en-info"><strong>原因:</strong> {_truncate(reason, 200)}</div>\n'
                f'<div class="en-info"><strong>中文最高分:</strong> {zh_best_pct}% | <strong>eligible:</strong> {zh_eligible_count} | <strong>阈值:</strong> {threshold}%</div>\n'
                f'<div class="en-info" style="font-size:0.85em;color:#888">'
                f'触发条件: A(零中文)={cond_a} B(低分)={cond_b} C(稀缺+勉强)={cond_c} D(下载全挂)={cond_d} '
                f'[scarce_count={scarce_count}, buffer=+{scarce_buffer}%]</div>\n'
                f'{en_search_table}\n'
                f'{en_dl_table}\n'
            )

        return (
            f'<div class="movie" id="movie-{idx}">\n'
            f'<h2>{title} ({year})</h2>\n'
            f'<div class="movie-meta">总耗时: {_fmt_ms(duration_ms)} | 模式: {"仅搜索" if dry_run else "完整"}</div>\n'
            f'\n'
            f'<details open>\n'
            f'<summary>📋 输入信息</summary>\n'
            f'<table>\n'
            f'<tr><th>字段</th><th>值</th></tr>\n'
            f'<tr><td>文件名</td><td class="mono">{_truncate(filename, 120)}</td></tr>\n'
            f'<tr><td>视频类型</td><td>{video_type}</td></tr>\n'
            f'<tr><td>搜索关键词</td><td>{_truncate(keyword, 120)}</td></tr>\n'
            f'<tr><td>总结果数</td><td>{total_results}</td></tr>\n'
            f'</table>\n'
            f'</details>\n'
            f'\n'
            f'<details>\n'
            f'<summary>🔍 搜索结果 (引擎数: {len(provider_search_data)})</summary>\n'
            f"{''.join(provider_sections)}\n"
            f'</details>\n'
            f'\n'
            f'<details>\n'
            f'<summary>📊 评分排名</summary>\n'
            f'<table>\n'
            f'<tr><th>排名</th><th>引擎</th><th>标题</th><th>语言</th><th>分数%</th><th>是否Eligible</th><th>原因</th></tr>\n'
            f"{''.join(scoring_rows)}\n"
            f'</table>\n'
            f'</details>\n'
            f'\n'
            f'<details>\n'
            f'<summary>💾 下载结果</summary>\n'
            f'{download_section}\n'
            f'</details>\n'
            f'\n'
            f'<details>\n'
            f'<summary>🌐 英文补充</summary>\n'
            f'{en_section}\n'
            f'</details>\n'
            f'</div>\n'
        )

    movie_sections = []
    for i, movie in enumerate(results_store, 1):
        movie_sections.append(_render_movie(movie, i))

    if not movie_sections:
        movie_sections.append('<div class="no-data">暂无数据</div>')

    json_data = _json.dumps(results_store, ensure_ascii=False, indent=2, default=str)

    auto_refresh = '<meta http-equiv="refresh" content="3">' if running else ""
    status_badge = '<span style="background:#ffc107;color:#000;padding:2px 8px;border-radius:4px;font-weight:bold;">⏳ 搜索中...</span>' if running else '<span style="background:#28a745;color:#fff;padding:2px 8px;border-radius:4px;font-weight:bold;">✅ 已完成</span>'

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
 <head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
{auto_refresh}
<title>字幕下载诊断报告</title>
<style>
/* ── Base ── */
* {{ box-sizing: border-box; }}
body {{
    margin: 0;
    padding: 20px;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    background: #1a1a2e;
    color: #e0e0e0;
    line-height: 1.6;
}}

/* ── Header ── */
header {{
    text-align: center;
    padding: 20px 0 30px;
    border-bottom: 2px solid #2d2d44;
    margin-bottom: 30px;
}}
h1 {{
    margin: 0 0 10px;
    font-size: 2rem;
    color: #f8f9fa;
    letter-spacing: 1px;
}}
.header-meta {{
    color: #a0a0b0;
    font-size: 0.95rem;
}}

/* ── Movie card ── */
.movie {{
    background: #16213e;
    border: 1px solid #2d2d44;
    border-radius: 8px;
    padding: 20px;
    margin-bottom: 25px;
}}
.movie h2 {{
    margin: 0 0 8px;
    font-size: 1.4rem;
    color: #f8f9fa;
    border-bottom: 1px solid #2d2d44;
    padding-bottom: 10px;
}}
.movie-meta {{
    color: #a0a0b0;
    font-size: 0.85rem;
    margin-bottom: 15px;
}}

/* ── Details / Summary ── */
details {{
    margin: 10px 0;
    border: 1px solid #2d2d44;
    border-radius: 6px;
    background: #0f3460;
    overflow: hidden;
}}
summary {{
    padding: 12px 16px;
    cursor: pointer;
    font-weight: 600;
    font-size: 1rem;
    color: #f8f9fa;
    background: #1a1a2e;
    border-bottom: 1px solid #2d2d44;
    user-select: none;
    list-style: none;
}}
summary::-webkit-details-marker {{ display: none; }}
summary::before {{
    content: "▶ ";
    font-size: 0.8em;
    color: #007bff;
    display: inline-block;
    transition: transform 0.2s;
}}
details[open] > summary::before {{
    transform: rotate(90deg);
}}
details > *:not(summary) {{
    padding: 12px 16px;
}}
.provider-details {{
    margin: 8px 0 8px 12px;
    background: #16213e;
}}
.provider-details summary {{
    font-size: 0.9rem;
    font-weight: 500;
    padding: 8px 12px;
    background: #0f3460;
}}

/* ── Tables ── */
table {{
    width: 100%;
    border-collapse: collapse;
    margin: 8px 0;
    font-size: 0.9rem;
}}
th, td {{
    padding: 8px 10px;
    text-align: left;
    border-bottom: 1px solid #2d2d44;
    max-width: 400px;
    overflow-wrap: break-word;
    word-break: break-all;
}}
th {{
    background: #1a1a2e;
    color: #f8f9fa;
    font-weight: 600;
    font-size: 0.85rem;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}}
tr:nth-child(even) {{ background: rgba(255,255,255,0.03); }}
tr:hover {{ background: rgba(255,255,255,0.06); }}

/* ── Badges ── */
.badge {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 12px;
    font-size: 0.8rem;
    font-weight: 600;
    line-height: 1.4;
}}
.badge-success {{ background: #28a745; color: #fff; }}
.badge-error {{ background: #dc3545; color: #fff; }}
.badge-gray {{ background: #6c757d; color: #fff; }}
.badge-blue {{ background: #007bff; color: #fff; }}
.badge-warning {{ background: #ffc107; color: #856404; }}

/* ── Utility ── */
.mono {{ font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace; font-size: 0.85rem; }}
.error-text {{ color: #dc3545; font-size: 0.85rem; }}
.dry-run-msg {{
    padding: 16px;
    text-align: center;
    color: #6c757d;
    font-style: italic;
    background: #1a1a2e;
    border-radius: 6px;
    margin: 8px 0;
}}
.status-tag {{ margin: 8px 0; }}
.en-info {{ margin: 6px 0; color: #a0a0b0; font-size: 0.9rem; }}
.no-data {{
    text-align: center;
    padding: 60px 20px;
    color: #6c757d;
    font-size: 1.2rem;
}}
h4 {{
    margin: 16px 0 8px;
    font-size: 1rem;
    color: #f8f9fa;
    border-bottom: 1px solid #2d2d44;
    padding-bottom: 6px;
}}

/* ── Responsive ── */
@media (min-width: 1200px) {{
    body {{ max-width: 1200px; margin: 0 auto; padding: 30px; }}
}}
@media (max-width: 768px) {{
    body {{ padding: 10px; }}
    .movie {{ padding: 12px; }}
    th, td {{ padding: 6px 8px; font-size: 0.85rem; }}
}}

/* ── JSON data script ── */
#diag-data {{ display: none; }}
</style>
</head>
<body>
<header>
<h1>字幕下载诊断报告 {status_badge}</h1>
<div class="header-meta">生成时间: {timestamp} | 电影数: {movie_count}/{total_movies} | 模式: {mode_label}</div>
</header>
<main>
{''.join(movie_sections)}
</main>
<script id="diag-data" type="application/json">
{json_data}
</script>
</body>
</html>'''

    return html


# ── HTTP server ──────────────────────────────────────────────────────────────

class DiagHandler(BaseHTTPRequestHandler):
    """Serves the diagnostic HTML report and JSON data."""

    results_store = []
    temp_dir = ""
    running = True  # True while search is still in progress

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            html = generate_html_report(self.results_store, running=self.running)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
        elif self.path == "/results.json":
            json_path = Path(self.temp_dir) / "diag_results.json"
            if json_path.exists():
                data = json_path.read_text(encoding="utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(data.encode("utf-8"))
            else:
                self.send_response(404)
                self.end_headers()
        elif self.path == "/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"running": self.running, "movies_done": len(self.results_store)}).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        """Suppress default request logging."""
        pass


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Subtitle diagnostic tool")
    parser.add_argument("--port", type=int, default=8899, help="HTTP server port (default: 8899)")
    parser.add_argument("--config", type=str, default="config.json", help="Path to config.json")
    parser.add_argument("--dry-run", action="store_true", help="Search only, no downloads")
    args = parser.parse_args()

    global test_config
    test_config = load_test_config(args.config)

    enabled = test_config.get("enabled_providers", ["zimuku"])
    if not enabled:
        enabled = ["zimuku"]

    temp_dir = tempfile.mkdtemp(prefix="sub_diag_")
    print(f"[diag] Temp directory: {temp_dir}")
    print(f"[diag] Config: {args.config}")
    print(f"[diag] Providers: {enabled}")
    print(f"[diag] Dry run: {args.dry_run}")
    print()

    results_store = []
    DiagHandler.results_store = results_store
    DiagHandler.temp_dir = temp_dir
    DiagHandler.running = True

    server = HTTPServer(("", args.port), DiagHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print(f"[diag] HTTP server: http://0.0.0.0:{args.port}/")
    print(f"[diag] JSON data: http://0.0.0.0:{args.port}/results.json")
    print(f"[diag] 页面会每3秒自动刷新，搜索完成后自动停止刷新")
    print()

    total_start = time.time()

    for i, movie in enumerate(TEST_MOVIES, 1):
        print(f"[{i}/{len(TEST_MOVIES)}] {movie['title']} ({movie['year']})")
        movie_start = time.time()

        # 1. Search chain
        print(f"  Searching...")
        search_data = run_search_chain(movie, enabled)
        print(f"  Keyword: {search_data['keyword']}")
        print(f"  Total results: {search_data['total_results']}")
        for psd in search_data["provider_search_data"]:
            print(f"    {psd['provider']}: {psd['result_count']} results ({psd['duration_ms']}ms) [{psd['status']}]")

        if args.dry_run:
            # Dry run — skip download and English supplement chains
            # Serialize SubtitleResult objects for JSON compatibility
            movie_result = {
                "title": movie["title"],
                "year": movie["year"],
                "filename": movie["filename"],
                "search": {
                    "keyword": search_data["keyword"],
                    "video_type": search_data["video_type"],
                    "total_results": search_data["total_results"],
                    "provider_search_data": search_data["provider_search_data"],
                    "all_results": [
                        {
                            "title": r.title,
                            "language": r.language,
                            "score": r.score,
                            "score_pct": _score_to_pct(r.score, search_data["video_type"]),
                            "provider": r.provider,
                            "page_url": r.page_url,
                        }
                        for r in search_data["all_results"]
                    ],
                },
                "download": None,
                "en_supplement": None,
                "dry_run": True,
                "duration_ms": round((time.time() - movie_start) * 1000, 1),
            }
            results_store.append(movie_result)
            print(f"  [DRY RUN] Skipping download & English supplement")
            print()
            continue

        # 2. Download chain
        print(f"  Downloading...")
        download_data = run_download_chain(movie, search_data, Path(temp_dir))
        print(f"  Eligible: {download_data['eligible_count']}, Downloaded: {download_data['success_count']}")
        for dr in download_data["download_records"]:
            print(f"    #{dr['rank']} {dr['provider']}: {dr['status']} (score={dr['score_pct']}%) {dr['title'][:50]}")
            if dr["status"] == "failed":
                print(f"      Failure: {dr['failure_reason'][:80]}")

        # 3. English supplement chain
        print(f"  English supplement...")
        zh_downloaded = any(
            dr.get("status") == "success" and dr.get("language", "") not in ("eng", "en")
            for dr in download_data["download_records"]
        )
        en_data = run_english_supplement_chain(
            movie, search_data, download_data["zh_best_pct"],
            download_data["eligible_count"], zh_downloaded,
            enabled, Path(temp_dir)
        )
        print(f"  EN supplement: activated={en_data['activated']}, reason={en_data['reason']}")
        if en_data["activated"]:
            for edr in en_data["en_downloads"]:
                print(f"    EN {edr['provider']}: {edr['status']} (score={edr['score_pct']}%) {edr['title'][:50]}")

        movie_duration = round((time.time() - movie_start) * 1000, 1)

        movie_result = {
            "title": movie["title"],
            "year": movie["year"],
            "filename": movie["filename"],
            "search": {
                "keyword": search_data["keyword"],
                "video_type": search_data["video_type"],
                "total_results": search_data["total_results"],
                "provider_search_data": search_data["provider_search_data"],
                # Serialize all_results for JSON
                "all_results": [
                    {
                        "title": r.title,
                        "language": r.language,
                        "score": r.score,
                        "score_pct": _score_to_pct(r.score, search_data["video_type"]),
                        "provider": r.provider,
                        "page_url": r.page_url,
                    }
                    for r in search_data["all_results"]
                ],
            },
            "download": {
                "lang": download_data["lang"],
                "eligible_count": download_data["eligible_count"],
                "success_count": download_data["success_count"],
                "zh_best_pct": download_data["zh_best_pct"],
                "download_records": download_data["download_records"],
            },
            "en_supplement": en_data,
            "dry_run": False,
            "duration_ms": movie_duration,
        }
        results_store.append(movie_result)
        print()

    total_duration = round((time.time() - total_start) * 1000, 1)

    DiagHandler.running = False

    # Save results JSON
    results_json_path = Path(temp_dir) / "diag_results.json"
    with open(results_json_path, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "total_duration_ms": total_duration,
            "config_path": args.config,
            "dry_run": args.dry_run,
            "providers": enabled,
            "movies": results_store,
        }, f, ensure_ascii=False, indent=2)
    print(f"[diag] Results saved to {results_json_path}")

    html_path = Path(temp_dir) / "index.html"
    html_path.write_text(generate_html_report(results_store, running=False), encoding="utf-8")

    print(f"[diag] 全部完成! 总耗时: {total_duration / 1000:.1f}s")
    print(f"[diag] 报告地址: http://0.0.0.0:{args.port}/")
    print(f"[diag] 按 Ctrl+C 退出")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[diag] Shutting down...")
        server.shutdown()
        try:
            shutil.rmtree(temp_dir)
            print(f"[diag] Cleaned up temp dir: {temp_dir}")
        except OSError:
            pass


if __name__ == "__main__":
    main()
