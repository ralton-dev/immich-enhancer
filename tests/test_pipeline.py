"""enhance_file: an upright upscale and a pixel-doubled copy, both with the original's EXIF.

The real models load from ENHANCER_MODEL_DIR (see the README's Development
section; CI fetches them). The large synthetic inputs swap the upscaler for a
nearest-neighbour stand-in, so that they test scale choice and tiling in
seconds; the real networks are exercised on the fixture.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import piexif
import pytest
import torch
from PIL import Image

from immich_enhancer import pipeline, run
from immich_enhancer.config import Settings
from immich_enhancer.pipeline import (
    EnhanceResult,
    NothingToEnhanceError,
    choose_scale,
    enhance_file,
)

REPO = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).parent / "fixtures" / "face-160x120.jpg"
MODEL_DIR = os.environ.get("ENHANCER_MODEL_DIR", str(REPO / "models"))
DATE_TIME_ORIGINAL = b"2004:06:12 14:03:00"
GIB = 2**30


def settings(**overrides) -> Settings:
    values = {"immich_api_key": "test", "app_version": "test", "model_dir": MODEL_DIR}
    return Settings(**(values | overrides))


def tagged_fixture(path: Path) -> Path:
    """The fixture as the contract test seeds it: Orientation 6 and a capture date."""
    exif = piexif.dump(
        {
            "0th": {piexif.ImageIFD.Orientation: 6},
            "Exif": {piexif.ExifIFD.DateTimeOriginal: DATE_TIME_ORIGINAL},
        }
    )
    out = io.BytesIO()
    piexif.insert(exif, FIXTURE.read_bytes(), out)
    path.write_bytes(out.getvalue())
    return path


def upright_fixture() -> Image.Image:
    """The fixture's pixels turned upright: Orientation 6 is "rotate 90° clockwise"."""
    with Image.open(FIXTURE) as image:
        return image.transpose(Image.Transpose.ROTATE_270)


def outputs(tmp_path: Path) -> tuple[Path, Path]:
    """Where a test has enhance_file write: the upscale, then the pixel copy."""
    return tmp_path / "out.jpg", tmp_path / "out-x4.jpg"


def pixels(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image, dtype=np.float32)


class NearestX4:
    """Stand-in for a x4 network: nearest-neighbour, no weights, instant."""

    scale = 4

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, x: torch.Tensor) -> torch.Tensor:
        self.calls += 1
        return torch.nn.functional.interpolate(x, scale_factor=4, mode="nearest")


class NoUpscaler:
    """Fails the test if the pipeline loads the upscaler at all."""

    def __call__(self, settings: Settings):
        raise AssertionError("an input held to x1 has nothing to enhance: no model may load")


# -- the fixture, x4, real models ----------------------------------------------


def test_fixture_becomes_an_upright_x4_upscale_and_a_pixel_copy(tmp_path):
    src = tagged_fixture(tmp_path / "in.jpg")
    before = src.read_bytes()
    enhanced, pixel = outputs(tmp_path)

    result = enhance_file(src, enhanced, pixel, settings())

    assert isinstance(result, EnhanceResult)
    assert result.scale == 4
    assert result.in_size == (120, 160)
    assert result.out_size == (480, 640)
    assert result.seconds > 0
    for out_path in (enhanced, pixel):
        with Image.open(out_path) as out:
            assert out.format == "JPEG"
            assert out.mode == "RGB"
            assert out.size == (480, 640)
        exif = piexif.load(str(out_path))
        assert exif["0th"][piexif.ImageIFD.Orientation] == 1
        assert exif["Exif"][piexif.ExifIFD.DateTimeOriginal] == DATE_TIME_ORIGINAL
    assert src.read_bytes() == before

    # Upright, not merely portrait-shaped: the upscale looks like the upright
    # fixture and not like it turned the other way (Orientation 8's mistake).
    upright = np.asarray(upright_fixture(), dtype=np.float32)
    with Image.open(enhanced) as out:
        small = np.asarray(out.resize((120, 160), Image.Resampling.BOX), dtype=np.float32)
    assert np.abs(small - upright).mean() < 12
    assert np.abs(small - upright[::-1, ::-1]).mean() > 30

    # The pixel copy is the upright fixture with every pixel a 4x4 block, give
    # or take its JPEG encoding (measured 0.9 levels). The upscale is not
    # (measured 6.8 levels from it).
    nearest = upright.repeat(4, axis=0).repeat(4, axis=1)
    assert np.abs(pixels(pixel) - nearest).mean() < 2
    assert np.abs(pixels(enhanced) - nearest).mean() > 4


def test_the_same_input_gives_the_same_bytes_and_the_model_loads_once(tmp_path, monkeypatch):
    # Byte-identical output lets a re-run's uploads come back `duplicate`
    # (plan decision 11) instead of adding a second pair of copies.
    src = tagged_fixture(tmp_path / "in.jpg")
    enhance_file(src, tmp_path / "first.jpg", tmp_path / "first-x4.jpg", settings())

    loads = []
    monkeypatch.setattr(
        pipeline.ModelLoader, "load_from_file", lambda self, path: loads.append(path)
    )
    enhance_file(src, tmp_path / "second.jpg", tmp_path / "second-x4.jpg", settings())

    assert loads == []
    for name in ("{}.jpg", "{}-x4.jpg"):
        first, second = tmp_path / name.format("first"), tmp_path / name.format("second")
        assert first.read_bytes() == second.read_bytes()


def test_one_log_line_per_call(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="immich_enhancer.pipeline")

    enhance_file(tagged_fixture(tmp_path / "in.jpg"), *outputs(tmp_path), settings())

    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("enhanced ")]
    assert len(lines) == 1, lines
    assert re.search(r"scale=4 in=120x160 out=480x640 ", lines[0]), lines[0]
    assert re.search(r"seconds=\d+\.\d+", lines[0]), lines[0]
    assert re.search(r"peak_rss_mib=\d+", lines[0]), lines[0]
    assert "face" not in lines[0]


RSS_PROBE = """
import json, resource, sys
from pathlib import Path
from immich_enhancer.config import Settings
from immich_enhancer.pipeline import enhance_file

settings = Settings(immich_api_key="test", app_version="test", model_dir=sys.argv[4])
result = enhance_file(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), settings)
peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(json.dumps({
    "scale": result.scale,
    "peak_bytes": peak if sys.platform == "darwin" else peak * 1024,
}))
"""

# Peak RSS of one fresh process enhancing the fixture at x4 with the default
# RealESRGAN_x4plus: the imports, its one weight, and the call that writes both
# copies. Measured 2026-10-23, after the face pass was removed: 0.99 GiB on
# Linux (arm64 in Docker, 4 CPUs, the image's own torch), 1.85 GiB before;
# 1.67 GiB on macOS (M5 Pro), 3.20 GiB before. PyTorch's macOS wheels are built
# without oneDNN, so there every convolution is im2col + GEMM, which allocates
# about 0.6 GB for one 64-channel 3x3 convolution at 512x512. The plan's bound
# is for Linux, where CI and the cluster run.
RSS_LIMIT = {"linux": 2.5 * GIB, "darwin": 4 * GIB}


def test_peak_rss_for_the_x4_fixture(tmp_path):
    src = tagged_fixture(tmp_path / "in.jpg")
    enhanced, pixel = outputs(tmp_path)
    # A fresh interpreter, so the number is this case alone and not whatever
    # the test session did before.
    proc = subprocess.run(
        [sys.executable, "-c", RSS_PROBE, str(src), str(enhanced), str(pixel), MODEL_DIR],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    probe = json.loads(proc.stdout.strip().splitlines()[-1])
    assert probe["scale"] == 4
    assert enhanced.is_file() and pixel.is_file()
    limit = RSS_LIMIT.get(sys.platform, RSS_LIMIT["linux"])
    print(f"peak RSS {probe['peak_bytes'] / GIB:.2f} GiB on {sys.platform}")  # pytest -rP
    assert probe["peak_bytes"] < limit, f"peak RSS {probe['peak_bytes'] / GIB:.2f} GiB"


# -- scale rule (plan decisions 7 and 24) ------------------------------------------


@pytest.mark.parametrize(
    ("width", "height", "max_output_mp", "scale"),
    [
        (120, 160, 16, 4),
        (1000, 1000, 16, 4),  # exactly 1 MP: still x4, exactly 16 MP out
        (1001, 1000, 16, 2),
        (2000, 2000, 16, 2),  # exactly 4 MP: still x2, exactly 16 MP out
        (2300, 1700, 16, 2),
        # The plan's WP-A acceptance names 2500x1900 as a x2 input, but it is
        # 4.75 MP, past decision 7's 4 MP line, and x2 would be 19 MP against
        # the 16 MP cap. Decision 7 wins; the x2 test below uses 2300x1700.
        (2500, 1900, 16, 1),
        (2001, 2000, 16, 1),
        (5000, 4000, 16, 1),
        # A lower cap steps the scale down rather than producing a fraction.
        (1000, 1000, 8, 2),
        (1000, 1000, 3, 1),
        (2000, 2000, 15, 1),
        # Never below the original.
        (6000, 4000, 16, 1),
    ],
)
def test_choose_scale(width, height, max_output_mp, scale):
    assert choose_scale(width, height, max_output_mp) == scale


@pytest.mark.parametrize(
    ("size", "max_output_mp"),
    [((5000, 4000), 16), ((1000, 1000), 3)],
    ids=["over-4-MP", "held-to-x1-by-the-cap"],
)
def test_an_input_held_to_x1_has_nothing_to_enhance_and_nothing_is_written(
    tmp_path, monkeypatch, size, max_output_mp
):
    monkeypatch.setattr(pipeline, "_sr_model", NoUpscaler())
    src = tmp_path / "in.jpg"
    Image.new("RGB", size, (96, 120, 150)).save(src, quality=90)

    with pytest.raises(
        NothingToEnhanceError, match=r"^nothing to enhance: no upscale at this size"
    ):
        enhance_file(src, *outputs(tmp_path), settings(max_output_mp=max_output_mp))
    assert [p.name for p in tmp_path.iterdir()] == ["in.jpg"]


@pytest.mark.parametrize("error", [pipeline.UnsupportedImageError, NothingToEnhanceError])
def test_the_run_skips_what_the_pipeline_refuses(error):
    # run.py matches these by class name, so that it never imports torch. A
    # rename here would turn every such skip into a failure.
    assert issubclass(error, ValueError)
    assert run._is_pipeline_skip(error("refused"))


# -- the tiler and the pixel copy ---------------------------------------------------


@pytest.mark.parametrize("out_scale", [4, 2])
def test_tiles_reassemble_exactly(out_scale):
    # Nearest x4 then the 2x2 area downsample is exactly nearest x2, so any
    # tile misplaced, cropped or overlapped shows up as a mismatch.
    rgb = np.random.default_rng(7).integers(0, 256, (110, 150, 3), dtype=np.uint8)

    out = pipeline._upscale(NearestX4(), rgb, out_scale, tile=64)

    expected = rgb.repeat(out_scale, axis=0).repeat(out_scale, axis=1)
    assert out.dtype == np.uint8
    np.testing.assert_array_equal(out, expected)


def test_tiled_real_model_matches_untiled():
    model = pipeline._sr_model(settings(sr_model="realesr-general-x4v3"))
    rgb = np.array(upright_fixture())

    tiled = pipeline._upscale(model, rgb, 4, tile=64).astype(np.int16)
    whole = pipeline._upscale(model, rgb, 4, tile=4096).astype(np.int16)

    diff = np.abs(tiled - whole)
    assert diff.mean() < 0.5
    assert np.percentile(diff, 99.9) <= 8


@pytest.mark.parametrize("scale", [4, 2])
def test_the_pixel_copy_makes_each_pixel_a_block(scale):
    rgb = np.random.default_rng(3).integers(0, 256, (37, 53, 3), dtype=np.uint8)

    copy = pipeline._pixel_copy(rgb, scale)

    expected = rgb.repeat(scale, axis=0).repeat(scale, axis=1)
    np.testing.assert_array_equal(np.asarray(copy), expected)


def test_a_3_9_mp_input_is_enhanced_x2(tmp_path, monkeypatch):
    stand_in = NearestX4()
    monkeypatch.setattr(pipeline, "_sr_model", lambda settings: stand_in)
    src = tmp_path / "in.jpg"
    Image.new("RGB", (2300, 1700), (96, 120, 150)).save(src, quality=90)

    result = enhance_file(src, *outputs(tmp_path), settings())

    assert result.scale == 2
    assert result.in_size == (2300, 1700)
    assert result.out_size == (4600, 3400)
    assert stand_in.calls > 1  # tiled
    for out_path in outputs(tmp_path):
        with Image.open(out_path) as out:
            assert out.size == (4600, 3400)


# -- inputs that are not plain RGB JPEGs -----------------------------------------


def test_png_with_alpha_is_composited_on_white(tmp_path):
    image = upright_fixture().convert("RGBA")
    alpha = Image.new("L", image.size, 0)
    alpha.paste(255, (10, 10, image.width - 10, image.height - 10))
    image.putalpha(alpha)
    src = tmp_path / "in.png"
    image.save(src)

    result = enhance_file(src, *outputs(tmp_path), settings())

    assert (result.scale, result.out_size) == (4, (480, 640))
    for out_path in outputs(tmp_path):
        with Image.open(out_path) as out:
            assert out.mode == "RGB"
            assert "exif" not in out.info  # a PNG without EXIF gives a JPEG without
            corner = np.asarray(out)[:20, :20]
        assert corner.min() >= 235


def sixteen_bit_grey(path: Path) -> Image.Image:
    grey = np.asarray(upright_fixture().convert("L"), dtype=np.uint16) * 257
    Image.fromarray(grey).save(path)  # a 16-bit greyscale PNG
    with Image.open(path) as image:
        assert image.mode.startswith("I"), image.mode
    return upright_fixture().convert("L")


@pytest.mark.parametrize(
    ("mode", "suffix", "out_mode"),
    [
        ("L", ".jpg", "L"),
        ("P", ".png", "RGB"),
        ("CMYK", ".jpg", "RGB"),
        ("I;16", ".png", "L"),
    ],
)
def test_other_modes_are_converted_not_refused(tmp_path, mode, suffix, out_mode):
    src = tmp_path / f"in{suffix}"
    if mode == "I;16":
        reference = sixteen_bit_grey(src)
    else:
        reference = upright_fixture().convert(mode)
        reference.save(src)
    # The compact upscaler keeps four runs quick; the conversion is the point.
    result = enhance_file(src, *outputs(tmp_path), settings(sr_model="realesr-general-x4v3"))

    assert result.out_size == (480, 640)
    expected = np.asarray(reference.convert("L"), dtype=np.float32).mean()
    for out_path in outputs(tmp_path):
        with Image.open(out_path) as out:
            assert out.mode == out_mode  # greyscale in, greyscale out
            brightness = np.asarray(out.convert("L"), dtype=np.float32).mean()
        assert abs(brightness - expected) < 10  # e.g. 16-bit scaled down, not clipped


def test_formats_other_than_jpeg_and_png_are_refused(tmp_path):
    src = tmp_path / "in.webp"
    upright_fixture().save(src, "WEBP")

    with pytest.raises(pipeline.UnsupportedImageError, match="WEBP"):
        enhance_file(src, *outputs(tmp_path), settings())
    assert [p.name for p in tmp_path.iterdir()] == ["in.webp"]


def test_a_missing_weight_is_named_and_never_downloaded(tmp_path, monkeypatch):
    models = tmp_path / "models"
    models.mkdir()
    monkeypatch.setattr(pipeline, "_cache", {})
    src = tagged_fixture(tmp_path / "in.jpg")

    with pytest.raises(FileNotFoundError, match="RealESRGAN_x4plus.pth"):
        enhance_file(src, *outputs(tmp_path), settings(model_dir=str(models)))
    assert list(models.iterdir()) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["in.jpg", "models"]
