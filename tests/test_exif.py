"""The enhanced JPEG's EXIF: the original's block, upright, without its thumbnail.

`output_exif` takes the block as Pillow reads it (`Image.info["exif"]`, from a
JPEG's APP1 or a PNG's eXIf chunk) and returns bytes for Pillow's `exif=`.
"""

from __future__ import annotations

import io
import logging
import struct

import piexif
import pytest
from PIL import Image

from immich_enhancer.exif import output_exif

DATE_TIME = b"2004:06:12 14:03:00"
DATE_TIME_ORIGINAL = b"2004:06:12 14:03:00"
DATE_TIME_DIGITIZED = b"2004:06:12 14:03:01"
OFFSET_TIME_ORIGINAL = b"+01:00"
GPS = {
    piexif.GPSIFD.GPSVersionID: (2, 2, 0, 0),
    piexif.GPSIFD.GPSLatitudeRef: b"N",
    piexif.GPSIFD.GPSLatitude: ((51, 1), (30, 1), (1234, 100)),
    piexif.GPSIFD.GPSLongitudeRef: b"W",
    piexif.GPSIFD.GPSLongitude: ((0, 1), (7, 1), (3956, 100)),
    piexif.GPSIFD.GPSAltitudeRef: 0,
    piexif.GPSIFD.GPSAltitude: (2100, 100),
    piexif.GPSIFD.GPSDateStamp: b"2004:06:12",
}


def thumbnail_jpeg() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (16, 12), (200, 30, 30)).save(out, "JPEG")
    return out.getvalue()


def camera_exif(extra_exif: dict[int, object] | None = None) -> bytes:
    """A 2000s compact camera's block: rotated, dated, geotagged, with a thumbnail."""
    exif_ifd = {
        piexif.ExifIFD.DateTimeOriginal: DATE_TIME_ORIGINAL,
        piexif.ExifIFD.DateTimeDigitized: DATE_TIME_DIGITIZED,
        piexif.ExifIFD.OffsetTimeOriginal: OFFSET_TIME_ORIGINAL,
        piexif.ExifIFD.PixelXDimension: 160,
        piexif.ExifIFD.PixelYDimension: 120,
    }
    exif_ifd.update(extra_exif or {})
    return piexif.dump(
        {
            "0th": {
                piexif.ImageIFD.Orientation: 6,
                piexif.ImageIFD.Make: b"Canon",
                piexif.ImageIFD.Model: b"Canon PowerShot A70",
                piexif.ImageIFD.DateTime: DATE_TIME,
            },
            "Exif": exif_ifd,
            "GPS": dict(GPS),
            "1st": {piexif.ImageIFD.Compression: 6},
            "thumbnail": thumbnail_jpeg(),
        }
    )


def test_dates_and_gps_survive_orientation_resets_and_thumbnail_goes():
    original = camera_exif()
    assert piexif.load(original)["thumbnail"]  # the input really has one

    out = piexif.load(output_exif(original, (480, 640)))

    assert out["0th"][piexif.ImageIFD.Orientation] == 1
    assert out["0th"][piexif.ImageIFD.DateTime] == DATE_TIME
    assert out["0th"][piexif.ImageIFD.Model] == b"Canon PowerShot A70"
    assert out["Exif"][piexif.ExifIFD.DateTimeOriginal] == DATE_TIME_ORIGINAL
    assert out["Exif"][piexif.ExifIFD.DateTimeDigitized] == DATE_TIME_DIGITIZED
    assert out["Exif"][piexif.ExifIFD.OffsetTimeOriginal] == OFFSET_TIME_ORIGINAL
    assert out["GPS"] == GPS
    assert out["1st"] == {}
    assert out["thumbnail"] is None


def test_pixel_dimensions_describe_the_output():
    out = piexif.load(output_exif(camera_exif(), (480, 640)))

    assert out["Exif"][piexif.ExifIFD.PixelXDimension] == 480
    assert out["Exif"][piexif.ExifIFD.PixelYDimension] == 640


def test_pillow_writes_it_into_a_jpeg_intact():
    out = io.BytesIO()
    Image.new("RGB", (480, 640)).save(out, "JPEG", exif=output_exif(camera_exif(), (480, 640)))

    written = piexif.load(out.getvalue())
    assert written["0th"][piexif.ImageIFD.Orientation] == 1
    assert written["Exif"][piexif.ExifIFD.DateTimeOriginal] == DATE_TIME_ORIGINAL
    assert written["GPS"] == GPS
    assert written["thumbnail"] is None


@pytest.mark.parametrize("original", [None, b""])
def test_no_exif_in_gives_no_exif_out(original):
    # A PNG without an eXIf chunk: Pillow has no "exif" in Image.info.
    assert output_exif(original, (480, 640)) is None


def test_png_without_exif_round_trips_to_a_jpeg_without_exif():
    png = io.BytesIO()
    Image.new("RGB", (8, 6)).save(png, "PNG")
    with Image.open(png) as image:
        exif = output_exif(image.info.get("exif"), (32, 24))
        jpeg = io.BytesIO()
        image.save(jpeg, "JPEG", **({"exif": exif} if exif else {}))
    assert exif is None
    with Image.open(jpeg) as written:
        assert "exif" not in written.info


def test_a_makernote_too_big_for_one_jpeg_segment_is_dropped():
    # A JPEG APP1 segment holds at most 65533 bytes; Pillow refuses more.
    original = camera_exif({piexif.ExifIFD.MakerNote: b"\x00" * 70_000})

    exif = output_exif(original, (480, 640))

    assert len(exif) <= 65_533
    out = piexif.load(exif)
    assert piexif.ExifIFD.MakerNote not in out["Exif"]
    assert out["Exif"][piexif.ExifIFD.DateTimeOriginal] == DATE_TIME_ORIGINAL
    assert out["GPS"] == GPS


def tiff_with_scene_type_as_byte() -> bytes:
    """A block piexif can read but not write back: SceneType stored as BYTE.

    Some cameras store SceneType (0xA301) as BYTE where the standard says
    UNDEFINED. piexif then loads it as an int and refuses to dump it.
    """
    dto = DATE_TIME_ORIGINAL + b"\x00"  # 20 bytes, ASCII with its NUL
    ifd0 = 8
    exif_ifd = ifd0 + 2 + 12 + 4  # one entry, then the next-IFD pointer
    dto_at = exif_ifd + 2 + 2 * 12 + 4
    data = b"MM\x00\x2a" + struct.pack(">I", ifd0)
    data += struct.pack(">H", 1) + struct.pack(">HHII", 0x8769, 4, 1, exif_ifd) + b"\0" * 4
    data += struct.pack(">H", 2)
    data += struct.pack(">HHII", 0x9003, 2, len(dto), dto_at)
    data += struct.pack(">HHI", 0xA301, 1, 1) + b"\x01\0\0\0"
    data += b"\0" * 4 + dto
    return b"Exif\x00\x00" + data


def test_a_tag_piexif_cannot_write_is_dropped_and_the_rest_kept():
    original = tiff_with_scene_type_as_byte()
    loaded = piexif.load(original)
    assert loaded["Exif"][piexif.ExifIFD.SceneType] == 1
    with pytest.raises(ValueError):
        piexif.dump(loaded)

    out = piexif.load(output_exif(original, (480, 640)))

    assert piexif.ExifIFD.SceneType not in out["Exif"]
    assert out["Exif"][piexif.ExifIFD.DateTimeOriginal] == DATE_TIME_ORIGINAL
    assert out["0th"][piexif.ImageIFD.Orientation] == 1


def test_an_unreadable_block_gives_no_exif_and_a_warning(caplog):
    caplog.set_level(logging.WARNING, logger="immich_enhancer.exif")

    assert output_exif(b"Exif\x00\x00MM\x00\x2a\x00\x00\xff\xff", (480, 640)) is None

    assert any("EXIF" in r.getMessage() for r in caplog.records)
