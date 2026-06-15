import io
import os
import time
import zipfile
from pathlib import Path
from typing import Optional, List

import requests

from .base import SubtitleResult, SubtitleProvider, _unique_output_path
from .utils import SUBTITLE_EXTENSIONS, _extract_best_from_zip, _extract_best_from_rar


class AssrtProvider(SubtitleProvider):
    name = "assrt"

    API_BASE = "https://api.assrt.net/v1"
    API_FALLBACK = "https://api.makedie.me/v1"

    def __init__(self, api_token: str = ""):
        self.api_token = api_token or os.environ.get("ASSRT_API_TOKEN", "")
        self.session = requests.Session()
        if self.api_token:
            self.session.headers["Authorization"] = f"Bearer {self.api_token}"

    def _api_get(self, endpoint: str, params: dict = None) -> Optional[dict]:
        for base in [self.API_BASE, self.API_FALLBACK]:
            try:
                r = self.session.get(
                    f"{base}{endpoint}",
                    params=params, timeout=20,
                )
                r.raise_for_status()
                return r.json()
            except Exception:
                continue
        return None

    def search(self, keyword: str, video_info: dict = None,
               season: int = None, episode: int = None) -> List[SubtitleResult]:
        if not self.api_token:
            return []

        try:
            params = {"q": keyword, "is_file": 1}
            if video_info:
                if video_info.get("title"):
                    params["q"] = str(video_info["title"])
                if video_info.get("year"):
                    params["q"] += f" {video_info['year']}"
                vi_season = video_info.get("season")
                vi_episode = video_info.get("episode")
                if vi_season is not None:
                    try:
                        s = int(vi_season)
                        if vi_episode is not None:
                            e = int(vi_episode)
                            params["q"] += f" S{s:02d}E{e:02d}"
                        else:
                            params["q"] += f" S{s:02d}"
                    except (ValueError, TypeError):
                        pass

            data = self._api_get("/sub/search", params)
            if not data or data.get("status") != 0:
                return []

            subs = data.get("sub", {}).get("subs", [])
            results = []
            for sub in subs:
                lang = "zho_chs"
                lang_desc = sub.get("lang", {}).get("desc", "").lower() if isinstance(sub.get("lang"), dict) else str(sub.get("lang", "")).lower()
                if any(kw in lang_desc for kw in ("双语", "中英", "简英", "简繁")):
                    lang = "zho_chs+eng"
                elif any(kw in lang_desc for kw in ("简", "chs", "gb")):
                    lang = "zho_chs"
                elif any(kw in lang_desc for kw in ("繁", "cht", "big5")):
                    lang = "zho_cht"
                elif any(kw in lang_desc for kw in ("英", "eng", "english")):
                    lang = "eng"

                sub_id = sub.get("id", "")
                results.append(SubtitleResult(
                    title=sub.get("native_name", "") or sub.get("videoname", ""),
                    language=lang,
                    download_url="",
                    provider=self.name,
                    score=0.0,
                    page_url=f"https://assrt.net/sub/{sub_id}" if sub_id else "",
                    extra={"sub_id": sub_id},
                ))

            return results[:30]
        except Exception:
            return []

    def download(self, result: SubtitleResult, output_dir: Path,
                 preferred_lang: str = "zho_chs",
                 video_filename: str = "") -> Optional[Path]:
        if not self.api_token:
            return None

        try:
            sub_id = result.extra.get("sub_id")
            if not sub_id:
                return None

            data = self._api_get("/sub/detail", {"id": sub_id})
            if not data or data.get("status") != 0:
                return None

            subs_list = data.get("sub", {}).get("subs", [])
            sub_data = subs_list[0] if subs_list else {}
            file_list = sub_data.get("filelist", [])

            download_url = None
            if file_list:
                best_file = file_list[0]
                download_url = best_file.get("url", "")

            if not download_url:
                download_url = sub_data.get("url", "")

            if not download_url:
                return None

            if not download_url.startswith("http"):
                download_url = f"https://assrt.net{download_url}"

            r = self.session.get(download_url, timeout=30)
            if r.status_code != 200 or not r.content:
                return None

            output_dir.mkdir(parents=True, exist_ok=True)

            disposition = r.headers.get("Content-Disposition", "")
            if "filename=" in disposition:
                fname = disposition.split("filename=")[-1].strip('"\'; ')
            else:
                fname = f"assrt_{sub_id}_{int(time.time())}.zip"

            archive_path = output_dir / fname
            archive_path.write_bytes(r.content)

            extracted, original_name = self._extract_subtitle(archive_path, output_dir, video_filename, result.language or preferred_lang)
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
                    return f"{Path(video_filename).stem}.{lang_code}.assrt{ext}"
                return archive_path.stem + ext

            data = archive_path.read_bytes()

            import rarfile
            _ensure_rarfile()
            if rarfile.is_rarfile(io.BytesIO(data)):
                content, best_name = _extract_best_from_rar(data, archive_path.name)
                if content:
                    ext = Path(best_name).suffix if best_name else ".srt"
                    out_path = _unique_output_path(output_dir, _out_name(ext))
                    out_path.write_bytes(content)
                    return out_path, best_name or out_path.name

            if zipfile.is_zipfile(io.BytesIO(data)):
                content, best_name = _extract_best_from_zip(data, archive_path.name)
                if content:
                    ext = Path(best_name).suffix if best_name else ".srt"
                    out_path = _unique_output_path(output_dir, _out_name(ext))
                    out_path.write_bytes(content)
                    return out_path, best_name or out_path.name

            if any(archive_path.name.lower().endswith(ext) for ext in SUBTITLE_EXTENSIONS):
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
        except Exception:
            return None, ""
