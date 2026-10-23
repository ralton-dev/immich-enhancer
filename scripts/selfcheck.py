"""Build-time self-check. The Dockerfile runs it as uid 1000 with no network.

It fails the image build, rather than the first CronJob run, when the image
cannot do its job offline: a weight that is missing or unreadable by uid 1000,
a native library one of the job's imports needs, or a model that no longer
loads from /opt/models. It imports the job's own modules and runs each
upscaler through the real pipeline on a small picture, so what it proves is
what the CronJob runs.
"""

import os
import sys
import tempfile
from pathlib import Path

from immich_enhancer.config import SR_MODELS, Settings  # standard library only

MODELS = Path(os.environ.get("ENHANCER_MODEL_DIR", "/opt/models"))
WEIGHTS = [f"{name}.pth" for name in SR_MODELS]

unreadable = [w for w in WEIGHTS if not os.access(MODELS / w, os.R_OK)]
if unreadable:
    sys.exit(f"selfcheck: missing or unreadable as uid {os.getuid()}: {unreadable}")

import numpy as np  # noqa: E402
import torch  # noqa: E402
from PIL import Image  # noqa: E402

import immich_enhancer.run  # noqa: E402, F401  (the CronJob's entry point)
from immich_enhancer.pipeline import enhance_file  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    work = Path(tmp)
    src = work / "in.png"
    gradient = np.linspace(0, 255, 40 * 30 * 3).reshape(30, 40, 3).astype(np.uint8)
    Image.fromarray(gradient).save(src)
    for name in SR_MODELS:
        settings = Settings(
            immich_api_key="selfcheck",
            app_version="selfcheck",
            sr_model=name,
            model_dir=str(MODELS),
            work_dir=tmp,
        )
        result = enhance_file(src, work / f"{name}.jpg", work / f"{name}-x4.jpg", settings)
        print(
            f"selfcheck: {name}: x{result.scale} {result.in_size} -> {result.out_size} "
            f"in {result.seconds:.1f} s"
        )
print(
    f"selfcheck: ok as uid {os.getuid()}: torch {torch.__version__}, "
    f"{len(WEIGHTS)} weights loaded and run with no network"
)
