"""Best-effort, layered GPU VRAM detection for the model selector's
context-length estimate.

There is no single cross-vendor, cross-platform API for "how much VRAM is
on this machine" -- NVIDIA has NVML/nvidia-smi, AMD has rocm-smi on Linux
and nothing reliable on Windows short of a driver-populated registry key,
and WMI's `AdapterRAM` is documented-unreliable (caps out around 4GB on
modern cards). This module tries each layer in order of trustworthiness
and reports which one (if any) succeeded, and NEVER raises -- detection
failure is a normal, expected outcome (headless box, exotic GPU, no
drivers), not an error. The dashboard always shows the result in an
editable field (Requirement: manual override always visible) so a failed
or wrong guess is one click away from being corrected by the user.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_SUBPROCESS_TIMEOUT = 5.0


@dataclass
class VRAMDetectionResult:
    """Outcome of one detect_vram_gb() call."""

    vram_gb: Optional[float]
    source: str  # e.g. "nvidia-smi", "windows_registry", "rocm-smi", "none"
    detected: bool


def _try_nvidia_smi() -> Optional[float]:
    """Sum VRAM (GB) across all NVIDIA GPUs via `nvidia-smi`, if present.

    Works on both Windows and Linux wherever the NVIDIA driver is
    installed -- no extra Python dependency required, unlike pynvml.
    """
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT, check=True,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None
    total_mib = 0.0
    found = False
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            total_mib += float(line)
            found = True
        except ValueError:
            continue
    if not found:
        return None
    return total_mib / 1024.0  # MiB -> GiB


def _try_pynvml() -> Optional[float]:
    """Sum VRAM (GB) across all NVIDIA GPUs via the pynvml bindings, if
    the optional `pynvml` package happens to be installed. Not listed in
    requirements.txt -- this is a bonus path, nvidia-smi covers the same
    ground without the extra dependency."""
    try:
        import pynvml  # type: ignore
    except ImportError:
        return None
    try:
        pynvml.nvmlInit()
        try:
            total_bytes = 0
            for i in range(pynvml.nvmlDeviceGetCount()):
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                total_bytes += pynvml.nvmlDeviceGetMemoryInfo(handle).total
            return total_bytes / (1024.0 ** 3) if total_bytes else None
        finally:
            pynvml.nvmlShutdown()
    except Exception as exc:
        logger.debug("pynvml VRAM detection failed: %s", exc)
        return None


def _try_windows_registry() -> Optional[float]:
    """Sum `HardwareInformation.qwMemorySize` across display adapter
    subkeys in the Windows registry -- the documented-reliable fallback
    for AMD/Intel GPUs on Windows (unlike WMI's AdapterRAM, which is
    known to misreport/cap for any card above ~4GB)."""
    try:
        import winreg
    except ImportError:
        return None  # Not on Windows

    base_path = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    total_bytes = 0
    found = False
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base_path) as base_key:
            index = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(base_key, index)
                except OSError:
                    break
                index += 1
                if not re.fullmatch(r"\d{4}", subkey_name):
                    continue
                try:
                    with winreg.OpenKey(base_key, subkey_name) as adapter_key:
                        value, _ = winreg.QueryValueEx(
                            adapter_key, "HardwareInformation.qwMemorySize"
                        )
                        if isinstance(value, int) and value > 0:
                            total_bytes += value
                            found = True
                except OSError:
                    continue
    except OSError as exc:
        logger.debug("Windows registry VRAM detection failed: %s", exc)
        return None

    if not found:
        return None
    return total_bytes / (1024.0 ** 3)


def _try_rocm_smi() -> Optional[float]:
    """Sum VRAM (GB) across AMD GPUs via `rocm-smi` on Linux, if present."""
    try:
        result = subprocess.run(
            ["rocm-smi", "--showmeminfo", "vram", "--json"],
            capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT, check=True,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None
    try:
        import json
        data = json.loads(result.stdout)
    except (ValueError, TypeError):
        return None
    total_bytes = 0
    found = False
    for card in data.values():
        if not isinstance(card, dict):
            continue
        for key, value in card.items():
            if "total" in key.lower() and "vram" in key.lower():
                try:
                    total_bytes += int(value)
                    found = True
                except (TypeError, ValueError):
                    continue
    if not found:
        return None
    return total_bytes / (1024.0 ** 3)


# Below this, a "detected" figure is more likely an integrated GPU's small
# BIOS-carved-out shared-memory allocation (typically 0.5-2GB) than a real
# usable VRAM budget worth proposing a context length from -- especially
# from the Windows registry path, which reports whatever the driver
# advertises for ANY adapter, discrete or integrated. Treated as "not
# detected" so the UI falls through to the manual-override prompt instead
# of confidently showing a misleading number.
_MIN_PLAUSIBLE_VRAM_GB = 2.0

# Ordered by trustworthiness: NVIDIA's own tooling first (most accurate),
# then the Windows registry fallback for AMD/Intel, then rocm-smi for
# Linux AMD. Each is independently best-effort and never raises.
_DETECTORS = (
    ("nvidia-smi", _try_nvidia_smi),
    ("pynvml", _try_pynvml),
    ("windows_registry", _try_windows_registry),
    ("rocm-smi", _try_rocm_smi),
)


def detect_vram_gb() -> VRAMDetectionResult:
    """Best-effort total VRAM across all GPUs, trying each detector in
    order and returning the first one that finds something plausible.

    Never raises. Returns `detected=False, vram_gb=None, source="none"`
    when every layer comes up empty or implausibly low -- e.g. an
    integrated GPU's small shared-memory carve-out (headless machine,
    unsupported GPU, no drivers, iGPU-only) -- the caller (the model
    selector UI) always shows this result in an editable field so the
    user can fill it in by hand.
    """
    for source, detector in _DETECTORS:
        try:
            vram_gb = detector()
        except Exception as exc:  # pragma: no cover - defense in depth
            logger.warning("VRAM detector %s raised unexpectedly: %s", source, exc)
            continue
        if vram_gb and vram_gb >= _MIN_PLAUSIBLE_VRAM_GB:
            return VRAMDetectionResult(vram_gb=round(vram_gb, 1), source=source, detected=True)
        if vram_gb:
            logger.info(
                "VRAM detector %s found %.2fGB, below plausibility floor "
                "(%.1fGB) -- likely an iGPU shared-memory carve-out, "
                "treating as undetected.", source, vram_gb, _MIN_PLAUSIBLE_VRAM_GB,
            )
    return VRAMDetectionResult(vram_gb=None, source="none", detected=False)


# ---------------------------------------------------------------------------
# Context-length estimation (pure-HTTP "propose and reconcile" approach --
# no dependency on the `lms` CLI. This is a deliberately rough heuristic:
# the caller sends the proposed context_length to LM Studio's load
# endpoint and displays whatever LM Studio actually applied, which is the
# authoritative number -- this function only picks a reasonable starting
# guess to propose.
# ---------------------------------------------------------------------------

# Coarse bytes-per-token KV-cache footprint by rough parameter-count tier,
# assuming fp16 KV cache on a modern GQA (grouped-query attention)
# transformer -- most local models people run today (Llama-3/3.1, Mistral,
# Qwen2+, Gemma2+) use GQA, which cuts KV cache dramatically vs. classic
# multi-head attention. Values are order-of-magnitude industry rules of
# thumb (KB/token, not bytes/token -- e.g. an 8B GQA model runs roughly
# ~100-150KB per token of KV cache at fp16), deliberately approximate
# since real footprint depends on exact architecture/head count/quant --
# only used to pick a starting proposal to send to LM Studio, never
# trusted as exact. See propose_context_length()'s docstring: the
# caller MUST treat LM Studio's echoed-back applied config as the real
# answer, not this guess.
_KV_BYTES_PER_TOKEN_BY_TIER = (
    (3, 100_000),    # <=3B params (~100KB/token -- GQA head count doesn't
                      # shrink with model size, so small models aren't
                      # proportionally cheaper; e.g. Llama-3.2-3B ~112KB/tok)
    (8, 130_000),    # <=8B params  (~130KB/token)
    (14, 180_000),   # <=14B params (~180KB/token)
    (34, 280_000),   # <=34B params (~280KB/token)
    (70, 400_000),   # <=70B params (~400KB/token)
)
_DEFAULT_KV_BYTES_PER_TOKEN = 600_000  # unknown/larger models: conservative

# VRAM reserved for the model weights themselves + OS/driver overhead
# before whatever's left gets divided among KV cache. Rough only.
_OVERHEAD_FRACTION = 0.35
_USABLE_VRAM_FRACTION = 0.8  # never propose using 100% of detected VRAM

_MODEL_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*b\b", re.IGNORECASE)

MIN_PROPOSED_CONTEXT = 2048
MAX_PROPOSED_CONTEXT = 131072


def _kv_bytes_per_token(model_id: str) -> int:
    match = _MODEL_SIZE_RE.search(model_id or "")
    if not match:
        return _DEFAULT_KV_BYTES_PER_TOKEN
    try:
        params_b = float(match.group(1))
    except ValueError:
        return _DEFAULT_KV_BYTES_PER_TOKEN
    for tier_max, bytes_per_token in _KV_BYTES_PER_TOKEN_BY_TIER:
        if params_b <= tier_max:
            return bytes_per_token
    return _DEFAULT_KV_BYTES_PER_TOKEN


def propose_context_length(
    vram_gb: float, model_id: str = "", max_context_length: Optional[int] = None,
) -> int:
    """Propose a context length to try loading a model with, given a VRAM
    budget in GB. Rough heuristic only -- the caller MUST send this to LM
    Studio's load endpoint and treat the echoed-back applied config as the
    real answer, never this number by itself.

    Args:
        vram_gb: Total VRAM budget in GB (from detect_vram_gb() or the
            user's manual override).
        model_id: Model identifier, used to guess parameter count from a
            "7b"/"20b"/etc. substring for a per-size KV-cache estimate.
        max_context_length: The model's own reported ceiling, if known
            (from LM Studio's /api/v1/models listing) -- the proposal is
            never allowed to exceed it.

    Returns:
        A proposed context length in tokens, clamped to
        [MIN_PROPOSED_CONTEXT, MAX_PROPOSED_CONTEXT] and to
        max_context_length if provided.
    """
    if vram_gb <= 0:
        return MIN_PROPOSED_CONTEXT
    usable_bytes = vram_gb * 1e9 * _USABLE_VRAM_FRACTION * (1 - _OVERHEAD_FRACTION)
    bytes_per_token = _kv_bytes_per_token(model_id)
    proposed = int(usable_bytes / bytes_per_token) if bytes_per_token else MIN_PROPOSED_CONTEXT
    proposed = max(MIN_PROPOSED_CONTEXT, min(proposed, MAX_PROPOSED_CONTEXT))
    if max_context_length and max_context_length > 0:
        proposed = min(proposed, max_context_length)
    return proposed
