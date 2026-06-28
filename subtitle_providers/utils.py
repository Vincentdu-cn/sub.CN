"""Shared subtitle utility functions across all providers.

Extracted from zimuku_downloader.py to eliminate duplication.
"""

import io
import os
import re
import zipfile
from typing import Optional, Tuple, Set, List, Dict

from guessit import guessit

SUBTITLE_EXTENSIONS = (".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".smi")

# Lazy-loaded ddddocr instance
_ddddocr_instance = None


def _get_ddddocr():
    """Lazy-load ddddocr to avoid cold-start on every import."""
    global _ddddocr_instance
    if _ddddocr_instance is None:
        import ddddocr
        _stderr = os.dup(2)
        _devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(_devnull, 2)
        try:
            _ddddocr_instance = ddddocr.DdddOcr(show_ad=False)
        finally:
            os.dup2(_stderr, 2)
            os.close(_devnull)
            os.close(_stderr)
    return _ddddocr_instance


def string_to_hex(s: str) -> str:
    """Simulate JS stringToHex: convert each char to hex (lowercase)."""
    return "".join(f"{ord(c):02x}" for c in s)


def _extract_best_subtitle(archive, filename: str) -> tuple:
    """Pick the best subtitle file from an archive.

    Returns (content_bytes, best_name) or (None, None) if not found.
    """
    candidates = []
    for name in archive.namelist():
        if os.path.basename(name).startswith("."):
            continue
        if not name.lower().endswith(SUBTITLE_EXTENSIONS):
            continue
        score = 0
        lower_name = name.lower()
        # Prefer .ass/.ssa/.srt
        if any(ext in lower_name for ext in (".ass", ".ssa", ".srt")):
            score += 1
        # Prefer Chinese simplified
        if any(kw in lower_name for kw in (
            "简体", "chs", ".gb.", "_gb.", "简中",
            ".zh.", "_zh.", ".chi.", "_chi.", ".zho_chs.", ".zho.", "_zho.",
        )):
            score += 2
        # Prefer Chinese traditional
        if any(kw in lower_name for kw in (
            "繁体", "cht", ".big5.", "_big5.", "繁中",
            ".zht.", "_zht.", ".zho_cht.",
        )):
            score += 2
        # Prefer bilingual
        if any(kw in lower_name for kw in (
            "中英", "简英", "繁英", "双语", "简体&英文", "繁体&英文",
            "chs.eng", "cht.eng",
            ".zh-en.", "_zh-en.", ".zh+en.", ".chs+eng.", ".cht+eng.",
            ".zho_chs+eng.", ".zho_en.",
        )):
            score += 4
        candidates.append((score, name))

    if not candidates:
        return None, None
    candidates.sort(key=lambda x: (-x[0], x[1]))
    best_name = candidates[0][1]
    return archive.read(best_name), best_name


def _extract_best_from_zip(data: bytes, archive_name: str) -> tuple:
    """Wrapper: extract best subtitle from ZIP bytes."""
    stream = io.BytesIO(data)
    if not zipfile.is_zipfile(stream):
        return None, None
    archive = zipfile.ZipFile(stream)
    return _extract_best_subtitle(archive, archive_name)


def _ensure_rarfile():
    """Configure rarfile's UNRAR_TOOL path if needed."""
    try:
        import rarfile
        if not rarfile.UNRAR_TOOL or rarfile.UNRAR_TOOL == "unrar":
            import shutil
            unrar = shutil.which("unrar") or shutil.which("unar") or "unrar"
            rarfile.UNRAR_TOOL = unrar
    except ImportError:
        pass


def _extract_best_from_rar(data: bytes, archive_name: str) -> tuple:
    """Wrapper: extract best subtitle from RAR bytes."""
    try:
        import rarfile
        _ensure_rarfile()
        stream = io.BytesIO(data)
        if rarfile.is_rarfile(stream):
            archive = rarfile.RarFile(stream)
            return _extract_best_subtitle(archive, archive_name)
    except Exception:
        pass
    return None, None


class _DictArchive:
    """Adapter that makes a dict of {name: bytes} quack like a zipfile
    so _extract_best_subtitle can score and read from it."""

    def __init__(self, files: dict):
        self._files = files

    def namelist(self):
        return list(self._files.keys())

    def read(self, name):
        return self._files[name]


def _extract_best_from_7z(data: bytes, archive_name: str) -> tuple:
    """Wrapper: extract best subtitle from 7z bytes."""
    try:
        import py7zr
        import tempfile
        stream = io.BytesIO(data)
        with py7zr.SevenZipFile(stream) as archive:
            names = archive.namelist()
            with tempfile.TemporaryDirectory() as tmpdir:
                archive.extractall(path=tmpdir)
                files = {}
                for name in names:
                    fpath = os.path.join(tmpdir, name)
                    if os.path.isfile(fpath):
                        with open(fpath, "rb") as f:
                            files[name] = f.read()
        if not files:
            return None, None
        return _extract_best_subtitle(_DictArchive(files), archive_name)
    except Exception:
        pass
    return None, None


def _strip_chinese_prefix(text: str) -> str:
    """Remove Chinese character prefix before English content starts."""
    m = re.match(r'^[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef\u00b7\u2027\s·]+', text)
    if m:
        return text[m.end():].lstrip()
    return text


_FLATTEN_RE = re.compile(r'[^a-z0-9]')


def flatten_filename(filename: str) -> str:
    return _FLATTEN_RE.sub('', filename.lower())


def parse_filename(filename: str) -> dict:
    try:
        cleaned = _strip_chinese_prefix(filename)
        result = guessit(cleaned)
        out = {}
        for key in ('title', 'year', 'source', 'video_codec',
                     'audio_codec', 'release_group', 'type', 'season', 'episode',
                     'edition', 'streaming_service'):
            if key in result:
                out[key] = result[key]
        if 'screen_size' in result:
            out['resolution'] = result['screen_size']
        # Extract color_spec: prefer HDR/DV from 'other' over 10bit from 'color_depth'
        if 'other' in result:
            others = result['other']
            if isinstance(others, str):
                others = [others]
            for o in others:
                flat_o = flatten_filename(o)
                for variants in COLOR_SPEC_EQUIVALENCE.values():
                    if flat_o in variants:
                        out['color_spec'] = o
                        break
                if 'color_spec' in out:
                    break
        if 'color_spec' not in out and 'color_depth' in result:
            out['color_spec'] = result['color_depth']
        return out
    except Exception:
        return {}


def is_filename(text: str) -> bool:
    lower = text.lower()
    if any(lower.endswith(ext) for ext in
           ('.mp4', '.mkv', '.avi', '.wmv', '.flv', '.webm', '.mov')):
        return True
    if re.search(r'\b(720p|1080p|2160p)\b', lower):
        return True
    if re.search(r'\.\d{4}\.(bluray|brrip|bdrip|webrip|web-?dl|hdtv|dvdrip)',
                 lower, re.IGNORECASE):
        return True
    return False


def build_search_keyword(parsed: dict) -> str:
    title = parsed.get('title', '')
    year = parsed.get('year')
    if title and year:
        return f"{title} {year}"
    return str(title)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

MOVIE_SCORES = {
    "title": 55, "year": 28, "source": 12, "release_group": 6,
    "streaming_service": 5, "edition": 5, "color_spec": 5,
    "audio_codec": 4, "resolution": 3, "video_codec": 2,
}

EPISODE_SCORES = {
    "series": 56, "year": 0, "season": 24, "episode": 24,
    "source": 12, "release_group": 7, "streaming_service": 6,
    "edition": 3, "color_spec": 5, "audio_codec": 3,
    "resolution": 3, "video_codec": 2,
}
EPISODE_YEAR_MISMATCH_PENALTY = 40

MOVIE_MAX_SCORE = sum(MOVIE_SCORES.values())
EPISODE_MAX_SCORE = sum(EPISODE_SCORES.values())

SOURCE_GROUPS = {
    "disk-hd": {"bluray", "bdrip", "brrip", "remux", "hddvd", "uhdbd", "ultrahdbluray"},
    "disk-sd": {"dvd", "dvdrip", "vhs"},
    "tv": {"hdtv", "sdtv", "ahdtv", "uhdtv", "hdrip", "pdtv"},
    "air": {"satrip", "dvb", "ppv", "dsrip"},
    "web": {"web", "webdl", "webrip"},
}

RESOLUTION_EQUIVALENCE = {
    "2160p": {"2160p", "4k"},
    "1080p": {"1080p"},
    "1080i": {"1080i"},
    "720p": {"720p"},
}

VIDEO_CODEC_EQUIVALENCE = {
    "h264": {"h264", "x264", "avc"},
    "h265": {"h265", "x265", "hevc"},
    "divx": {"divx"},
    "xvid": {"xvid"},
    "mpeg2": {"mpeg2"},
    "av1": {"av1"},
}

AUDIO_CODEC_EQUIVALENCE = {
    "aac": {"aac"},
    "dts": {"dts"},
    "dtshd": {"dtshd", "dtshr"},
    "dtshdma": {"dtshdma", "dtsma"},
    "dtsx": {"dtsx"},
    "atmos": {"atmos"},
    "truehd": {"truehd"},
    "ac3": {"ac3", "dd"},
    "eac3": {"eac3", "ddplus", "ddp"},
    "flac": {"flac"},
}

COLOR_SPEC_EQUIVALENCE = {
    "hdr": {"hdr", "hdr10"},
    "hdr10plus": {"hdr10plus"},
    "dolbyvision": {"dolbyvision", "dv", "dovi"},
    "10bit": {"10bit"},
    "8bit": {"8bit"},
    "sdr": {"sdr"},
}

STREAMING_SERVICE_EQUIVALENCE = {
    "netflix": {"netflix"},
    "amazon": {"amzn"},
    "disney": {"dsnp"},
    "hbomax": {"hmax"},
    "hbo": {"hbo"},
    "hulu": {"hulu"},
    "appletv": {"atvp"},
    "paramount": {"pmtp"},
    "peacock": {"pcok"},
    "crunchyroll": {"crunchyroll"},
}

EDITION_EQUIVALENCE = {
    "extended": {"extended"},
    "directorscut": {"directorscut"},
    "unrated": {"unrated", "uncut"},
    "theatrical": {"theatrical"},
    "remastered": {"remastered"},
    "imax": {"imax"},
    "specialedition": {"specialedition"},
    "criterion": {"criterion"},
    "limited": {"limited"},
}

RELEASE_GROUP_EQUIVALENCE = [
    frozenset({"yts", "yify"}),
    frozenset({"lol", "dimension"}),
    frozenset({"asap", "immerse", "fleet"}),
    frozenset({"avs", "sva"}),
    frozenset({"framestor", "w4nk3r", "bhdstudio"}),
]


def _find_equivalence_group(value: str, pool: dict) -> set:
    flat = flatten_filename(str(value))
    for canonical, variants in pool.items():
        if flat in variants or flat == canonical:
            return variants
    return {flat}


def _find_release_group_pool(value: str) -> set:
    flat = flatten_filename(str(value))
    for equiv_set in RELEASE_GROUP_EQUIVALENCE:
        if flat in equiv_set:
            return equiv_set
    return {flat}


def _normalize_title(title: str) -> str:
    return title.lower().replace("-", " ").replace("_", " ").replace(".", " ").strip()


def _extract_english_title(text: str) -> str:
    cleaned = text
    cleaned = re.sub(r'【[^】]*】', '', cleaned)
    cleaned = re.sub(r'（[^）]*）', '', cleaned)
    cleaned = re.sub(r'\([^)]*[\u4e00-\u9fff][^)]*\)', '', cleaned)
    cleaned = re.sub(r'\[[^\]]*\]', '', cleaned)

    m = re.search(r'([A-Z][A-Za-z0-9.]+?\.\d{4})', cleaned)
    if m:
        t = re.match(r'((?:[A-Z][A-Za-z0-9]+\.?)+)\.\d{4}', m.group(1))
        if t:
            return t.group(1).replace('.', ' ').strip()

    m = re.search(r'([A-Z][A-Za-z]+(?: [A-Za-z]+)+)\s+(\d{4})', cleaned)
    if m:
        return m.group(1).strip()

    m = re.search(r'([A-Z][A-Za-z]+(?: [A-Za-z]+)+)_', cleaned)
    if m:
        return m.group(1).strip()

    return ''


def _title_matches(video_title: str, subtitle_raw_name: str) -> bool:
    if not video_title:
        return False
    v_norm = _normalize_title(str(video_title))
    if not v_norm:
        return False
    s_norm = _normalize_title(subtitle_raw_name)
    if v_norm in s_norm or s_norm in v_norm:
        return True
    eng = _extract_english_title(subtitle_raw_name)
    if eng and _normalize_title(eng) == v_norm:
        return True
    flat_v = flatten_filename(str(video_title))
    flat_s = flatten_filename(subtitle_raw_name)
    if flat_v and flat_v in flat_s:
        return True
    return False


def _search_pool_in_flat(pool: set, flat_str: str) -> Optional[str]:
    best = None
    best_len = 0
    for variant in pool:
        if variant in flat_str and len(variant) > best_len:
            best = variant
            best_len = len(variant)
    return best


def _find_best_in_dimension(
    video_value: str, pool_dict: dict, flat_str: str,
) -> Tuple[bool, Optional[str]]:
    video_pool = _find_equivalence_group(video_value, pool_dict)

    best_canon = None
    best_variant = None
    best_len = 0

    for canon_key, variants in pool_dict.items():
        matched = _search_pool_in_flat(variants, flat_str)
        if matched and len(matched) > best_len:
            best_canon = canon_key
            best_variant = matched
            best_len = len(matched)

    if best_canon is not None:
        best_pool = pool_dict[best_canon]
        if video_pool is best_pool:
            return True, best_variant
        return False, best_variant

    matched = _search_pool_in_flat(video_pool, flat_str)
    if matched:
        return True, matched
    return False, None


def _consume_source_tokens(flat_str: str) -> str:
    all_variants = []
    for variants in SOURCE_GROUPS.values():
        all_variants.extend(variants)
    for v in sorted(all_variants, key=len, reverse=True):
        flat_str = flat_str.replace(v, '')
    return flat_str


def compute_match_score(
    video_info: dict,
    subtitle_raw_name: str,
    video_type: str = "movie",
) -> Tuple[int, Set[str]]:
    scores = EPISODE_SCORES if video_type == "episode" else MOVIE_SCORES
    total = 0
    matches = set()

    # Title
    title_key = "series" if video_type == "episode" else "title"
    v_title = video_info.get("title")
    title_matched = False
    if v_title and _title_matches(v_title, subtitle_raw_name):
        total += scores.get(title_key, 0)
        matches.add("title")
        title_matched = True

    flat_sub = flatten_filename(subtitle_raw_name)

    # Remove matched title to prevent pollution
    if title_matched and v_title:
        flat_title = flatten_filename(str(v_title))
        if flat_title:
            pos = flat_sub.find(flat_title)
            if pos >= 0:
                flat_sub = flat_sub[:pos] + flat_sub[pos + len(flat_title):]

    # Year
    v_year = video_info.get("year")
    if v_year is not None:
        year_str = str(v_year)
        sub_years = re.findall(r'(19\d{2}|20\d{2})', flat_sub)
        if sub_years:
            if year_str in sub_years:
                total += scores.get("year", 0)
                matches.add("year")
                for y in sub_years:
                    flat_sub = flat_sub.replace(y, '', 1)
            else:
                if video_type == "episode":
                    total -= EPISODE_YEAR_MISMATCH_PENALTY

    # Season
    v_season = video_info.get("season")
    if v_season is not None and video_type == "episode":
        season_str = f"s{int(v_season):02d}"
        if season_str in flat_sub:
            total += scores.get("season", 0)
            matches.add("season")

    # Episode
    v_episode = video_info.get("episode")
    if v_episode is not None and video_type == "episode":
        ep_str = f"e{int(v_episode):02d}"
        if ep_str in flat_sub:
            total += scores.get("episode", 0)
            matches.add("episode")

    # Source
    v_source = video_info.get("source")
    if v_source:
        source_hit, source_var = _find_best_in_dimension(
            v_source, SOURCE_GROUPS, flat_sub)
        if source_hit:
            total += scores.get("source", 0)
            matches.add("source")
    # Consume ALL source tokens to prevent cross-dimension collisions
    # (e.g., 'dv' inside 'dvd', 'hdr' inside 'hdrip', 'dd' inside 'hddvd')
    flat_sub = _consume_source_tokens(flat_sub)

    # Resolution
    v_res = video_info.get("resolution")
    if v_res:
        res_pool = _find_equivalence_group(v_res, RESOLUTION_EQUIVALENCE)
        matched = _search_pool_in_flat(res_pool, flat_sub)
        if matched:
            total += scores.get("resolution", 0)
            matches.add("resolution")
            flat_sub = flat_sub.replace(matched, '', 1)

    # Video codec
    v_vc = video_info.get("video_codec")
    if v_vc:
        vc_pool = _find_equivalence_group(v_vc, VIDEO_CODEC_EQUIVALENCE)
        matched = _search_pool_in_flat(vc_pool, flat_sub)
        if matched:
            total += scores.get("video_codec", 0)
            matches.add("video_codec")
            flat_sub = flat_sub.replace(matched, '', 1)

    # Audio codec
    v_ac = video_info.get("audio_codec")
    if v_ac:
        ac_hit, ac_var = _find_best_in_dimension(
            v_ac, AUDIO_CODEC_EQUIVALENCE, flat_sub)
        if ac_hit:
            total += scores.get("audio_codec", 0)
            matches.add("audio_codec")
            if ac_var:
                flat_sub = flat_sub.replace(ac_var, '', 1)

    # Color spec — after source consumption, 'dv' can't match inside 'dvd'
    v_color = video_info.get("color_spec")
    if v_color:
        cs_pool = _find_equivalence_group(v_color, COLOR_SPEC_EQUIVALENCE)
        matched = _search_pool_in_flat(cs_pool, flat_sub)
        if matched:
            total += scores.get("color_spec", 0)
            matches.add("color_spec")
            flat_sub = flat_sub.replace(matched, '', 1)

    # Streaming service
    v_ss = video_info.get("streaming_service")
    if v_ss:
        ss_pool = _find_equivalence_group(v_ss, STREAMING_SERVICE_EQUIVALENCE)
        matched = _search_pool_in_flat(ss_pool, flat_sub)
        if matched:
            total += scores.get("streaming_service", 0)
            matches.add("streaming_service")
            flat_sub = flat_sub.replace(matched, '', 1)

    # Edition
    v_ed = video_info.get("edition")
    if v_ed:
        ed_pool = _find_equivalence_group(v_ed, EDITION_EQUIVALENCE)
        matched = _search_pool_in_flat(ed_pool, flat_sub)
        if matched:
            total += scores.get("edition", 0)
            matches.add("edition")
            flat_sub = flat_sub.replace(matched, '', 1)

    # Release group (requires source match per Bazarr rule)
    v_rg = video_info.get("release_group")
    if v_rg and "source" in matches:
        rg_pool = _find_release_group_pool(v_rg)
        matched = _search_pool_in_flat(rg_pool, flat_sub)
        if matched:
            total += scores.get("release_group", 0)
            matches.add("release_group")

    return total, matches
