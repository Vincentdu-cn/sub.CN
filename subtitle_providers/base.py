from dataclasses import dataclass, field
from typing import Optional, List
from pathlib import Path


def _unique_output_path(output_dir: Path, base_name: str) -> Path:
    """Generate a unique output path; append .1, .2 etc. if file exists."""
    out_path = output_dir / base_name
    if not out_path.exists():
        return out_path
    stem = Path(base_name).stem
    suffix = Path(base_name).suffix
    counter = 1
    while True:
        new_name = f"{stem}.{counter}{suffix}"
        out_path = output_dir / new_name
        if not out_path.exists():
            return out_path
        counter += 1


@dataclass
class SubtitleResult:
    title: str
    language: str
    download_url: str = ""
    provider: str = ""
    score: float = 0.0
    page_url: str = ""
    extra: dict = field(default_factory=dict)


class SubtitleProvider:
    name: str = ""

    def search(self, keyword: str, video_info: dict = None,
               season: int = None, episode: int = None) -> List[SubtitleResult]:
        raise NotImplementedError

    def download(self, result: SubtitleResult, output_dir: Path,
                 preferred_lang: str = "zho_chs",
                 video_filename: str = "") -> Optional[Path]:
        raise NotImplementedError
