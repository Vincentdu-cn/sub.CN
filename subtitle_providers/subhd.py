import base64
import io
import os
import re
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Optional, List
from urllib.parse import urljoin, quote

import requests
from bs4 import BeautifulSoup

from .base import SubtitleResult, SubtitleProvider, _unique_output_path
from .utils import (
    SUBTITLE_EXTENSIONS,
    _extract_best_from_zip,
    _extract_best_from_rar,
    _extract_best_from_7z,
    _ensure_rarfile,
    _get_ddddocr,
)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0",
]


def _pick_ua():
    return USER_AGENTS[int(time.time()) % len(USER_AGENTS)]


def _detect_language(text: str, lang_tags: str = "") -> str:
    """Detect subtitle language.

    Args:
        text: Title text for fallback detection.
        lang_tags: Space-separated tags from detail page (e.g. "双语 简体 英语").
                   When available, these are authoritative and take priority.
    """
    # --- Authoritative path: use detail page language tags ---
    if lang_tags:
        tags = set(lang_tags.split())
        if "双语" in tags:
            return "zho_chs+eng"
        has_chs = "简体" in tags
        has_cht = "繁体" in tags
        has_eng = "英语" in tags or "英文" in tags
        if has_chs and has_eng:
            return "zho_chs+eng"
        if has_cht and has_eng:
            return "zho_cht+eng"
        if has_chs and has_cht:
            return "zho_chs"  # 简繁都有，优先简体
        if has_chs:
            return "zho_chs"
        if has_cht:
            return "zho_cht"
        if has_eng:
            return "eng"

    # --- Fallback path: guess from title text ---
    lower = text.lower()
    has_chs = any(kw in lower for kw in ("简体", "chs", "gb", "简中", "简英", "简繁"))
    has_cht = any(kw in lower for kw in ("繁体", "cht", "big5", "繁中", "繁英"))
    has_eng = any(kw in lower for kw in ("英文", "eng", "english"))
    has_bilingual = any(kw in lower for kw in ("中英", "双语", "简体&英文", "繁体&英文", "chs.eng", "cht.eng"))

    if has_bilingual:
        return "zho_chs+eng"
    if has_chs:
        return "zho_chs"
    if has_cht:
        return "zho_cht"
    if has_eng and not has_chs and not has_cht:
        return "eng"
    if re.search(r'[\u4e00-\u9fff]', text):
        return "zho_chs"
    return "zho_chs"


class SubHDCaptchaSolver:

    def solve(self, svg_content: str,
              ocr_api_key: str = "", ocr_secret_key: str = "") -> str:
        # 1. Split into per-char SVGs, render each to PNG, OCR individually
        result = self._ocr_per_char(svg_content, ocr_api_key, ocr_secret_key)
        if result:
            return result

        # 2. Fallback: render whole SVG as one image, OCR the whole thing
        return self._ocr_whole(svg_content, ocr_api_key, ocr_secret_key)

    def _ocr_per_char(self, svg_content: str,
                      ocr_api_key: str = "", ocr_secret_key: str = "") -> str:
        char_svgs = self._split_svg_chars(svg_content)
        if not char_svgs:
            return ""

        result_chars = []
        for char_svg in char_svgs:
            png = self._render_single_svg(char_svg)
            if not png:
                return ""

            char = self._ocr_single_char(png, ocr_api_key, ocr_secret_key)
            if not char:
                return ""
            result_chars.append(char)

        return "".join(result_chars)

    def _ocr_single_char(self, png_data: bytes,
                         ocr_api_key: str = "", ocr_secret_key: str = "") -> str:
        # Primary: ddddocr
        try:
            text = _get_ddddocr().classification(png_data)
            if text and text.strip() and len(text.strip()) == 1:
                return text.strip()
        except Exception:
            pass

        # Fallback: Baidu OCR
        if ocr_api_key and ocr_secret_key:
            try:
                b64 = base64.b64encode(png_data).decode("utf-8")
                text = self._baidu_ocr(b64, ocr_api_key, ocr_secret_key)
                if text and text.strip() and len(text.strip()) == 1:
                    return text.strip()
            except Exception:
                pass

        return ""

    def _ocr_whole(self, svg_content: str,
                   ocr_api_key: str = "", ocr_secret_key: str = "") -> str:
        png_data = self._render_single_svg(svg_content)
        if not png_data:
            return ""

        try:
            text = _get_ddddocr().classification(png_data)
            if text and text.strip():
                return text.strip()
        except Exception:
            pass

        if ocr_api_key and ocr_secret_key:
            try:
                b64 = base64.b64encode(png_data).decode("utf-8")
                text = self._baidu_ocr(b64, ocr_api_key, ocr_secret_key)
                if text and text.strip():
                    return text.strip()
            except Exception:
                pass

        return ""

    def _render_single_svg(self, svg_content: str) -> Optional[bytes]:
        try:
            import cairosvg
            png = cairosvg.svg2png(bytestring=svg_content.encode("utf-8"))
            # Composite onto white background (cairosvg renders transparent bg)
            from PIL import Image
            img = Image.open(io.BytesIO(png)).convert("RGBA")
            bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
            bg.paste(img, (0, 0), img)
            buf = io.BytesIO()
            bg.convert("RGB").save(buf, "PNG")
            return buf.getvalue()
        except Exception:
            pass
        return None

    def _split_svg_chars(self, svg_content: str) -> list:
        ns_match = re.search(r'xmlns="([^"]+)"', svg_content)
        xmlns = ns_match.group(0) if ns_match else 'xmlns="http://www.w3.org/2000/svg"'

        chars = []
        for m in re.finditer(r'<path\s+([^>]+)>', svg_content):
            attrs = m.group(1)
            if 'fill="none"' in attrs:
                continue

            d_match = re.search(r'd="([^"]+)"', attrs)
            if not d_match:
                continue
            d_val = d_match.group(1)
            if len(d_val) <= 500:
                continue

            x_match = re.search(r'M\s*(\d+(?:\.\d*)?)', d_val)
            x = float(x_match.group(1)) if x_match else 0.0

            # Compute bounding box from path coordinates
            all_nums = [float(n) for n in re.findall(r'-?\d+(?:\.\d+)?', d_val)]
            if len(all_nums) < 4:
                continue

            # Coordinates come in pairs: x,y x,y ... (SVG path commands)
            xs = all_nums[0::2]
            ys = all_nums[1::2]
            min_x, max_x = min(xs), max(xs)
            min_y, max_y = min(ys), max(ys)

            pad = 2
            vb_x = min_x - pad
            vb_y = min_y - pad
            vb_w = max_x - min_x + pad * 2
            vb_h = max_y - min_y + pad * 2
            img_w = max(int(vb_w), 20)
            img_h = max(int(vb_h), 30)

            char_svg = (
                f'<svg {xmlns} viewBox="{vb_x:.1f} {vb_y:.1f} {vb_w:.1f} {vb_h:.1f}" '
                f'width="{img_w}" height="{img_h}">'
                f'<path d="{d_val}" fill="#000"/>'
                f'</svg>'
            )
            chars.append((x, char_svg))

        chars.sort(key=lambda c: c[0])
        return [svg for _, svg in chars]

    def _baidu_ocr(self, image_base64: str,
                   api_key: str, secret_key: str) -> str:
        token_url = "https://aip.baidubce.com/oauth/2.0/token"
        params = {
            "grant_type": "client_credentials",
            "client_id": api_key,
            "client_secret": secret_key,
        }
        r = requests.post(token_url, params=params, timeout=10)
        access_token = r.json().get("access_token", "")
        if not access_token:
            return ""

        ocr_url = f"https://aip.baidubce.com/rest/2.0/ocr/v1/accurate?access_token={access_token}"
        headers = {"Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json"}
        data = {"image": image_base64}
        r = requests.post(ocr_url, headers=headers, data=data, timeout=10)
        result = r.json()
        words = result.get("words_result", [])
        return "".join(w.get("words", "") for w in words)


_captcha_solver = SubHDCaptchaSolver()


class SubHDProvider(SubtitleProvider):
    name = "subhd"

    BASE_URL = "https://subhd.tv"

    def __init__(self, ocr_api_key: str = "", ocr_secret_key: str = ""):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = _pick_ua()
        self.ocr_api_key = ocr_api_key
        self.ocr_secret_key = ocr_secret_key

    def _get(self, url: str, **kwargs) -> Optional[requests.Response]:
        try:
            self.session.headers["User-Agent"] = _pick_ua()
            r = self.session.get(url, timeout=20, **kwargs)
            return r
        except Exception:
            return None

    def _extract_sid(self, page_url: str) -> Optional[str]:
        m = re.search(r'/(?:detail|sub)/(\d+)', page_url)
        if m:
            return m.group(1)
        m = re.search(r'/a/([A-Za-z0-9]+)', page_url)
        if m:
            return m.group(1)
        try:
            r = self._get(page_url)
            if r and r.status_code == 200:
                m2 = re.search(r'sid["\s:=]+["\']?(\d+)', r.text)
                if m2:
                    return m2.group(1)
                m3 = re.search(r'/a/([A-Za-z0-9]+)', r.text)
                if m3:
                    return m3.group(1)
                soup = BeautifulSoup(r.text, "html.parser")
                btn = soup.find("a", {"id": "down1"})
                if btn:
                    href = btn.get("href", "")
                    m4 = re.search(r'/(\d+)', href)
                    if m4:
                        return m4.group(1)
        except Exception:
            pass
        return None

    def _fetch_bytes(self, url: str, referer: str = "", timeout: int = 20) -> Optional[bytes]:
        try:
            hdrs = {"Referer": referer, "User-Agent": _pick_ua()}
            r = self.session.get(url, headers=hdrs, timeout=(5, timeout))
            if r.status_code == 200 and r.content:
                return r.content
        except Exception:
            pass
        try:
            cmd = ["curl", "-sL", "--max-time", str(timeout), "-o", "-"]
            if referer:
                cmd += ["-e", referer]
            cmd.append(url)
            r = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            if r.returncode == 0 and r.stdout:
                return r.stdout
        except Exception:
            pass
        return None

    def _download_via_api(self, sid: str, referer: str) -> Optional[bytes]:
        api_url = f"{self.BASE_URL}/api/sub/down"
        headers = {
            "Referer": referer,
            "Content-Type": "application/json",
            "User-Agent": _pick_ua(),
        }
        payload = {"sid": sid, "cap": ""}

        for attempt in range(6):
            try:
                r = self.session.post(api_url, json=payload, headers=headers, timeout=20)
                data = r.json()

                if data.get("pass") is True or data.get("url"):
                    download_url = data.get("url", "")
                    if download_url:
                        if not download_url.startswith("http"):
                            download_url = urljoin(self.BASE_URL, download_url)
                        content = self._fetch_bytes(download_url, referer=api_url, timeout=30)
                        if content:
                            return content
                    continue

                svg = data.get("msg", "")
                if svg and "<svg" in svg:
                    code = _captcha_solver.solve(svg, self.ocr_api_key, self.ocr_secret_key)
                    payload["cap"] = code
                    time.sleep(0.5)
                    continue

                break
            except Exception:
                break
        return None

    def search(self, keyword: str, video_info: dict = None,
               season: int = None, episode: int = None) -> List[SubtitleResult]:
        try:
            if video_info and video_info.get("imdb_id"):
                search_keyword = video_info["imdb_id"]
            else:
                search_keyword = keyword
            if season:
                search_keyword += f" S{season:02d}"
                if episode:
                    search_keyword += f"E{episode:02d}"

            url = f"{self.BASE_URL}/search/{quote(search_keyword)}"
            r = self._get(url)
            if r is None or r.status_code != 200:
                return []

            soup = BeautifulSoup(r.text, "html.parser")
            results = []
            seen_hrefs = set()

            for link_tag in soup.find_all("a", href=True):
                href = link_tag["href"]
                if not href.startswith("/a/") and "/detail/" not in href and "/sub/" not in href:
                    continue
                if href in seen_hrefs:
                    continue

                title = link_tag.get_text(strip=True)
                if not title or len(title) < 3:
                    continue

                seen_hrefs.add(href)
                page_url = urljoin(self.BASE_URL, href)

                full_title = title
                lang_tags = ""
                if len(results) < 20:
                    fetched_title, fetched_tags = self._fetch_detail_title(page_url)
                    if fetched_title:
                        full_title = fetched_title
                    if fetched_tags:
                        lang_tags = fetched_tags

                results.append(SubtitleResult(
                    title=full_title,
                    language=_detect_language(full_title, lang_tags),
                    page_url=page_url,
                    provider=self.name,
                    score=0.0,
                ))

            time.sleep(0.5)
            return results[:30]
        except Exception:
            return []

    def _fetch_detail_title(self, page_url: str) -> tuple:
        """Return (title, lang_tags_str) from detail page.
        lang_tags_str is space-separated e.g. "双语 简体 英语".
        """
        try:
            r = self._get(page_url)
            if r is None or r.status_code != 200:
                return None, ""
            soup = BeautifulSoup(r.text, "html.parser")
            title_tag = soup.find("title")
            title = title_tag.get_text(strip=True).split(" - ")[0].replace("分享交流下载字幕平台", "").strip() if title_tag else None
            lang_tags = []
            for span in soup.find_all("span", class_="p-1"):
                t = span.get_text(strip=True)
                if t in ("双语", "简体", "繁体", "英语", "英文", "日语", "法语", "西班牙语", "俄语"):
                    lang_tags.append(t)
            return title, " ".join(lang_tags)
        except Exception:
            return None, ""

    def download(self, result: SubtitleResult, output_dir: Path,
                 preferred_lang: str = "zho_chs",
                 video_filename: str = "") -> Optional[Path]:
        try:
            content = self._download_via_api_flow(result)
            if content:
                path, original_name = self._save_content(
                    content, output_dir, video_filename,
                    subtitle_lang=result.language or preferred_lang)
                result.extra["_original_name"] = original_name
                return path
        except Exception:
            pass

        path, original_name = self._download_via_scraping(
            result, output_dir, video_filename,
            subtitle_lang=result.language or preferred_lang)
        result.extra["_original_name"] = original_name
        return path

    def _download_via_api_flow(self, result: SubtitleResult) -> Optional[bytes]:
        # Visit detail page first to set session cookies
        try:
            self._get(result.page_url)
        except Exception:
            pass

        sid = self._extract_sid(result.page_url)
        if not sid:
            return None

        content = self._download_via_api(sid, result.page_url)
        return content

    def _download_via_scraping(self, result: SubtitleResult,
                               output_dir: Path,
                               video_filename: str,
                               subtitle_lang: str = "") -> tuple:
        try:
            r = self._get(result.page_url)
            if r is None:
                return None, ""

            soup = BeautifulSoup(r.text, "html.parser")

            download_url = None
            for a in soup.find_all("a", href=True):
                href = a["href"]
                text = a.get_text(strip=True).lower()
                if any(kw in text for kw in ("下载", "download")):
                    download_url = urljoin(self.BASE_URL, href)
                    break
                if any(kw in href for kw in ("/down/", "/download/", "/file/")):
                    download_url = urljoin(self.BASE_URL, href)
                    break

            if download_url is None:
                btn = soup.find("a", {"id": "down1"}) or soup.find("a", class_="download")
                if btn and btn.get("href"):
                    download_url = urljoin(self.BASE_URL, btn["href"])

            if download_url is None:
                return None, ""

            time.sleep(1)
            r = self._get(download_url, headers={"Referer": result.page_url})
            if r is None or not r.content:
                return None, ""

            content_type = r.headers.get("Content-Type", "")
            if "text/html" in content_type and len(r.content) < 50000:
                soup = BeautifulSoup(r.text, "html.parser")
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if any(href.lower().endswith(ext) for ext in (".zip", ".rar", ".7z", ".srt", ".ass")):
                        direct_url = urljoin(download_url, href)
                        r = self._get(direct_url, headers={"Referer": download_url})
                        if r and r.content:
                            break

            if not r or not r.content:
                return None, ""

            return self._save_content(r.content, output_dir, video_filename,
                                      disposition=r.headers.get("Content-Disposition", ""),
                                      subtitle_lang=subtitle_lang)
        except Exception:
            return None, ""

    def _save_content(self, content: bytes, output_dir: Path,
                      video_filename: str,
                      disposition: str = "",
                      subtitle_lang: str = "") -> tuple:
        # Detect HTML error pages masquerading as downloads
        if content[:100].strip().startswith(b'<!') or content[:100].strip().startswith(b'<html'):
            return None, ""

        output_dir.mkdir(parents=True, exist_ok=True)

        if "filename=" in disposition:
            fname = disposition.split("filename=")[-1].strip('"\'; ')
        elif content[:2] == b'PK':
            fname = f"subhd_{int(time.time())}.zip"
        elif b'[Script Info]' in content[:500]:
            fname = f"subhd_{int(time.time())}.ass"
        elif b'-->' in content[:1000] and b'\n' in content[:200]:
            fname = f"subhd_{int(time.time())}.srt"
        elif content[:4] == b'Rar!':
            fname = f"subhd_{int(time.time())}.rar"
        elif content[:6] == b'7z\xbc\xaf\x27\x1c':
            fname = f"subhd_{int(time.time())}.7z"
        else:
            return None, ""

        archive_path = output_dir / fname
        archive_path.write_bytes(content)

        extracted, original_name = self._extract_subtitle(archive_path, output_dir, video_filename, subtitle_lang)
        if extracted:
            try:
                archive_path.unlink()
            except OSError:
                pass
            return extracted, original_name
        return archive_path, archive_path.name

    def _extract_subtitle(self, archive_path: Path, output_dir: Path,
                          video_filename: str = "", subtitle_lang: str = "") -> tuple:
        try:
            lang_map = {"zho_chs": "zh", "zho_cht": "zht", "eng": "en",
                        "zho_chs+eng": "zh+en", "zho_cht+eng": "zht+en",
                        "unknown": "zh"}
            lang_code = lang_map.get(subtitle_lang, "zh")

            def _out_name(ext: str) -> str:
                if video_filename:
                    return f"{Path(video_filename).stem}.{lang_code}.subhd{ext}"
                return archive_path.stem + ext

            data = archive_path.read_bytes()
            fname = archive_path.name.lower()

            if fname.endswith(".zip") or zipfile.is_zipfile(io.BytesIO(data)):
                content, best_name = _extract_best_from_zip(data, archive_path.name)
                if content:
                    ext = Path(best_name).suffix if best_name else ".srt"
                    out_path = _unique_output_path(output_dir, _out_name(ext))
                    out_path.write_bytes(content)
                    return out_path, best_name or out_path.name

            if any(fname.endswith(ext) for ext in SUBTITLE_EXTENSIONS):
                if video_filename:
                    new_path = _unique_output_path(output_dir, _out_name(archive_path.suffix))
                    new_path.write_bytes(archive_path.read_bytes())
                    try:
                        archive_path.unlink()
                    except OSError:
                        pass
                    return new_path, new_path.name
                return archive_path, archive_path.name

            try:
                import rarfile
                _ensure_rarfile()
                stream = io.BytesIO(data)
                if rarfile.is_rarfile(stream):
                    content, best_name = _extract_best_from_rar(data, archive_path.name)
                    if content:
                        ext = Path(best_name).suffix if best_name else ".srt"
                        out_path = _unique_output_path(output_dir, _out_name(ext))
                        out_path.write_bytes(content)
                        return out_path, best_name or out_path.name
            except Exception:
                pass

            try:
                if data[:6] == b'7z\xbc\xaf\x27\x1c':
                    content, best_name = _extract_best_from_7z(data, archive_path.name)
                    if content:
                        ext = Path(best_name).suffix if best_name else ".srt"
                        out_path = _unique_output_path(output_dir, _out_name(ext))
                        out_path.write_bytes(content)
                        return out_path, best_name or out_path.name
            except Exception:
                pass

            return None, ""
        except Exception:
            return None, ""
