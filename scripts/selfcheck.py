"""Build-time self-check. The Dockerfile runs it as uid 1000 with no network.

It fails the image build, rather than the first CronJob run, when the image
cannot do its job offline: a native library missing under cv2, a weight that
is missing or unreadable by uid 1000, or a model that no longer loads from
/opt/models (facexlib would otherwise try to download it, and the pod has no
internet).
"""

import os
import sys
from pathlib import Path

MODELS = Path(os.environ.get("ENHANCER_MODEL_DIR", "/opt/models"))
WEIGHTS = [
    "RealESRGAN_x4plus.pth",
    "realesr-general-x4v3.pth",
    "GFPGANv1.4.pth",
    "facexlib/detection_Resnet50_Final.pth",
    "facexlib/parsing_parsenet.pth",
]

unreadable = [w for w in WEIGHTS if not os.access(MODELS / w, os.R_OK)]
if unreadable:
    sys.exit(f"selfcheck: missing or unreadable as uid {os.getuid()}: {unreadable}")

import cv2  # noqa: E402  (needs libGL and libgthread from the runtime stage)
import torch  # noqa: E402
from facexlib.utils.face_restoration_helper import FaceRestoreHelper  # noqa: E402
from spandrel import ModelLoader  # noqa: E402

for name in WEIGHTS[:3]:
    model = ModelLoader().load_from_file(MODELS / name)
    print(f"selfcheck: {name}: {model.architecture.name} x{model.scale} {model.purpose}")

# Loads detection_Resnet50_Final.pth and parsing_parsenet.pth from model_rootpath.
FaceRestoreHelper(
    upscale_factor=2,
    face_size=512,
    model_rootpath=str(MODELS / "facexlib"),
    use_parse=True,
    det_model="retinaface_resnet50",
)
print(
    f"selfcheck: ok as uid {os.getuid()}: torch {torch.__version__}, "
    f"opencv {cv2.__version__}, five weights loaded with no network"
)
