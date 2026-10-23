# Test fixtures

## `face-160x120.jpg`

A head-and-shoulders portrait, 160×120 pixels **as stored**, with no metadata.

| | |
| --- | --- |
| Source | [File:Mae Carol Jemison.jpg](https://commons.wikimedia.org/wiki/File:Mae_Carol_Jemison.jpg) on Wikimedia Commons, original at <https://upload.wikimedia.org/wikipedia/commons/5/55/Mae_Carol_Jemison.jpg> (6204×7755, sha1 `7ba6b96b03f6e585953a3be15129924d1c486f00`) |
| Subject | Mae Carol Jemison, NASA astronaut portrait, taken 1 July 1992 |
| Author | NASA |
| Licence | **Public domain**: a work of the US federal government (Commons template `PD-USGov-NASA`). Its use here does not imply endorsement by NASA. |
| This file | sha256 `0e8ffc7a16cac9ab290089c811f6fe739133622f48c6cb59e6e012c899e5bd5e` |

### How it was made

1. Cropped the original to the 3:4 box `(2101, 300, 5103, 4303)`, which keeps head and shoulders.
2. Resized it to **120×160** (upright portrait) with Lanczos.
3. Rotated it **90° counter-clockwise** and saved it as a 160×120 JPEG (quality 90), with no
   EXIF or ICC profile.

The rotation is deliberate. The contract test writes EXIF `Orientation 6` ("rotate 90°
clockwise to display") and `DateTimeOriginal 2004:06:12 14:03:00` with `piexif` at setup.
A viewer that honours the orientation then shows the face upright at **120×160**, and an
upright ×4 enhancement is the **480×640** the test expects. If the stored pixels were
already upright, Orientation 6 would turn the face onto its side, and the face pass would
have nothing to find.

Checked on 2026-10-23 with `facexlib` 0.3.0 (RetinaFace ResNet-50): on the upright 120×160
image it detects exactly one face (box about 44×55 px, score 1.0). GFPGAN restores it and
ParseNet pastes it back.
