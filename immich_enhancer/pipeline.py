"""The enhancement itself: an upscale and a pixel-doubled copy, upright, EXIF carried over.

`enhance_file(src, dst_enhanced, dst_pixel, settings)` reads a JPEG or PNG and
writes two JPEGs at the same size (plan decision 23):

1. Decode, apply the EXIF orientation, and convert to 8-bit RGB: alpha is
   composited on white; greyscale, palette, CMYK and 16-bit are converted.
2. Choose the scale (plan decision 7). At 1 there is nothing to enhance
   (decision 24): `NothingToEnhanceError`, before any model loads or any file
   is written.
3. `dst_enhanced`: the configured x4 network's upscale, run in tiles, so memory
   depends on the tile, not the image.
4. `dst_pixel`: the same picture enlarged by nearest neighbour, every pixel a
   block of scale x scale. It is the original as it was, only bigger, for
   comparing with the upscale side by side.

Both carry the original's EXIF with Orientation 1 (`exif.py`), at the same
JPEG quality. The network loads on first use and stays loaded for the life of
the process. Nothing is ever downloaded: a missing weight file is an error that
names it.
"""

from __future__ import annotations

import logging
import os
import resource
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import torch
import torch.nn.functional as F
from PIL import Image, ImageOps
from spandrel import ModelLoader

from immich_enhancer.config import Settings
from immich_enhancer.exif import output_exif

log = logging.getLogger(__name__)

MP = 1_000_000
# Context each tile takes from its neighbours, in input pixels, on every side.
OVERLAP = 16
# Pillow's names. MPO is a JPEG with extra frames (some phones, 3D cameras);
# the first frame is the photo.
SUPPORTED_FORMATS = ("JPEG", "MPO", "PNG")
_GREY_MODES = ("1", "L", "LA", "I", "I;16", "I;16B", "I;16L", "I;16N")


class UnsupportedImageError(ValueError):
    """The input is not a format the pipeline handles (JPEG and PNG, plan decision 10)."""


class NothingToEnhanceError(ValueError):
    """The input is held to x1 (over 4 MP, or by the output cap): plan decision 24.

    Both copies would be re-encodes of the original at its own size, so none is
    written."""


@dataclass(frozen=True)
class EnhanceResult:
    scale: int  # 2 or 4; at 1, enhance_file raises NothingToEnhanceError instead
    in_size: tuple[int, int]  # (width, height), upright
    out_size: tuple[int, int]  # (width, height), of both outputs
    seconds: float  # wall time of the call, including any first-use model load


def choose_scale(width: int, height: int, max_output_mp: int) -> int:
    """Plan decision 7: x4 up to 1 MP, x2 up to 4 MP, x1 beyond.

    A scale whose output would pass `max_output_mp` steps down to the next one,
    so the answer is always 4, 2 or 1. 1 means no upscale, which `enhance_file`
    refuses (decision 24).
    """
    pixels = width * height
    band = 4 if pixels <= 1 * MP else 2 if pixels <= 4 * MP else 1
    for scale in (4, 2):
        if scale <= band and pixels * scale * scale <= max_output_mp * MP:
            return scale
    return 1


def enhance_file(
    src: Path, dst_enhanced: Path, dst_pixel: Path, settings: Settings
) -> EnhanceResult:
    """Write the upscale of the JPEG or PNG at `src` to `dst_enhanced`, and its
    pixel-doubled copy to `dst_pixel`. `src` is only read."""
    started = time.monotonic()
    loaded_before = _load_seconds
    src, dst_enhanced, dst_pixel = Path(src), Path(dst_enhanced), Path(dst_pixel)
    if src.resolve() in (dst_enhanced.resolve(), dst_pixel.resolve()):
        raise ValueError(f"refusing to write over the original: {src}")
    if dst_enhanced.resolve() == dst_pixel.resolve():
        raise ValueError(f"the two copies need two files, not one: {dst_enhanced}")

    with _lock, torch.inference_mode():
        torch.set_num_threads(settings.threads)
        picture = _read(src)
        width, height = picture.size
        scale = choose_scale(width, height, settings.max_output_mp)
        if scale == 1:
            raise NothingToEnhanceError(
                f"nothing to enhance: no upscale at this size ({width}x{height})"
            )
        upscaled = _upscale(_sr_model(settings), picture.rgb, scale, settings.tile)
        out_size = _write(Image.fromarray(upscaled), picture, dst_enhanced, settings.jpeg_quality)
        _write(_pixel_copy(picture.rgb, scale), picture, dst_pixel, settings.jpeg_quality)

    result = EnhanceResult(
        scale=scale,
        in_size=(width, height),
        out_size=out_size,
        seconds=time.monotonic() - started,
    )
    log.info(
        "enhanced scale=%d in=%dx%d out=%dx%d seconds=%.2f load_seconds=%.2f "
        "peak_rss_mib=%d sr_model=%s",
        scale,
        width,
        height,
        *out_size,
        result.seconds,
        _load_seconds - loaded_before,
        peak_rss_mib(),
        settings.sr_model,
    )
    return result


def peak_rss_mib() -> int:
    """This process's peak resident set size so far, in MiB."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Bytes on macOS, kibibytes on Linux.
    return peak // 2**20 if sys.platform == "darwin" else peak // 2**10


# -- reading and writing ----------------------------------------------------------


@dataclass(frozen=True)
class _Picture:
    rgb: np.ndarray  # (height, width, 3) uint8, upright
    grey: bool  # the original has no colour, so neither do the outputs
    exif: bytes | None  # the original's raw EXIF block
    icc: bytes | None  # its colour profile, when the pixels stay in its space

    @property
    def size(self) -> tuple[int, int]:
        return self.rgb.shape[1], self.rgb.shape[0]


def _read(src: Path) -> _Picture:
    with Image.open(src) as image:
        if image.format not in SUPPORTED_FORMATS:
            raise UnsupportedImageError(
                f"{src.name} is {image.format}; only JPEG and PNG are enhanced"
            )
        image.load()  # a PNG's eXIf chunk may follow its pixels
        exif = image.info.get("exif")
        # A CMYK profile no longer describes the pixels once they are RGB.
        icc = None if image.mode == "CMYK" else image.info.get("icc_profile")
        grey = image.mode in _GREY_MODES
        upright = ImageOps.exif_transpose(image)
    return _Picture(rgb=_to_rgb(upright), grey=grey, exif=exif, icc=icc)


def _to_rgb(image: Image.Image) -> np.ndarray:
    """8-bit RGB pixels for the networks, which take exactly three channels."""
    if image.mode.startswith("I"):
        # 16-bit greyscale. Pillow's own convert("L") clips at 255 rather than
        # scaling, which turns most of a 16-bit picture white.
        values = np.asarray(image, dtype=np.float64)
        if image.mode != "I" or values.max() > 255:
            values = values / 257
        image = Image.fromarray(np.clip(np.rint(values), 0, 255).astype(np.uint8))
    if image.mode in ("RGBA", "LA", "PA", "RGBa", "La") or "transparency" in image.info:
        # Composite on white: spandrel's networks refuse a fourth channel, and
        # transparent pixels carry arbitrary colour underneath.
        rgba = image.convert("RGBA")
        image = Image.alpha_composite(Image.new("RGBA", rgba.size, (255, 255, 255, 255)), rgba)
    return np.array(image.convert("RGB"), dtype=np.uint8)


def _pixel_copy(rgb: np.ndarray, scale: int) -> Image.Image:
    """`rgb` enlarged `scale` times by nearest neighbour: each pixel becomes a
    `scale` x `scale` block, and nothing is smoothed or invented."""
    image = Image.fromarray(rgb)
    return image.resize((image.width * scale, image.height * scale), Image.Resampling.NEAREST)


def _write(image: Image.Image, picture: _Picture, dst: Path, quality: int) -> tuple[int, int]:
    if picture.grey:
        image = image.convert("L")
    params: dict[str, Any] = {"quality": quality, "optimize": True}
    exif = output_exif(picture.exif, image.size)
    if exif:
        params["exif"] = exif
    if picture.icc:
        params["icc_profile"] = picture.icc
    dst.parent.mkdir(parents=True, exist_ok=True)
    partial = dst.with_name(dst.name + ".part")
    try:
        image.save(partial, "JPEG", **params)
        os.replace(partial, dst)
    finally:
        partial.unlink(missing_ok=True)
    return image.size


# -- super-resolution ---------------------------------------------------------------


@torch.inference_mode()
def _upscale(model: Any, rgb: np.ndarray, out_scale: int, tile: int) -> np.ndarray:
    """`rgb` upscaled by `out_scale` with a x`model.scale` network, tile by tile.

    Each tile is `tile` input pixels square plus OVERLAP pixels of context on
    every side, clipped at the image edge. Only the tile's own region of the
    network's output is kept, so neighbours meet without seams, and memory is
    bounded by the tile size whatever the image size.

    x2 with a x4 network runs the network at x4 and averages each 2x2 block of
    its output (area downsampling). Every tile's x4 region is a whole number of
    2x2 blocks, so doing this per tile gives exactly the whole-image result
    without ever holding the x4 image. Shrinking the input first would be four
    times cheaper, but it throws away the original's own detail before the
    network sees it.
    """
    net = model.scale
    if net % out_scale:
        raise ValueError(f"a x{net} network cannot produce x{out_scale}")
    shrink = net // out_scale
    height, width = rgb.shape[:2]
    out = np.empty((height * out_scale, width * out_scale, 3), dtype=np.uint8)
    pixels = torch.from_numpy(rgb).permute(2, 0, 1).unsqueeze(0)  # a view, uint8
    for y0 in range(0, height, tile):
        y1 = min(y0 + tile, height)
        top, bottom = max(y0 - OVERLAP, 0), min(y1 + OVERLAP, height)
        for x0 in range(0, width, tile):
            x1 = min(x0 + tile, width)
            left, right = max(x0 - OVERLAP, 0), min(x1 + OVERLAP, width)
            patch = pixels[:, :, top:bottom, left:right].float().div_(255)
            result = model(patch)
            oy, ox = (y0 - top) * net, (x0 - left) * net
            core = result[:, :, oy : oy + (y1 - y0) * net, ox : ox + (x1 - x0) * net]
            if shrink > 1:
                core = F.avg_pool2d(core, shrink)
            out[y0 * out_scale : y1 * out_scale, x0 * out_scale : x1 * out_scale] = (
                core[0].permute(1, 2, 0).mul(255).round_().clamp_(0, 255).to(torch.uint8).numpy()
            )
    return out


# -- the model: loaded once per process ---------------------------------------------

_cache: dict[str, Any] = {}
_lock = threading.RLock()
_load_seconds = 0.0  # running total, so each call can log its own share


def _sr_model(settings: Settings) -> Any:
    path = Path(settings.model_dir) / f"{settings.sr_model}.pth"
    return _cached(path, lambda: _load_spandrel(path))


def _cached(key: Path, load: Any) -> Any:
    global _load_seconds
    with _lock:
        model = _cache.get(str(key))
        if model is None:
            started = time.monotonic()
            model = load()
            _cache[str(key)] = model
            elapsed = time.monotonic() - started
            _load_seconds += elapsed
            log.info("loaded %s in %.1f s", key.name, elapsed)
        return model


def _load_spandrel(path: Path) -> Any:
    if not path.is_file():
        raise FileNotFoundError(
            f"model weights missing (they are baked into the image, never downloaded): {path}"
        )
    model = ModelLoader().load_from_file(path)
    if model.purpose != "SR":
        raise ValueError(f"{path.name} is a {model.purpose} model, expected SR")
    return model.eval()
