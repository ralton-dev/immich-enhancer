"""EXIF for the output JPEGs: the original's block, upright, without its thumbnail.

The pipeline turns the pixels upright before it enlarges them, so `Orientation`
becomes 1. The embedded thumbnail shows the original, at the original's size, so
it goes. Everything else is copied, the capture dates and GPS above all: Immich
places each copy on the timeline and the map from them.
"""

from __future__ import annotations

import logging
import struct

import piexif

log = logging.getLogger(__name__)

# One JPEG APP1 segment holds 65535 bytes including its length field; Pillow
# refuses EXIF longer than this.
MAX_EXIF_BYTES = 65_533

_IFDS = ("0th", "Exif", "GPS", "Interop")
_DUMP_ERRORS = (ValueError, TypeError, struct.error)


def output_exif(original: bytes | None, size: tuple[int, int]) -> bytes | None:
    """EXIF bytes for Pillow's `exif=`, or None when the output should carry none.

    `original` is the block as Pillow reads it (`Image.info["exif"]`, from a
    JPEG's APP1 or a PNG's eXIf chunk), or None. `size` is the output's
    (width, height). A block piexif cannot read gives None and a warning rather
    than an exception: the copy is still worth having, and its upload carries
    the original's timestamps either way.
    """
    if not original:
        return None
    try:
        exif = piexif.load(original)
    except Exception as exc:  # struct.error, ValueError, InvalidImageDataError, ...
        log.warning("original EXIF is unreadable, the copy carries none: %r", exc)
        return None

    exif["1st"] = {}
    exif["thumbnail"] = None
    exif["0th"][piexif.ImageIFD.Orientation] = 1
    width, height = size
    for ifd, tag, value in (
        ("0th", piexif.ImageIFD.ImageWidth, width),
        ("0th", piexif.ImageIFD.ImageLength, height),
        ("Exif", piexif.ExifIFD.PixelXDimension, width),
        ("Exif", piexif.ExifIFD.PixelYDimension, height),
    ):
        if tag in exif[ifd]:  # describe the output, but add nothing
            exif[ifd][tag] = value

    data = _dump(exif)
    if len(data) > MAX_EXIF_BYTES and piexif.ExifIFD.MakerNote in exif["Exif"]:
        log.info("dropped the MakerNote: the EXIF would not fit one JPEG segment")
        del exif["Exif"][piexif.ExifIFD.MakerNote]
        data = _dump(exif)
    if len(data) > MAX_EXIF_BYTES:
        log.warning("original EXIF is %d bytes, too long for a JPEG: copy carries none", len(data))
        return None
    return data


def _dump(exif: dict) -> bytes:
    """`piexif.dump`, dropping any tag piexif can read but not write back.

    Some cameras store a tag with a type the standard does not give it (SceneType
    as BYTE rather than UNDEFINED, say). piexif loads it and then refuses to dump
    the whole block, which would cost the dates and GPS for one odd tag.
    """
    try:
        return piexif.dump(exif)
    except _DUMP_ERRORS:
        pass
    for ifd in _IFDS:
        for tag, value in list(exif[ifd].items()):
            try:
                piexif.dump({ifd: {tag: value}})
            except _DUMP_ERRORS as exc:
                log.info("dropped EXIF %s tag %d, which piexif cannot write: %s", ifd, tag, exc)
                del exif[ifd][tag]
    return piexif.dump(exif)
