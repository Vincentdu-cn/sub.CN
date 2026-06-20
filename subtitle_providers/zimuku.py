"""Zimuku subtitle provider with inline core classes.

Core logic migrated from zimuku_downloader.py to eliminate cross-imports.
"""

import base64
import contextlib
import io
import os
import re
import time
import zipfile
from pathlib import Path
from random import randrange
from typing import Optional, List, Dict
from urllib.parse import urljoin, quote

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageFile

from .base import SubtitleResult, SubtitleProvider
from .utils import (
    _get_ddddocr,
    string_to_hex,
    _extract_best_subtitle,
    _extract_best_from_7z,
    compute_match_score,
)


class YunsuoSession:
    """requests.Session wrapper with automatic Yunsuo WAF bypass."""

    SERVER_URL = "https://srtku.com"

    def __init__(self, ocr_api_key="", ocr_secret_key=""):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
        self.location_re = re.compile(r'self\.location = "(.*)" \+ stringToHex\(')
        self.verification_image_re = re.compile(
            r'<img.*?src="data:image/bmp;base64,(.*?)".*?>'
        )
        self.ocr_api_key = ocr_api_key
        self.ocr_secret_key = ocr_secret_key

    def _solve_captcha(self, b64_data: str) -> str:
        """Decode BMP captcha and recognize text. Try ddddocr first, Baidu OCR as fallback."""
        raw = base64.b64decode(b64_data)
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        img = Image.open(io.BytesIO(raw))
        img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "png")
        buf.seek(0)

        # Primary: ddddocr
        try:
            result = _get_ddddocr().classification(buf.read())
            if result and result.strip():
                return result
        except Exception:
            pass

        # Fallback: Baidu OCR
        if self.ocr_api_key and self.ocr_secret_key:
            try:
                buf.seek(0)
                b64_png = base64.b64encode(buf.read()).decode("utf-8")
                result = self._baidu_ocr(b64_png)
                if result and result.strip():
                    return result
            except Exception:
                pass

        # Last resort: return empty (will trigger coordinate fallback in get())
        return ""

    def _baidu_ocr(self, image_base64: str) -> str:
        """Use Baidu OCR API as fallback captcha solver."""
        token_url = "https://aip.baidubce.com/oauth/2.0/token"
        params = {
            "grant_type": "client_credentials",
            "client_id": self.ocr_api_key,
            "client_secret": self.ocr_secret_key,
        }
        r = requests.post(token_url, params=params, timeout=10)
        access_token = r.json().get("access_token", "")
        if not access_token:
            return ""

        ocr_url = f"https://aip.baidubce.com/rest/2.0/ocr/v1/accurate?access_token={access_token}"
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
        data = {"image": image_base64}
        r = requests.post(ocr_url, headers=headers, data=data, timeout=10)
        result = r.json()
        words = result.get("words_result", [])
        return "".join(w.get("words", "") for w in words)

    def get(self, url: str, *args, **kwargs) -> requests.Response:
        """GET with automatic Yunsuo bypass (max 3 attempts)."""
        for attempt in range(3):
            r = self.session.get(url, *args, **kwargs)
            if r.status_code != 404 or not self.location_re.findall(r.text):
                return r

            print(f"  [WAF] Captcha challenge on attempt {attempt + 1}")
            tr = self.location_re.findall(r.text)
            imgs = self.verification_image_re.findall(r.text)

            if imgs:
                code = self._solve_captcha(imgs[0])
                print(f"  [WAF] Recognized captcha: '{code}'")
            else:
                code = f"{randrange(800, 1920)},{randrange(600, 1080)}"
                print(f"  [WAF] No image, using coordinate fallback: {code}")

            self.session.cookies.set("srcurl", string_to_hex(r.url), domain="srtku.com")
            if tr:
                verify_url = urljoin(self.SERVER_URL, tr[0] + string_to_hex(code))
                self.session.get(verify_url, allow_redirects=False, timeout=30)

        raise RuntimeError("Yunsuo WAF bypass failed after 3 attempts")

    def close(self):
        self.session.close()


class ZimukuSubtitle:
    """Represents a single subtitle entry."""

    def __init__(self, title: str, lang: str, detail_url: str,
                 session: YunsuoSession, score: Optional[int] = None):
        self.title = title
        self.language = lang
        self.detail_url = detail_url
        self.session = session
        self.score = score

    def __repr__(self):
        base = f"ZimukuSubtitle({self.language}: {self.title})"
        if self.score is not None:
            base += f" [Score: {self.score}]"
        return base


class ZimukuClient:
    """Standalone Zimuku subtitle client."""

    def __init__(self, ocr_api_key="", ocr_secret_key=""):
        self.session = YunsuoSession(ocr_api_key=ocr_api_key, ocr_secret_key=ocr_secret_key)

    def _detect_language(self, lang_td) -> str:
        """Detect subtitle language from the <td class='lang'> images."""
        flags = set()
        for img in lang_td.find_all("img"):
            src = img.get("src", "").lower()
            if "china" in src:
                flags.add("cn")
            if "hongkong" in src:
                flags.add("hk")
            if "uk" in src:
                flags.add("en")
            if "jollyroger" in src:
                flags.add("bilingual")

        if "bilingual" in flags:
            return "zho_chs+eng"
        if "cn" in flags:
            return "zho_chs"
        if "hk" in flags:
            return "zho_cht"
        if "en" in flags:
            return "eng"
        return "unknown"

    def score_search_results(self, results: List[ZimukuSubtitle],
                             video_info: dict) -> None:
        video_type = video_info.get('type', 'movie')
        for sub in results:
            score, _matched = compute_match_score(video_info, sub.title, video_type)
            sub.score = score

    def search(self, keyword: str, season: Optional[int] = None,
               episode: Optional[int] = None,
               video_info: Optional[dict] = None) -> List[ZimukuSubtitle]:
        """Search for subtitles and return a flat list."""
        has_cjk = bool(re.search(r'[\u4e00-\u9fff]', keyword))
        search_variants = [keyword]
        if season and not has_cjk:
            search_variants = [keyword + f".S{season:02d}", keyword]

        results = []
        for variant in search_variants:
            search_url = f"{self.session.SERVER_URL}/search?q={quote(variant)}"
            print(f"Searching: {search_url}")
            try:
                r = self.session.get(search_url, timeout=30)
                r.raise_for_status()
                soup = BeautifulSoup(r.content.decode("utf-8", "ignore"), "html.parser")
                items = soup.find_all("div", class_="item")
                if items:
                    print(f"Found {len(items)} result groups")
                    results = self._filter_and_parse(items, season)
                else:
                    print("No result groups found")
            except Exception:
                pass
            if results:
                break

        if video_info and results:
            self.score_search_results(results, video_info)
            results.sort(key=lambda s: s.score if s.score is not None else 0,
                         reverse=True)

        return results

    def _filter_and_parse(self, items, season: Optional[int] = None) -> List[ZimukuSubtitle]:
        results = []
        for item in items:
            title_a = item.find("p", class_="tt clearfix")
            if not title_a:
                continue
            title_a = title_a.find("a")
            if not title_a:
                continue

            title = title_a.get_text(strip=True)
            link = urljoin(self.session.SERVER_URL, title_a.get("href", ""))

            if season:
                season_cn = re.search("第(.*)季", title)
                season_en = re.search(r'[Ss](\d{1,2})', title)
                if season_cn:
                    season_text = season_cn.group(1).strip()
                    cn_map = "一二三四五六七八九十"
                    expected = cn_map[season - 1] if season <= 10 else str(season)
                    if season_text != expected:
                        continue
                elif season_en:
                    try:
                        if int(season_en.group(1)) != season:
                            continue
                    except ValueError:
                        pass

            subs = self._parse_episode_page(link)
            results.extend(subs)
        return results

    def _parse_episode_page(self, link: str) -> List[ZimukuSubtitle]:
        """Parse an episode/subtitle list page."""
        r = self.session.get(link, timeout=30)
        r.raise_for_status()

        soup = BeautifulSoup(r.content.decode("utf-8", "ignore"), "html.parser")
        tbody = soup.find("tbody")
        if not tbody:
            return []

        subs = []
        for row in tbody.find_all("tr"):
            a = row.find("a")
            if not a:
                continue

            name = a.get_text(strip=True)
            name = os.path.splitext(name)[0]

            lang_td = row.find("td", class_="tac lang")
            lang = self._detect_language(lang_td) if lang_td else "unknown"

            detail_url = urljoin(self.session.SERVER_URL, a.get("href", ""))
            subs.append(ZimukuSubtitle(name, lang, detail_url, self.session))

        return subs

    def download(self, subtitle: ZimukuSubtitle, output_dir: Path,
                 preferred_lang: Optional[str] = None,
                 video_filename: Optional[str] = None) -> tuple:
        """Download a single subtitle, extract best file, save to output_dir."""
        if preferred_lang and subtitle.language != preferred_lang:
            _zh_sim = {"zho_chs", "zho", "chi", "chs", "zho_chs+eng"}
            _zh_tra = {"zho_cht", "cht", "zho_cht+eng"}
            same_group = (preferred_lang in _zh_sim and subtitle.language in _zh_sim) or \
                         (preferred_lang in _zh_tra and subtitle.language in _zh_tra)
            if not same_group:
                return None, ""

        print(f"  Downloading: {subtitle.title} [{subtitle.language}]")

        # Step 1: Get detail page, find "down1" link
        r = self.session.get(subtitle.detail_url, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.content.decode("utf-8", "ignore"), "html.parser")

        down1 = soup.find("a", {"id": "down1"})
        if not down1:
            print("    [!] No download link found")
            return None, ""

        down_page_url = urljoin(subtitle.detail_url, down1.get("href", ""))

        # Step 2: Follow to download intermediate page
        r = self.session.get(down_page_url, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.content.decode("utf-8", "ignore"), "html.parser")

        download_link = soup.find("a", {"rel": "nofollow"})
        if not download_link:
            print("    [!] No direct download link found")
            return None, ""

        file_url = urljoin(down_page_url, download_link.get("href", ""))

        # Step 3: Download the actual file
        r = self.session.get(file_url, headers={"Referer": down_page_url}, timeout=(10, 15))
        r.raise_for_status()

        if not r.content:
            print("    [!] Empty response")
            return None, ""

        # Determine filename
        disposition = r.headers.get("Content-Disposition", "").lower()
        if "filename=" in disposition:
            fname = disposition.split("filename=")[-1].strip('"\'; ')
        elif r.content[:6] == b'7z\xbc\xaf\x27\x1c':
            fname = f"subtitle_{int(time.time())}.7z"
        else:
            fname = f"subtitle_{int(time.time())}.zip"

        output_dir.mkdir(parents=True, exist_ok=True)
        archive_path = output_dir / fname
        archive_path.write_bytes(r.content)
        print(f"    Archive saved: {archive_path}")

        # Step 4: Extract best subtitle
        extracted, original_name = self._extract(archive_path, output_dir,
                                  video_filename=video_filename,
                                  subtitle_lang=subtitle.language)
        if extracted:
            if extracted.suffix == ".txt":
                print(f"    [!] Archive contains a text file (likely Baidu Pan link)")
                print(f"    [!] Please check: {extracted}")
            else:
                print(f"    Subtitle extracted: {extracted}")
            return extracted, original_name
        else:
            print("    [!] Could not extract subtitle from archive")
            return archive_path, archive_path.name

    def _extract(self, archive_path: Path, output_dir: Path,
                 video_filename: Optional[str] = None,
                 subtitle_lang: str = "") -> tuple:
        """Extract the best subtitle file from RAR/ZIP archive."""
        from .base import _unique_output_path

        lang_map = {"zho_chs": "zh", "zho_cht": "zht", "eng": "en",
                    "zho_chs+eng": "zh+en", "zho_cht+eng": "zht+en",
                    "unknown": "zh"}
        lang_code = lang_map.get(subtitle_lang, "zh")

        def _out_name(ext: str) -> str:
            if video_filename:
                return f"{Path(video_filename).stem}.{lang_code}.zimuku{ext}"
            return archive_path.stem + ext

        data = archive_path.read_bytes()
        stream = io.BytesIO(data)

        # ZIP
        if zipfile.is_zipfile(stream):
            archive = zipfile.ZipFile(stream)
            content, best_name = _extract_best_subtitle(archive, archive_path.name)
            if content:
                ext = Path(best_name).suffix if best_name else ".srt"
                out_path = _unique_output_path(output_dir, _out_name(ext))
                out_path.write_bytes(content)
                return out_path, best_name or out_path.name
            for name in archive.namelist():
                if name.lower().endswith(".txt"):
                    out_path = _unique_output_path(output_dir, _out_name(".txt"))
                    out_path.write_bytes(archive.read(name))
                    return out_path, name

        # RAR
        try:
            import rarfile
            stream.seek(0)
            if rarfile.is_rarfile(stream):
                archive = rarfile.RarFile(stream)
                content, best_name = _extract_best_subtitle(archive, archive_path.name)
                if content:
                    ext = Path(best_name).suffix if best_name else ".srt"
                    out_path = _unique_output_path(output_dir, _out_name(ext))
                    out_path.write_bytes(content)
                    return out_path, best_name or out_path.name
                for name in archive.namelist():
                    if name.lower().endswith(".txt"):
                        out_path = _unique_output_path(output_dir, _out_name(".txt"))
                        out_path.write_bytes(archive.read(name))
                        return out_path, name
        except Exception:
            pass

        try:
            data = archive_path.read_bytes()
            if data[:6] == b'7z\xbc\xaf\x27\x1c':
                content, best_name = _extract_best_from_7z(data, archive_path.name)
                if content:
                    ext = Path(best_name).suffix if best_name else ".srt"
                    out_path = _unique_output_path(output_dir, _out_name(ext))
                    out_path.write_bytes(content)
                    return out_path, best_name or out_path.name
        except Exception:
            pass

        fname = archive_path.name.lower()
        if any(fname.endswith(ext) for ext in (".srt", ".ass", ".ssa", ".sub", ".vtt")):
            if video_filename:
                new_path = _unique_output_path(output_dir, _out_name(archive_path.suffix))
                new_path.write_bytes(archive_path.read_bytes())
                try:
                    archive_path.unlink()
                except OSError:
                    pass
                return new_path, new_path.name
            return archive_path, archive_path.name

        return None, ""

    def close(self):
        self.session.close()


class ZimukuProvider(SubtitleProvider):
    name = "zimuku"

    def __init__(self, ocr_api_key="", ocr_secret_key=""):
        self._client = None
        self._ocr_api_key = ocr_api_key
        self._ocr_secret_key = ocr_secret_key

    def _get_client(self):
        if self._client is None:
            self._client = ZimukuClient(
                ocr_api_key=self._ocr_api_key,
                ocr_secret_key=self._ocr_secret_key,
            )
        return self._client

    def _do_search(self, keyword: str, season: int = None,
                   episode: int = None, video_info: dict = None) -> List[SubtitleResult]:
        """Execute a single search against zimuku and return SubtitleResult list."""
        client = self._get_client()
        with contextlib.redirect_stdout(io.StringIO()):
            results = client.search(
                keyword=keyword, season=season,
                episode=episode, video_info=video_info,
            )
        return [
            SubtitleResult(
                title=sub.title,
                language=sub.language,
                page_url=sub.detail_url,
                provider=self.name,
                score=float(sub.score) if sub.score is not None else 0.0,
                extra={"_zimuku_sub": sub},
            )
            for sub in results
        ]

    def search(self, keyword: str, video_info: dict = None,
               season: int = None, episode: int = None) -> List[SubtitleResult]:
        try:
            search_keyword = keyword
            if video_info and video_info.get("imdb_id"):
                search_keyword = video_info["imdb_id"]

            results = self._do_search(search_keyword, season, episode, video_info)

            if (video_info and video_info.get("type") == "episode"
                    and video_info.get("season") and video_info.get("title")):
                s = video_info["season"]
                title = video_info["title"]
                cn_keywords = [f"{title} 第{s}季", f"{title} S{s:02d}"]
                existing_urls = {r.page_url for r in results}
                for extra_kw in cn_keywords:
                    if extra_kw == search_keyword:
                        continue
                    try:
                        extra_results = self._do_search(
                            extra_kw, season, episode, video_info,
                        )
                        for r in extra_results:
                            if r.page_url not in existing_urls:
                                results.append(r)
                                existing_urls.add(r.page_url)
                    except Exception:
                        continue

            return results
        except Exception:
            return []

    def download(self, result: SubtitleResult, output_dir: Path,
                 preferred_lang: str = "zho_chs",
                 video_filename: str = "") -> Optional[Path]:
        try:
            client = self._get_client()
            sub = result.extra.get("_zimuku_sub")
            if sub is None:
                return None
            with contextlib.redirect_stdout(io.StringIO()):
                path, original_name = client.download(
                    sub, output_dir,
                    preferred_lang=preferred_lang,
                    video_filename=video_filename or None,
                )
            if path and path.suffix in (".srt", ".ass", ".ssa"):
                for ext in (".zip", ".rar", ".7z"):
                    for f in output_dir.glob(f"*{ext}"):
                        try:
                            f.unlink()
                        except OSError:
                            pass
            result.extra["_original_name"] = original_name
            return path
        except Exception:
            return None
