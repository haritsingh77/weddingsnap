"""
GPU / CUDA detection and startup logging.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class DeviceInfo:
    cuda_available: bool = False
    gpu_name: Optional[str] = None
    vram_mb: Optional[int] = None
    cuda_version: Optional[str] = None
    inference_device: str = "cpu"
    onnx_providers: list = None
    tensorflow_gpu: bool = False
    pytorch_cuda: bool = False
    insightface_gpu: bool = False
    dlib_cuda: bool = False
    notes: list = None

    def __post_init__(self):
        if self.onnx_providers is None:
            self.onnx_providers = []
        if self.notes is None:
            self.notes = []


def _probe_nvidia_smi() -> tuple[Optional[str], Optional[int], Optional[str]]:
    try:
        import subprocess
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = [p.strip() for p in result.stdout.strip().split("\n")[0].split(",")]
            name = parts[0] if len(parts) > 0 else None
            vram = int(float(parts[1])) if len(parts) > 1 else None
            driver = parts[2] if len(parts) > 2 else None
            return name, vram, driver
    except Exception as e:
        log.debug("nvidia-smi probe failed: %s", e)
    return None, None, None


def _probe_onnx_providers() -> list[str]:
    try:
        import onnxruntime as ort
        return list(ort.get_available_providers())
    except Exception:
        return []


def _inject_windows_cuda_paths() -> list[str]:
    """
    Make CUDA/cuDNN DLL paths discoverable for ONNX Runtime on Windows.
    Supports both system CUDA installs and pip-installed NVIDIA runtime wheels.
    """
    if os.name != "nt":
        return []

    added: list[str] = []
    candidates: list[Path] = []

    # Common system install locations.
    cuda_home = os.getenv("CUDA_PATH")
    if cuda_home:
        candidates.extend(
            [
                Path(cuda_home) / "bin",
                Path(cuda_home) / "libnvvp",
            ]
        )
    candidates.extend(
        [
            Path(r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.6\bin"),
            Path(r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.4\bin"),
            Path(r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v11.8\bin"),
        ]
    )

    # pip-installed runtime wheels (nvidia-* packages).
    for site in sys.path:
        site_path = Path(site)
        if not site_path.exists():
            continue
        candidates.extend(
            [
                site_path / "nvidia" / "cublas" / "bin",
                site_path / "nvidia" / "cudnn" / "bin",
                site_path / "nvidia" / "cuda_runtime" / "bin",
                site_path / "nvidia" / "cuda_nvrtc" / "bin",
                site_path / "nvidia" / "cufft" / "bin",
                site_path / "nvidia" / "curand" / "bin",
                site_path / "nvidia" / "cusolver" / "bin",
                site_path / "nvidia" / "cusparse" / "bin",
            ]
        )

    existing = set()
    for p in os.environ.get("PATH", "").split(";"):
        p = p.strip()
        if p:
            existing.add(p.lower())

    for cand in candidates:
        if cand.exists() and str(cand).lower() not in existing:
            os.environ["PATH"] = f"{cand};{os.environ.get('PATH', '')}"
            if hasattr(os, "add_dll_directory"):
                try:
                    os.add_dll_directory(str(cand))
                except Exception as e:
                    log.debug("Failed to add DLL directory %s: %s", cand, e)
            added.append(str(cand))
            existing.add(str(cand).lower())

    return added


def _probe_onnx_cuda_runtime() -> tuple[bool, Optional[str]]:
    """
    Validate CUDA provider can actually load (not just appear in provider list).
    Returns (usable, error_message).
    """
    try:
        import onnxruntime as ort
        providers = ort.get_available_providers()
        if "CUDAExecutionProvider" not in providers:
            return False, "CUDAExecutionProvider not present"
        # Quick runtime-level check.
        try:
            # This can raise if CUDA DLL dependencies are missing.
            _ = ort.get_device()
            return True, None
        except Exception as e:
            return False, str(e)
    except Exception as e:
        return False, str(e)


def _probe_dlib_cuda() -> bool:
    try:
        import dlib
        return bool(getattr(dlib, "DLIB_USE_CUDA", False))
    except Exception:
        return False


def get_device_info() -> DeviceInfo:
    info = DeviceInfo()
    added_cuda_paths = _inject_windows_cuda_paths()
    if added_cuda_paths:
        info.notes.append(
            f"Added CUDA DLL paths: {len(added_cuda_paths)}"
        )
    info.onnx_providers = _probe_onnx_providers()
    info.dlib_cuda = _probe_dlib_cuda()

    gpu_name, vram, _driver = _probe_nvidia_smi()
    if gpu_name:
        info.gpu_name = gpu_name
        info.vram_mb = vram

    has_cuda_provider = any(
        "CUDA" in p.upper() for p in info.onnx_providers
    )
    cuda_runtime_ok, cuda_runtime_err = _probe_onnx_cuda_runtime()
    info.cuda_available = (has_cuda_provider and cuda_runtime_ok) or info.dlib_cuda

    if has_cuda_provider and cuda_runtime_ok:
        info.inference_device = "cuda"
        info.insightface_gpu = True
    elif info.dlib_cuda:
        info.inference_device = "cuda (dlib)"
    else:
        info.inference_device = "cpu"

    try:
        import torch
        info.pytorch_cuda = torch.cuda.is_available()
        if info.pytorch_cuda and not info.gpu_name:
            info.gpu_name = torch.cuda.get_device_name(0)
            info.vram_mb = torch.cuda.get_device_properties(0).total_memory // (1024 * 1024)
    except Exception:
        pass

    try:
        import tensorflow as tf
        gpus = tf.config.list_physical_devices("GPU")
        info.tensorflow_gpu = len(gpus) > 0
    except Exception:
        pass

    if not info.onnx_providers:
        info.notes.append(
            "onnxruntime not installed — install onnxruntime-gpu for InsightFace GPU"
        )
    elif not has_cuda_provider and gpu_name:
        info.notes.append(
            "NVIDIA GPU detected but ONNX CUDA provider missing — pip install onnxruntime-gpu"
        )
    elif has_cuda_provider and not cuda_runtime_ok:
        info.notes.append(
            f"CUDA provider listed but not loadable at runtime: {cuda_runtime_err}"
        )

    return info


def log_device_info(logger: Optional[logging.Logger] = None) -> DeviceInfo:
    """Print GPU availability on startup. Does not raise if CUDA missing."""
    lg = logger or log
    info = get_device_info()

    lg.info("=" * 50)
    lg.info("WeddingSnap Face Engine — Device Report")
    lg.info("  CUDA available      : %s", info.cuda_available)
    lg.info("  GPU name            : %s", info.gpu_name or "N/A")
    lg.info("  VRAM (MB)           : %s", info.vram_mb or "N/A")
    lg.info("  Inference device    : %s", info.inference_device)
    lg.info("  ONNX providers      : %s", info.onnx_providers or ["none"])
    lg.info("  InsightFace GPU     : %s", info.insightface_gpu)
    lg.info("  dlib CUDA build     : %s", info.dlib_cuda)
    lg.info("  PyTorch CUDA        : %s", info.pytorch_cuda)
    lg.info("  TensorFlow GPU      : %s", info.tensorflow_gpu)
    for note in info.notes:
        lg.warning("  Note: %s", note)
    lg.info("=" * 50)

    return info


def configure_onnx_memory(fraction: float = 0.85) -> None:
    """Limit ONNX GPU memory for 4GB cards."""
    os.environ.setdefault("ORT_CUDA_MEMORY_LIMIT", str(int(3500 * fraction)))
