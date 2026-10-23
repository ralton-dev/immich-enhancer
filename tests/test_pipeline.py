"""enhance_file: an upright, upscaled, face-restored JPEG with the original's EXIF.

The real models load from ENHANCER_MODEL_DIR (see the README's Development
section; CI fetches them). The two large synthetic inputs swap the upscaler for
a nearest-neighbour stand-in, so that they test scale choice, tiling and the
face pass in seconds; the real networks are exercised on the fixture.
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

from immich_enhancer import pipeline
from immich_enhancer.config import Settings
from immich_enhancer.pipeline import EnhanceResult, choose_scale, enhance_file

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


def canvas_with_face(size: tuple[int, int], face_scale: int) -> Image.Image:
    """Plain colour with the fixture portrait pasted in the middle, enlarged."""
    canvas = Image.new("RGB", size, (96, 120, 150))
    face = upright_fixture().resize((120 * face_scale, 160 * face_scale), Image.Resampling.LANCZOS)
    canvas.paste(face, ((size[0] - face.width) // 2, (size[1] - face.height) // 2))
    return canvas


class NearestX4:
    """Stand-in for a x4 network: nearest-neighbour, no weights, instant."""

    scale = 4

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, x: torch.Tensor) -> torch.Tensor:
        self.calls += 1
        return torch.nn.functional.interpolate(x, scale_factor=4, mode="nearest")


class NoUpscaler:
    """Fails the test if the pipeline upscales at all."""

    def __call__(self, settings: Settings):
        raise AssertionError("x1 is faces only: the upscaler must not be loaded")


# -- the fixture, x4, real models ----------------------------------------------


def test_fixture_becomes_upright_x4_with_a_restored_face(tmp_path):
    src = tagged_fixture(tmp_path / "in.jpg")
    before = src.read_bytes()
    dst = tmp_path / "out.jpg"

    result = enhance_file(src, dst, settings())

    assert isinstance(result, EnhanceResult)
    assert result.scale == 4
    assert result.in_size == (120, 160)
    assert result.out_size == (480, 640)
    assert result.faces >= 1
    assert result.seconds > 0
    with Image.open(dst) as out:
        assert out.format == "JPEG"
        assert out.mode == "RGB"
        assert out.size == (480, 640)
        small = np.asarray(out.resize((120, 160), Image.Resampling.BOX), dtype=np.float32)
    exif = piexif.load(str(dst))
    assert exif["0th"][piexif.ImageIFD.Orientation] == 1
    assert exif["Exif"][piexif.ExifIFD.DateTimeOriginal] == DATE_TIME_ORIGINAL
    assert src.read_bytes() == before

    # Upright, not merely portrait-shaped: the result looks like the upright
    # fixture and not like it turned the other way (Orientation 8's mistake).
    upright = np.asarray(upright_fixture(), dtype=np.float32)
    assert np.abs(small - upright).mean() < 12
    assert np.abs(small - upright[::-1, ::-1]).mean() > 30


def test_the_same_input_gives_the_same_bytes_and_models_load_once(tmp_path, monkeypatch):
    # Byte-identical output lets a re-run's upload come back `duplicate`
    # (plan decision 11) instead of adding a second enhanced copy.
    src = tagged_fixture(tmp_path / "in.jpg")
    enhance_file(src, tmp_path / "first.jpg", settings())

    loads = []
    monkeypatch.setattr(
        pipeline.ModelLoader, "load_from_file", lambda self, path: loads.append(path)
    )
    monkeypatch.setattr(pipeline, "FaceRestoreHelper", lambda *a, **k: loads.append("facexlib"))
    enhance_file(src, tmp_path / "second.jpg", settings())

    assert loads == []
    assert (tmp_path / "first.jpg").read_bytes() == (tmp_path / "second.jpg").read_bytes()


def test_one_log_line_per_call(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="immich_enhancer.pipeline")

    enhance_file(tagged_fixture(tmp_path / "in.jpg"), tmp_path / "out.jpg", settings())

    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("enhanced ")]
    assert len(lines) == 1, lines
    assert re.search(r"scale=4 faces=[1-9]\d* in=120x160 out=480x640 ", lines[0]), lines[0]
    assert re.search(r"seconds=\d+\.\d+", lines[0]), lines[0]
    assert re.search(r"peak_rss_mib=\d+", lines[0]), lines[0]


RSS_PROBE = """
import json, resource, sys
from pathlib import Path
from immich_enhancer.config import Settings
from immich_enhancer.pipeline import enhance_file

settings = Settings(immich_api_key="test", app_version="test", model_dir=sys.argv[3])
result = enhance_file(Path(sys.argv[1]), Path(sys.argv[2]), settings)
peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(json.dumps({
    "scale": result.scale,
    "faces": result.faces,
    "peak_bytes": peak if sys.platform == "darwin" else peak * 1024,
}))
"""

# Peak RSS of one fresh process enhancing the fixture at x4 with the default
# RealESRGAN_x4plus: the imports, all five weights loaded, and the call.
# Measured 2026-10-23: 1.85 GiB on Linux (arm64 in Docker, 4 CPUs, the image's
# own torch); 3.20 GiB on macOS (M5 Pro). PyTorch's macOS wheels are built
# without oneDNN, so there every convolution is im2col + GEMM, which allocates
# about 0.6 GB for one 64-channel 3x3 convolution at 512x512. The plan's bound
# is for Linux, where CI and the cluster run.
RSS_LIMIT = {"linux": 2.5 * GIB, "darwin": 4 * GIB}


def test_peak_rss_for_the_x4_fixture(tmp_path):
    src = tagged_fixture(tmp_path / "in.jpg")
    # A fresh interpreter, so the number is this case alone and not whatever
    # the test session did before.
    proc = subprocess.run(
        [sys.executable, "-c", RSS_PROBE, str(src), str(tmp_path / "out.jpg"), MODEL_DIR],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    probe = json.loads(proc.stdout.strip().splitlines()[-1])
    assert (probe["scale"], probe["faces"] >= 1) == (4, True)
    limit = RSS_LIMIT.get(sys.platform, RSS_LIMIT["linux"])
    print(f"peak RSS {probe['peak_bytes'] / GIB:.2f} GiB on {sys.platform}")  # pytest -rP
    assert probe["peak_bytes"] < limit, f"peak RSS {probe['peak_bytes'] / GIB:.2f} GiB"


# -- scale rule (plan decision 7) ----------------------------------------------


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
        # Never below the original: x1 is allowed past the cap.
        (6000, 4000, 16, 1),
    ],
)
def test_choose_scale(width, height, max_output_mp, scale):
    assert choose_scale(width, height, max_output_mp) == scale


# -- the tiler -------------------------------------------------------------------


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


# -- x2 and x1 on large inputs (upscaler stood in) -------------------------------


def test_a_3_9_mp_input_is_enhanced_x2(tmp_path, monkeypatch):
    stand_in = NearestX4()
    monkeypatch.setattr(pipeline, "_sr_model", lambda settings: stand_in)
    src = tmp_path / "in.jpg"
    canvas_with_face((2300, 1700), face_scale=5).save(src, quality=90)

    result = enhance_file(src, tmp_path / "out.jpg", settings())

    assert result.scale == 2
    assert result.in_size == (2300, 1700)
    assert result.out_size == (4600, 3400)
    assert result.faces >= 1
    assert stand_in.calls > 1  # tiled
    with Image.open(tmp_path / "out.jpg") as out:
        assert out.size == (4600, 3400)


def test_a_20_mp_input_is_faces_only_and_detected_downscaled(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "_sr_model", NoUpscaler())
    detected_on = []
    helper = pipeline._face_helper(settings())
    real_detect = helper.face_det.detect_faces

    def spy(image, *args, **kwargs):
        detected_on.append(image.shape)
        return real_detect(image, *args, **kwargs)

    monkeypatch.setattr(helper.face_det, "detect_faces", spy)
    src = tmp_path / "in.jpg"
    canvas_with_face((5000, 4000), face_scale=6).save(src, quality=90)

    result = enhance_file(src, tmp_path / "out.jpg", settings())

    assert result.scale == 1
    assert result.out_size == (5000, 4000)
    assert result.faces >= 1
    # RetinaFace at full size would need about 6 GB here (measured ~0.3 GB/MP).
    assert detected_on
    assert all(h * w <= pipeline.DETECT_MAX_PIXELS for h, w, _ in detected_on)
    with Image.open(tmp_path / "out.jpg") as out:
        assert out.size == (5000, 4000)


# -- inputs that are not plain RGB JPEGs -----------------------------------------


def test_png_with_alpha_is_composited_on_white(tmp_path):
    image = upright_fixture().convert("RGBA")
    alpha = Image.new("L", image.size, 0)
    alpha.paste(255, (10, 10, image.width - 10, image.height - 10))
    image.putalpha(alpha)
    src = tmp_path / "in.png"
    image.save(src)

    result = enhance_file(src, tmp_path / "out.jpg", settings())

    assert (result.scale, result.out_size) == (4, (480, 640))
    with Image.open(tmp_path / "out.jpg") as out:
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
    result = enhance_file(src, tmp_path / "out.jpg", settings(sr_model="realesr-general-x4v3"))

    assert result.out_size == (480, 640)
    with Image.open(tmp_path / "out.jpg") as out:
        assert out.mode == out_mode  # greyscale in, greyscale out
        brightness = np.asarray(out.convert("L"), dtype=np.float32).mean()
    expected = np.asarray(reference.convert("L"), dtype=np.float32).mean()
    assert abs(brightness - expected) < 10  # e.g. 16-bit scaled down, not clipped


def test_formats_other_than_jpeg_and_png_are_refused(tmp_path):
    src = tmp_path / "in.webp"
    upright_fixture().save(src, "WEBP")

    with pytest.raises(pipeline.UnsupportedImageError, match="WEBP"):
        enhance_file(src, tmp_path / "out.jpg", settings())
    assert not (tmp_path / "out.jpg").exists()


def test_a_missing_weight_is_named_and_never_downloaded(tmp_path, monkeypatch):
    models = tmp_path / "models"
    (models / "facexlib").mkdir(parents=True)
    monkeypatch.setattr(pipeline, "_cache", {})

    with pytest.raises(FileNotFoundError, match="detection_Resnet50_Final.pth"):
        pipeline._face_helper(settings(model_dir=str(models)))
    assert list((models / "facexlib").iterdir()) == []


# -- the face pass ---------------------------------------------------------------


def aligned_fixture_face():
    helper = pipeline._face_helper(settings())
    helper.clean_all()
    helper.set_upscale_factor(4)
    bgr = np.ascontiguousarray(np.asarray(upright_fixture())[:, :, ::-1])
    helper.read_image(bgr)
    assert helper.get_face_landmarks_5(eye_dist_threshold=5) == 1
    helper.align_warp_face()
    helper.get_inverse_affine()
    return helper, bgr


def test_gfpgan_gets_its_own_input_range():
    # GFPGAN expects [-1, 1]. Fed [0, 1] (what spandrel's __call__ does), it
    # crushed the blacks and shifted the colours on this face.
    helper, _ = aligned_fixture_face()
    crop = helper.cropped_faces[0]

    restored = pipeline._restore_face(pipeline._gfpgan(settings()), crop)

    assert restored.shape == (512, 512, 3)
    assert restored.dtype == np.uint8
    assert (restored == 0).all(axis=2).mean() < 0.005
    shift = np.abs(restored.reshape(-1, 3).mean(0) - crop.reshape(-1, 3).mean(0))
    assert shift.max() < 15, shift


def test_pasting_one_face_matches_facexlib():
    # pipeline._paste_face blends inside the face's own rectangle; facexlib's
    # paste_faces_to_input_image builds several float64 copies of the whole
    # output, which at 16 MP is over a gigabyte. Same arithmetic, same pixels.
    helper, bgr = aligned_fixture_face()
    restored = pipeline._restore_face(pipeline._gfpgan(settings()), helper.cropped_faces[0])
    helper.add_restored_face(restored)
    background = np.ascontiguousarray(bgr.repeat(4, axis=0).repeat(4, axis=1))
    ours = background.copy()

    pipeline._paste_face(
        helper.face_parse, ours, restored, helper.inverse_affine_matrices[0].copy(), 4
    )
    theirs = helper.paste_faces_to_input_image(upsample_img=background.copy())

    diff = np.abs(ours.astype(np.int16) - theirs.astype(np.int16))
    assert diff.max() <= 1
    assert (diff > 0).mean() < 0.001
    assert (ours != background).any()
