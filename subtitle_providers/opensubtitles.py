import io
import os
import time
import zipfile
from pathlib import Path
from typing import Optional, List

import requests

from .base import SubtitleResult, SubtitleProvider, _unique_output_path
from .utils import SUBTITLE_EXTENSIONS, _extract_best_from_zip, _extract_best_from_rar, _extract_best_from_7z


class OpenSubtitlesProvider(SubtitleProvider):
    name = "opensubtitles"

    API_BASE = "https://api.opensubtitles.com/api/v1"

    def __init__(self, api_key: str = "", username: str = "", password: str = ""):
        self.api_key = api_key or os.environ.get("OPENSUBTITLES_API_KEY", "")
        self.username = username or os.environ.get("OPENSUBTITLES_USERNAME", "")
        self.password = password or os.environ.get("OPENSUBTITLES_PASSWORD", "")
        self.session = requests.Session()
        self.session.headers["Api-Key"] = self.api_key
        self.session.headers["User-Agent"] = "SubSearch v1.0"
        self._token = None

    def _login(self) -> bool:
        if self._token:
            return True
        if not self.api_key or not self.username or not self.password:
            return False
        try:
            r = self.session.post(
                f"{self.API_BASE}/login",
                json={"username": self.username, "password": self.password},
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()
            self._token = data.get("token", "")
            if self._token:
                self.session.headers["Authorization"] = f"Bearer {self._token}"
                return True
            return False
        except Exception:
            return False

    def search(self, keyword: str, video_info: dict = None,
               season: int = None, episode: int = None) -> List[SubtitleResult]:
        if not self.api_key:
            return []

        try:
            if video_info and video_info.get("imdb_id"):
                imdb_raw = video_info["imdb_id"]
                imdb_num = int(imdb_raw.lstrip("t") or "0")
                params = {"imdb_id": imdb_num}
            else:
                params = {"query": keyword}
                if video_info and video_info.get("year"):
                    params["year"] = str(video_info["year"])
            if season:
                params["season_number"] = season
            if episode:
                params["episode_number"] = episode

            languages = "zh-cn,zh-tw,en"
            params["languages"] = languages

            r = self.session.get(
                f"{self.API_BASE}/subtitles",
                params=params, timeout=20,
            )
            if r.status_code != 200:
                return []

            data = r.json()
            subs = data.get("data", [])
            results = []

            for sub in subs:
                attrs = sub.get("attributes", {})
                lang_code = attrs.get("language", "").lower()
                lang = "eng"
                if lang_code in ("zh-cn", "zho", "chi"):
                    lang = "zho_chs"
                elif lang_code in ("zh-tw", "zht"):
                    lang = "zho_cht"
                elif lang_code == "zh-cn-zh-tw":
                    lang = "zho_chs"
                elif lang_code == "zho_chs+eng":
                    lang = "zho_chs+eng"
                elif lang_code in ("en", "eng"):
                    lang = "eng"

                release_name = attrs.get("release_name", "")
                files = attrs.get("files", [])
                if not release_name:
                    file_name = files[0].get("file_name", "") if files else ""
                    if file_name:
                        from pathlib import Path as _P
                        release_name = _P(file_name).stem
                    else:
                        release_name = attrs.get("feature_details", {}).get("movie_name", "")
                sub_id = sub.get("id", "")

                download_url = ""
                if files:
                    download_url = files[0].get("file_id", "")

                results.append(SubtitleResult(
                    title=release_name,
                    language=lang,
                    download_url=str(download_url),
                    provider=self.name,
                    score=0.0,
                    page_url=f"https://opensubtitles.org/subtitles/{sub_id}" if sub_id else "",
                    extra={"file_id": download_url, "sub_id": sub_id},
                ))

            return results[:30]
        except Exception:
            return []

    def download(self, result: SubtitleResult, output_dir: Path,
                 preferred_lang: str = "zho_chs",
                 video_filename: str = "") -> Optional[Path]:
        if not self.api_key:
            return None

        try:
            file_id = result.extra.get("file_id")
            if not file_id:
                return None

            r = self.session.post(
                f"{self.API_BASE}/download",
                json={"file_id": int(file_id)},
                timeout=20,
            )
            if r.status_code == 401:
                self._token = None
                if self._login():
                    r = self.session.post(
                        f"{self.API_BASE}/download",
                        json={"file_id": int(file_id)},
                        timeout=20,
                    )

            if r.status_code not in (200, 201):
                return None

            data = r.json()
            download_url = data.get("link", "")
            if not download_url:
                return None

            r = self.session.get(download_url, timeout=30)
            if r.status_code != 200 or not r.content:
                return None

            output_dir.mkdir(parents=True, exist_ok=True)

            disposition = r.headers.get("Content-Disposition", "")
            if "filename=" in disposition:
                fname = disposition.split("filename=")[-1].strip('"\'; ')
            elif r.content[:2] == b'PK':
                fname = f"opensubtitles_{int(time.time())}.zip"
            elif b'-->' in r.content[:1000] and b'\n' in r.content[:200]:
                fname = f"opensubtitles_{int(time.time())}.srt"
            else:
                fname = f"opensubtitles_{int(time.time())}.zip"

            archive_path = output_dir / fname
            archive_path.write_bytes(r.content)

            extracted, original_name = self._extract_subtitle(archive_path, output_dir, video_filename, result.language)
            if extracted:
                try:
                    archive_path.unlink()
                except OSError:
                    pass
                result.extra["_original_name"] = original_name
                return extracted
            result.extra["_original_name"] = archive_path.name
            return archive_path
        except Exception:
            return None

    def _extract_subtitle(self, archive_path: Path, output_dir: Path,
                          video_filename: str = "", subtitle_lang: str = "") -> tuple:
        try:
            lang_map = {"zho_chs": "zh", "zho_cht": "zht", "eng": "en",
                        "zho_chs+eng": "zh+en", "zho_cht+eng": "zht+en",
                        "unknown": "zh"}
            lang_code = lang_map.get(subtitle_lang, "zh")

            def _out_name(ext: str) -> str:
                if video_filename:
                    return f"{Path(video_filename).stem}.{lang_code}.opensubtitles{ext}"
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
                    return new_path, archive_path.name
                return archive_path, archive_path.name

            try:
                import rarfile
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
