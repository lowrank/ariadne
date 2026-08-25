from __future__ import annotations

import os
import subprocess
from typing import Any


_LOCAL_CPU_FLOOR = 12


def local_compute_resources() -> dict[str, Any]:
    """Return a small, dependency-free snapshot of locally usable compute."""
    memory_gb = 0.0
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        page_count = int(os.sysconf("SC_PHYS_PAGES"))
        memory_gb = round(page_size * page_count / (1024 ** 3), 2)
    except (AttributeError, OSError, ValueError):
        pass

    gpu_memory_gb = 0.0
    cuda_available = False
    try:
        result = subprocess.run(
            [
                "nvidia-smi", "--query-gpu=memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, timeout=2, check=False,
        )
        if result.returncode == 0:
            memory_mb = [
                float(line.strip()) for line in result.stdout.splitlines()
                if line.strip().replace(".", "", 1).isdigit()
            ]
            gpu_memory_gb = round(sum(memory_mb) / 1024, 2)
            cuda_available = bool(memory_mb)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass

    return {
        "cpu_cores": int(os.cpu_count() or 0),
        "memory_gb": memory_gb,
        "cuda_available": cuda_available,
        "gpu_memory_gb": gpu_memory_gb,
    }


def assess_experiment_resources(
    request: dict[str, Any], profile: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Decide whether a proposed numerical run is local or needs an HPC handoff."""
    profile = dict(profile or local_compute_resources())
    runtime = int(request.get("estimated_runtime_seconds", 0) or 0)
    minimum_cpu = int(request.get("minimum_cpu_cores", 0) or 0)
    minimum_memory = float(request.get("minimum_memory_gb", 0) or 0)
    minimum_gpu_memory = float(request.get("minimum_gpu_memory_gb", 0) or 0)
    requires_cuda = bool(request.get("requires_cuda", False))
    large = (
        str(request.get("scale", "small")).lower() == "large"
        or runtime > 900
        or bool(request.get("requires_human_approval", False))
        or minimum_cpu >= _LOCAL_CPU_FLOOR
        or requires_cuda
    )
    cpu_ready = (
        int(profile.get("cpu_cores", 0) or 0) >= max(_LOCAL_CPU_FLOOR, minimum_cpu)
        and float(profile.get("memory_gb", 0) or 0) >= minimum_memory
    )
    cuda_ready = (
        bool(profile.get("cuda_available", False))
        and float(profile.get("memory_gb", 0) or 0) >= minimum_memory
        and float(profile.get("gpu_memory_gb", 0) or 0) >= minimum_gpu_memory
    )
    local_adequate = cuda_ready if requires_cuda else (cpu_ready or cuda_ready)
    needs_hpc = large and not local_adequate
    reason = ""
    if needs_hpc:
        if requires_cuda:
            reason = (
                "The request requires CUDA with sufficient GPU and host memory; "
                "the detected local profile does not meet that requirement."
            )
        else:
            reason = (
                "The request is large and the detected local profile has neither "
                "at least 12 CPU cores with the requested memory nor adequate CUDA memory."
            )
    return {
        "is_large": large,
        "local_adequate": local_adequate,
        "needs_hpc": needs_hpc,
        "reason": reason,
        "requested": {
            "minimum_cpu_cores": minimum_cpu,
            "minimum_memory_gb": minimum_memory,
            "requires_cuda": requires_cuda,
            "minimum_gpu_memory_gb": minimum_gpu_memory,
            "estimated_runtime_seconds": runtime,
        },
        "local_profile": profile,
    }
