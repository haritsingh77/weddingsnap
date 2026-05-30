"""
Runtime metrics: speed, memory, ETA, optional GPU utilization.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def _memory_mb() -> Optional[float]:
    try:
        import psutil
        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:
        return None


def _gpu_utilization() -> Optional[float]:
    try:
        import subprocess
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0:
            return float(r.stdout.strip().split("\n")[0])
    except Exception:
        pass
    return None


@dataclass
class BatchMetrics:
    batch_index: int = 0
    batch_size: int = 0
    files_processed: int = 0
    faces_found: int = 0
    sec_per_file: float = 0.0
    batch_elapsed_sec: float = 0.0
    memory_mb: Optional[float] = None
    gpu_util_pct: Optional[float] = None


@dataclass
class RunMetrics:
    start_time: float = field(default_factory=time.time)
    total_files: int = 0
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    success: int = 0
    faces_found: int = 0
    current_file: str = ""
    model: str = ""
    backend: str = ""
    queue_size: int = 0
    batch_history: list = field(default_factory=list)

    def elapsed_sec(self) -> float:
        return time.time() - self.start_time

    def sec_per_file(self) -> Optional[float]:
        done = self.success + self.failed
        if done <= 0:
            return None
        return self.elapsed_sec() / done

    def eta_seconds(self) -> Optional[int]:
        speed = self.sec_per_file()
        if not speed or speed <= 0:
            return None
        remaining = max(0, self.total_files - (self.skipped + self.success + self.failed))
        return int(remaining * speed)

    def record_batch(self, batch_size: int, batch_elapsed: float, faces_in_batch: int) -> None:
        done = max(1, batch_size)
        bm = BatchMetrics(
            batch_index=len(self.batch_history) + 1,
            batch_size=batch_size,
            files_processed=self.success + self.failed,
            faces_found=faces_in_batch,
            sec_per_file=round(batch_elapsed / done, 2),
            batch_elapsed_sec=round(batch_elapsed, 2),
            memory_mb=_memory_mb(),
            gpu_util_pct=_gpu_utilization(),
        )
        self.batch_history.append(bm)
        log.info(
            "Batch #%d: %d files in %.1fs (%.2fs/file) | RAM: %s MB | GPU: %s%% | queue: %d",
            bm.batch_index,
            batch_size,
            batch_elapsed,
            bm.sec_per_file,
            f"{bm.memory_mb:.0f}" if bm.memory_mb else "?",
            f"{bm.gpu_util_pct:.0f}" if bm.gpu_util_pct is not None else "?",
            self.queue_size,
        )

    def to_state_dict(self) -> dict:
        speed = self.sec_per_file()
        return {
            "status": "running",
            "model": self.model,
            "backend": self.backend,
            "total_files": self.total_files,
            "processed_files_all_time": self.skipped + self.success + self.failed,
            "processed_this_run": self.success + self.failed,
            "skipped_this_run": self.skipped,
            "success_this_run": self.success,
            "failed_this_run": self.failed,
            "faces_found": self.faces_found,
            "current_file_name": self.current_file,
            "speed_seconds_per_file": round(speed, 2) if speed else None,
            "elapsed_seconds": int(self.elapsed_sec()),
            "estimated_remaining_seconds": self.eta_seconds(),
            "memory_mb": _memory_mb(),
            "gpu_utilization_pct": _gpu_utilization(),
            "queue_size": self.queue_size,
            "last_update": time.time(),
        }

    def write_state(self, output_folder: Path) -> None:
        try:
            path = output_folder / "preprocessor_state.json"
            path.write_text(
                json.dumps(self.to_state_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            log.warning("Failed to write preprocessor state: %s", e)
