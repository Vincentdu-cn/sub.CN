import json
import logging
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class ProgressTracker:
    def __init__(self, output_path: Path):
        self._path = Path(str(output_path) + ".progress.json")
        self._lock = threading.Lock()

    def save(self, completed_indices: list[int], translations_by_batch: dict[int, list[str]]):
        with self._lock:
            try:
                data = {
                    "completed_batches": sorted(completed_indices),
                    "translations_by_batch": {str(k): v for k, v in translations_by_batch.items()},
                }
                self._path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            except OSError as e:
                logger.warning("Failed to save progress: %s", e)

    def load(self) -> Optional[tuple[set[int], dict[int, list[str]]]]:
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if "completed_batches" in data and "translations_by_batch" in data:
                completed = set(data["completed_batches"])
                by_batch = {int(k): v for k, v in data["translations_by_batch"].items()}
                return (completed, by_batch)
            if "last_batch" in data and "translations" in data:
                last_batch = data["last_batch"]
                translations = data["translations"]
                by_batch = {last_batch: translations}
                completed = {last_batch}
                return (completed, by_batch)
            return None
        except (json.JSONDecodeError, OSError, KeyError) as e:
            logger.warning("Corrupt progress file, starting fresh: %s", e)
            try:
                self._path.unlink()
            except OSError:
                pass
            return None

    def clear(self):
        try:
            if self._path.exists():
                self._path.unlink()
        except OSError:
            pass
