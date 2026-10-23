"""The enhancement itself: upright, upscaled, faces restored, EXIF carried over.

`enhance_file(src, dst, settings)` reads a JPEG or PNG and writes a JPEG:

1. Decode, apply the EXIF orientation, and convert to 8-bit RGB: alpha is
   composited on white; greyscale, palette, CMYK and 16-bit are converted.
2. Choose the scale (plan decision 7) and, unless it is 1, upscale with the
   configured x4 network, in tiles, so memory depends on the tile, not the image.
3. Find, align and restore faces: facexlib's RetinaFace and alignment, GFPGAN
   v1.4 on each 512x512 face, blended back through facexlib's parsing mask.
4. Write a JPEG carrying the original's EXIF with Orientation 1 (`exif.py`).

The networks load on first use and stay loaded for the life of the process.
Nothing is ever downloaded: a missing weight file is an error that names it.
"""

from __future__ import annotations

import logging
import math
import os
import resource
import sys
import threading
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import torch
import torch.nn.functional as F
from facexlib.utils.face_restoration_helper import FaceRestoreHelper
from PIL import Image, ImageOps
from spandrel import ModelLoader

from immich_enhancer.config import Settings
from immich_enhancer.exif import output_exif

log = logging.getLogger(__name__)

MP = 1_000_000
# Context each tile takes from its neighbours, in input pixels, on every side.
OVERLAP = 16
# RetinaFace's working memory grows by about 0.3 GB per megapixel on Linux
# (0.6 GB/MP on macOS), so a 20 MP photo would need 6 GB. Larger inputs are
# detected on a copy shrunk to this size; the landmarks are scaled back.
DETECT_MAX_PIXELS = 2 * MP
# GFPGAN's own value: skip faces whose eyes are under 5 px apart (side-on or tiny).
EYE_DIST_THRESHOLD = 5
FACE_SIZE = 512
GFPGAN_WEIGHTS = "GFPGANv1.4.pth"
FACEXLIB_WEIGHTS = ("detection_Resnet50_Final.pth", "parsing_parsenet.pth")
# Pillow's names. MPO is a JPEG with extra frames (some phones, 3D cameras);
# the first frame is the photo.
SUPPORTED_FORMATS = ("JPEG", "MPO", "PNG")
_GREY_MODES = ("1", "L", "LA", "I", "I;16", "I;16B", "I;16L", "I;16N")
# facexlib's MASK_COLORMAP: which of ParseNet's 19 classes the restored face
# replaces (255) and which keep the background (0).
_PARSE_MASK = np.array(
    [0, 255, 255, 255, 255, 255, 255, 255, 255, 255, 255, 255, 255, 255, 0, 255, 0, 0, 0],
    dtype=np.float64,
)


class UnsupportedImageError(ValueError):
    """The input is not a format the pipeline handles (JPEG and PNG, plan decision 10)."""


@dataclass(frozen=True)
class EnhanceResult:
    scale: int  # 1, 2 or 4
    faces: int  # faces restored
    in_size: tuple[int, int]  # (width, height), upright
    out_size: tuple[int, int]  # (width, height)
    seconds: float  # wall time of the call, including any first-use model load


def choose_scale(width: int, height: int, max_output_mp: int) -> int:
    """Plan decision 7: x4 up to 1 MP, x2 up to 4 MP, x1 (faces only) beyond.

    A scale whose output would pass `max_output_mp` steps down to the next one,
    so the answer is always 4, 2 or 1. x1 is allowed past the cap: the output is
    never smaller than the original.
    """
    pixels = width * height
    band = 4 if pixels <= 1 * MP else 2 if pixels <= 4 * MP else 1
    for scale in (4, 2):
        if scale <= band and pixels * scale * scale <= max_output_mp * MP:
            return scale
    return 1


def enhance_file(src: Path, dst: Path, settings: Settings) -> EnhanceResult:
    """Enhance the JPEG or PNG at `src` into a JPEG at `dst`. `src` is only read."""
    started = time.monotonic()
    loaded_before = _load_seconds
    src, dst = Path(src), Path(dst)
    if src.resolve() == dst.resolve():
        raise ValueError(f"refusing to write over the original: {src}")

    with _lock, torch.inference_mode():
        torch.set_num_threads(settings.threads)
        cv2.setNumThreads(settings.threads)
        picture = _read(src)
        width, height = picture.size
        scale = choose_scale(width, height, settings.max_output_mp)
        if scale > 1:
            out = _upscale(_sr_model(settings), picture.rgb, scale, settings.tile)
        else:
            out = picture.rgb  # our own copy: the face pass may draw on it
        faces = _restore_faces(settings, picture.rgb, out, scale) if settings.face_restore else 0
        out_size = _write(out, picture, dst, settings.jpeg_quality)

    result = EnhanceResult(
        scale=scale,
        faces=faces,
        in_size=(width, height),
        out_size=out_size,
        seconds=time.monotonic() - started,
    )
    log.info(
        "enhanced scale=%d faces=%d in=%dx%d out=%dx%d seconds=%.2f load_seconds=%.2f "
        "peak_rss_mib=%d sr_model=%s",
        scale,
        faces,
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
    rgb: np.ndarray  # (height, width, 3) uint8, upright, writable
    grey: bool  # the original has no colour, so neither does the output
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


def _write(rgb: np.ndarray, picture: _Picture, dst: Path, quality: int) -> tuple[int, int]:
    image = Image.fromarray(rgb)
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


# -- faces ------------------------------------------------------------------------------


def _restore_faces(settings: Settings, rgb_in: np.ndarray, rgb_out: np.ndarray, scale: int) -> int:
    """Restore every face found in `rgb_in` into `rgb_out` (in place); the count.

    Faces are found and aligned on the input, as GFPGAN's own pipeline does, and
    land in the output at `scale`. facexlib works in BGR; `rgb_out` is drawn on
    through a channel-reversed view, so no copy of the output is made.
    """
    helper = _face_helper(settings)
    helper.clean_all()
    helper.set_upscale_factor(scale)
    bgr_in = np.ascontiguousarray(rgb_in[:, :, ::-1])
    helper.read_image(bgr_in)
    height, width = bgr_in.shape[:2]
    shrink_to = None  # facexlib's `resize`: the detection copy's shorter side
    if height * width > DETECT_MAX_PIXELS:
        shrink_to = int(min(height, width) * math.sqrt(DETECT_MAX_PIXELS / (height * width)))
    found = helper.get_face_landmarks_5(resize=shrink_to, eye_dist_threshold=EYE_DIST_THRESHOLD)
    if not found:
        return 0
    helper.align_warp_face()
    helper.get_inverse_affine()
    gfpgan = _gfpgan(settings)
    bgr_out = rgb_out[:, :, ::-1]
    for crop, inverse_affine in zip(
        helper.cropped_faces, helper.inverse_affine_matrices, strict=True
    ):
        restored = _restore_face(gfpgan, crop)
        _paste_face(helper.face_parse, bgr_out, restored, inverse_affine, scale)
    return len(helper.cropped_faces)


@torch.inference_mode()
def _restore_face(gfpgan: Any, crop: np.ndarray) -> np.ndarray:
    """GFPGAN on one aligned 512x512 BGR face; the restored face, BGR uint8.

    GFPGAN works in [-1, 1]. spandrel's descriptor call feeds it [0, 1] and clamps
    the result to [0, 1], which crushes the shadows and shifts the colours, so
    the network is called directly, as GFPGAN's own inference does. The stored
    noise (randomize_noise=False) makes the same face come out the same every
    time, so a re-run produces identical bytes.
    """
    rgb = np.ascontiguousarray(crop[:, :, ::-1])
    x = torch.from_numpy(rgb).permute(2, 0, 1).unsqueeze(0).float().div_(255)
    y = gfpgan.model(x * 2 - 1, return_rgb=False, randomize_noise=False)[0]
    y = (y.clamp_(-1, 1) + 1) / 2
    out = y[0].permute(1, 2, 0).mul_(255).round_().to(torch.uint8).numpy()
    return np.ascontiguousarray(out[:, :, ::-1])


def _paste_face(
    parse_net: Any,
    bgr_out: np.ndarray,
    restored: np.ndarray,
    inverse_affine: np.ndarray,
    scale: int,
) -> None:
    """Blend one restored face into `bgr_out` in place, through its parsing mask.

    This is facexlib's `paste_faces_to_input_image` with `use_parse` (the same
    offset, warp, mask and blend), done inside the rectangle the face lands in.
    facexlib's own builds several float64 copies of the whole output, over a
    gigabyte at 16 MP; this never touches more than the face.
    """
    out_h, out_w = bgr_out.shape[:2]
    affine = np.array(inverse_affine, dtype=np.float64)
    if scale > 1:
        affine[:, 2] += 0.5 * scale  # facexlib's offset "for more precise back alignment"
    corners = (
        np.array(
            [[0, 0, 1], [FACE_SIZE, 0, 1], [0, FACE_SIZE, 1], [FACE_SIZE, FACE_SIZE, 1]],
            dtype=np.float64,
        )
        @ affine.T
    )
    x0 = max(math.floor(corners[:, 0].min()) - 2, 0)
    y0 = max(math.floor(corners[:, 1].min()) - 2, 0)
    x1 = min(math.ceil(corners[:, 0].max()) + 2, out_w)
    y1 = min(math.ceil(corners[:, 1].max()) + 2, out_h)
    if x1 <= x0 or y1 <= y0:
        return
    affine[:, 2] -= (x0, y0)
    size = (x1 - x0, y1 - y0)
    face = cv2.warpAffine(restored, affine, size)
    mask = cv2.warpAffine(_parse_mask(parse_net, restored), affine, size, flags=3)[:, :, None]
    region = bgr_out[y0:y1, x0:x1]
    bgr_out[y0:y1, x0:x1] = (mask * face + (1 - mask) * region).astype(np.uint8)


@torch.inference_mode()
def _parse_mask(parse_net: Any, restored: np.ndarray) -> np.ndarray:
    """facexlib's blend weights for one restored face, 512x512 float64 in [0, 1]."""
    rgb = np.ascontiguousarray(restored[:, :, ::-1])
    x = torch.from_numpy(rgb).permute(2, 0, 1).unsqueeze(0).float().div_(255)
    labels = parse_net((x - 0.5) / 0.5)[0].argmax(dim=1).squeeze().numpy()
    mask = _PARSE_MASK[labels]
    mask = cv2.GaussianBlur(mask, (101, 101), 11)
    mask = cv2.GaussianBlur(mask, (101, 101), 11)
    edge = 10
    mask[:edge, :] = 0
    mask[-edge:, :] = 0
    mask[:, :edge] = 0
    mask[:, -edge:] = 0
    return mask / 255.0


# -- models: loaded once per process ----------------------------------------------------

_cache: dict[str, Any] = {}
_lock = threading.RLock()
_load_seconds = 0.0  # running total, so each call can log its own share


def _sr_model(settings: Settings) -> Any:
    path = Path(settings.model_dir) / f"{settings.sr_model}.pth"
    return _cached(path, lambda: _load_spandrel(path, "SR"))


def _gfpgan(settings: Settings) -> Any:
    path = Path(settings.model_dir) / GFPGAN_WEIGHTS
    return _cached(path, lambda: _load_spandrel(path, "FaceSR"))


def _face_helper(settings: Settings) -> FaceRestoreHelper:
    root = Path(settings.model_dir) / "facexlib"

    def load() -> FaceRestoreHelper:
        # facexlib downloads a weight it cannot find into model_rootpath. The pod
        # has no internet, so that would hang until the job's deadline instead.
        _require(*(root / name for name in FACEXLIB_WEIGHTS))
        with warnings.catch_warnings():
            # torchvision's notice about the ResNet `pretrained` argument.
            warnings.filterwarnings("ignore", category=UserWarning, module="torchvision")
            return FaceRestoreHelper(
                upscale_factor=1,  # set per call
                face_size=FACE_SIZE,
                det_model="retinaface_resnet50",
                use_parse=True,  # ParseNet loads either way; the mask is what we blend with
                device=torch.device("cpu"),
                model_rootpath=str(root),
            )

    return _cached(root, load)


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


def _load_spandrel(path: Path, purpose: str) -> Any:
    _require(path)
    model = ModelLoader().load_from_file(path)
    if model.purpose != purpose:
        raise ValueError(f"{path.name} is a {model.purpose} model, expected {purpose}")
    return model.eval()


def _require(*paths: Path) -> None:
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        raise FileNotFoundError(
            "model weights missing (they are baked into the image, never downloaded): "
            + ", ".join(missing)
        )
