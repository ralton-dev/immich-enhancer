"""The run loop against an in-memory Immich, with a stub in place of the pipeline.

The fake is the contract test's FakeImmich (the plan's call table, OpenAPI
v3.3.5 shapes), extended with what these tests set up: ping and version public
as they are in Immich, the key's own permissions, extra detail fields per asset
(a video, an edited photo, a pending metadata read), assets in the trash, and
refused calls. The stub writes recognisable bytes and never imports torch.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_contract import FakeImmich, immich_checksum
from werkzeug import Request, Response

from immich_enhancer import immich, run
from immich_enhancer.config import Settings

API_KEY = "run-test-key"
FILE_CREATED_AT = "2004-06-12T13:03:00.000Z"
FILE_MODIFIED_AT = "2011-03-02T09:15:27.000Z"
LOCAL_DATE_TIME = "2004-06-12T14:03:00.000Z"
NEEDED = [
    "asset.read",
    "asset.download",
    "asset.upload",
    "stack.create",
    "album.read",
    "albumAsset.create",
    "tag.create",
    "tag.asset",
]


@dataclass
class RunFake(FakeImmich):
    # Extra fields merged into an asset's detail, by asset id.
    overrides: dict[str, dict] = field(default_factory=dict)
    # The last word on any detail: (asset id, detail) -> detail.
    on_detail: Callable[[str, dict], dict] | None = None
    # Hidden from search, as Immich hides trashed assets.
    in_trash: set[str] = field(default_factory=set)
    ping_status: int = 200
    # What GET /api-keys/me answers; None leaves the route unknown (404).
    permissions: list[str] | None = field(default_factory=lambda: list(NEEDED))
    # (method, path pattern, status, message) answered instead of the route.
    refusals: list[tuple[str, str, int, str]] = field(default_factory=list)

    def detail(self, asset_id: str) -> dict:
        out = super().detail(asset_id)
        out.update(self.overrides.get(asset_id, {}))
        return self.on_detail(asset_id, out) if self.on_detail else out

    def dispatch(self, request: Request) -> Response:
        for method, pattern, status, message in self.refusals:
            if request.method == method and re.fullmatch(pattern, request.path):
                self._record(request)
                return self._json(status, {"message": message})
        if request.method == "GET" and request.path == "/api/server/ping":
            self._record(request)  # public in Immich: no key needed
            if self.ping_status != 200:
                return self._json(self.ping_status, {"message": "starting"})
            return self._ping(request)
        if request.method == "GET" and request.path == "/api/server/version":
            self._record(request)
            return self._version(request)
        if (
            request.method == "GET"
            and request.path == "/api/api-keys/me"
            and request.headers.get("x-api-key") == self.api_key
            and self.permissions is not None
        ):
            self._record(request)
            return self._json(
                200, {"id": "key-1", "name": "immich-enhancer", "permissions": self.permissions}
            )
        return super().dispatch(request)

    def _record(self, request: Request) -> None:
        entry: dict = {"method": request.method, "path": request.path, "args": dict(request.args)}
        if request.mimetype == "application/json":
            entry["json"] = request.get_json(silent=True)
        self.log.append(entry)

    def _search_metadata(self, request: Request) -> Response:
        payload = json.loads(super()._search_metadata(request).get_data())
        items = [i for i in payload["assets"]["items"] if i["id"] not in self.in_trash]
        payload["assets"].update(items=items, count=len(items), total=len(items))
        return self._json(200, payload)

    def calls(self) -> list[str]:
        return [f"{e['method']} {e['path']}" for e in self.log]


def enhanced(data: bytes) -> bytes:
    return b"ENHANCED:" + data


@dataclass
class Stub:
    """Stands in for pipeline.enhance_file. Fails for inputs listed in `fail`."""

    fail: set[bytes] = field(default_factory=set)
    on_call: Callable[[], None] | None = None
    calls: list[bytes] = field(default_factory=list)
    settings: Settings | None = None

    def __call__(self, src: Path, dst: Path, settings: Settings) -> SimpleNamespace:
        data = src.read_bytes()
        self.calls.append(data)
        self.settings = settings
        if self.on_call:
            self.on_call()
        if data in self.fail:
            raise RuntimeError("stub: this one cannot be enhanced")
        dst.write_bytes(enhanced(data))
        return SimpleNamespace(
            scale=4, faces=1, in_size=(160, 120), out_size=(640, 480), seconds=0.01
        )


def queue_photo(
    fake: RunFake,
    data: bytes,
    *,
    name: str = "photo.jpg",
    mime: str = "image/jpeg",
    favorite: bool = False,
    tags: tuple[str, ...] = ("enhance",),
    **detail: object,
) -> str:
    asset_id = fake.add_asset(
        data,
        filename=name,
        file_created_at=FILE_CREATED_AT,
        file_modified_at=FILE_MODIFIED_AT,
        mime=mime,
        favorite=favorite,
    )
    for tag in tags:
        fake.tag_members[fake.ensure_tag(tag)].append(asset_id)
    if detail:
        fake.overrides[asset_id] = dict(detail)
    return asset_id


def summary_of(capsys) -> dict:
    """The one JSON summary line on stdout."""
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line[:1] == "{"]
    summaries = [line for line in lines if line.get("event") == "summary"]
    assert len(summaries) == 1, lines
    return summaries[0]


def changes(fake: RunFake) -> list[str]:
    """Requests that change an asset, album, stack or tag membership. The tag
    upsert (PUT /tags) only names the five tags; search is a read sent as POST."""
    reads = {"PUT /api/tags", "POST /api/search/metadata"}
    return [c for c in fake.calls() if not c.startswith("GET ") and c not in reads]


@pytest.fixture
def fake(httpserver, monkeypatch, tmp_path) -> RunFake:
    fake = RunFake(api_key=API_KEY)
    httpserver.expect_request(re.compile(r"^/.*$")).respond_with_handler(fake.dispatch)
    for name in list(os.environ):
        if name.startswith("ENHANCER_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("IMMICH_URL", httpserver.url_for("/").rstrip("/"))
    monkeypatch.setenv("IMMICH_API_KEY", API_KEY)
    monkeypatch.setenv("APP_VERSION", "run-test")
    monkeypatch.setenv("ENHANCER_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setattr(immich, "PING_INTERVAL_S", 0.0)
    monkeypatch.setattr(run, "TIMELINE_POLL_S", 0.0)
    return fake


# -- the shape of a run -------------------------------------------------------


def test_an_empty_queue_exits_0_with_one_summary_line(fake, capsys):
    assert run.main(enhance=Stub()) == 0
    summary = summary_of(capsys)
    assert summary["app_version"] == "run-test"
    assert {k: summary[k] for k in ("processed", "succeeded", "failed", "skipped")} == {
        "processed": 0,
        "succeeded": 0,
        "failed": 0,
        "skipped": 0,
    }
    assert isinstance(summary["seconds"], int | float)
    assert summary["exit_code"] == 0
    assert fake.calls() == [
        "GET /api/server/ping",
        "GET /api/server/version",
        "GET /api/api-keys/me",
        "PUT /api/tags",
        "POST /api/search/metadata",
    ]


def test_a_queued_photo_is_enhanced_stacked_and_retagged(fake, capsys, tmp_path):
    original = queue_photo(fake, b"\xff\xd8photo", name="IMG_0001.JPG", favorite=True)
    album = fake.add_album("Summer 2004", [original])
    elsewhere = fake.add_album("Elsewhere", [])
    stub = Stub()

    assert run.main(enhance=stub) == 0

    (new,) = [asset_id for asset_id in fake.assets if asset_id != original]
    assert fake.files[new] == enhanced(b"\xff\xd8photo")
    (upload,) = fake.uploads
    assert upload["filename"] == "IMG_0001-enhanced.jpg"
    assert upload["form"] == {
        "filename": "IMG_0001-enhanced.jpg",
        "fileCreatedAt": FILE_CREATED_AT,
        "fileModifiedAt": FILE_MODIFIED_AT,
        "isFavorite": "true",
    }
    assert [s["assetIds"] for s in fake.stacks] == [[new, original]]
    assert fake.tag_names(original) == {"enhanced-source"}
    assert fake.tag_names(new) == {"enhanced"}
    assert fake.albums[album]["assetIds"] == [original, new]
    assert fake.albums[elsewhere]["assetIds"] == []
    # The original was read, never written.
    assert fake.files[original] == b"\xff\xd8photo"
    assert [
        c
        for c in changes(fake)
        if original in c and c.startswith(("PUT /api/assets", "DELETE /api/assets"))
    ] == []
    # The pipeline got the settings, and its scratch files are gone.
    assert isinstance(stub.settings, Settings)
    assert list((tmp_path / "work").iterdir()) == []
    summary = summary_of(capsys)
    assert (summary["processed"], summary["succeeded"], summary["exit_code"]) == (1, 1, 0)


# -- skip rules (decisions 10 and 11) ------------------------------------------


def test_a_video_is_retagged_skipped_and_the_run_exits_0(fake, capsys):
    video = queue_photo(fake, b"video", name="clip.mp4", mime="video/mp4", type="VIDEO")
    stub = Stub()
    assert run.main(enhance=stub) == 0
    assert fake.tag_names(video) == {"enhance-skipped"}
    assert stub.calls == []
    assert f"GET /api/assets/{video}/original" not in fake.calls()
    assert fake.uploads == []
    summary = summary_of(capsys)
    assert (summary["processed"], summary["skipped"], summary["failed"]) == (1, 1, 0)
    assert summary["exit_code"] == 0


def test_an_unsupported_file_type_is_skipped_with_its_reason_logged(fake, capsys, caplog):
    caplog.set_level(logging.INFO)
    heic = queue_photo(fake, b"heic", name="IMG_0002.HEIC", mime="image/heic")
    assert run.main(enhance=Stub()) == 0
    assert fake.tag_names(heic) == {"enhance-skipped"}
    assert any("image/heic" in record.getMessage() for record in caplog.records)


def test_an_original_already_in_a_stack_is_skipped(fake, capsys):
    original = queue_photo(fake, b"photo")
    burst = fake.add_asset(
        b"burst",
        filename="b.jpg",
        file_created_at=FILE_CREATED_AT,
        file_modified_at=FILE_MODIFIED_AT,
    )
    fake.stacks.append({"id": "stack-1", "assetIds": [burst, original]})
    stub = Stub()
    assert run.main(enhance=stub) == 0
    assert fake.tag_names(original) == {"enhance-skipped"}
    assert fake.stacks == [{"id": "stack-1", "assetIds": [burst, original]}]
    assert stub.calls == []
    assert fake.uploads == []


def test_a_photo_edited_in_immich_is_skipped(fake, capsys):
    # The download is the unedited original: an enhanced copy stacked on top
    # would show the photo without Ben's crop or rotation.
    edited = queue_photo(fake, b"photo", isEdited=True)
    stub = Stub()
    assert run.main(enhance=stub) == 0
    assert fake.tag_names(edited) == {"enhance-skipped"}
    assert stub.calls == []


@pytest.mark.parametrize("done_tag", ["enhanced-source", "enhanced"])
def test_a_retagged_source_or_result_is_left_alone_and_the_stray_tag_removed(
    fake, capsys, done_tag
):
    asset = queue_photo(fake, b"photo", tags=("enhance", done_tag))
    other = fake.add_asset(
        b"other",
        filename="o.jpg",
        file_created_at=FILE_CREATED_AT,
        file_modified_at=FILE_MODIFIED_AT,
    )
    fake.stacks.append({"id": "stack-1", "assetIds": [other, asset]})
    stub = Stub()
    assert run.main(enhance=stub) == 0
    assert fake.tag_names(asset) == {done_tag}
    assert fake.stacks == [{"id": "stack-1", "assetIds": [other, asset]}]
    assert stub.calls == []
    assert fake.uploads == []
    assert summary_of(capsys)["skipped"] == 1


class UnsupportedImageError(ValueError):
    """Stands in for pipeline.UnsupportedImageError, which run.py matches by name."""


def test_a_file_the_pipeline_finds_unsupported_is_skipped_not_failed(fake, capsys, caplog):
    # Immich labels a file by its extension: a WebP saved as .jpg arrives as image/jpeg.
    caplog.set_level(logging.INFO)
    webp = queue_photo(fake, b"RIFF....WEBP", name="saved-from-web.jpg")

    def refuse(src: Path, dst: Path, settings: Settings) -> None:
        raise UnsupportedImageError(f"{src.name} is WEBP; only JPEG and PNG are enhanced")

    assert run.main(enhance=refuse) == 0
    assert fake.tag_names(webp) == {"enhance-skipped"}
    assert fake.uploads == []
    assert any("WEBP" in r.getMessage() and r.levelno == logging.INFO for r in caplog.records), (
        caplog.text
    )
    summary = summary_of(capsys)
    assert (summary["skipped"], summary["failed"], summary["exit_code"]) == (1, 0, 0)


NOTHING_TO_ENHANCE = "nothing to enhance: larger than 4 MP and no face found"


def returning(result: object) -> Callable[[Path, Path, Settings], object]:
    """A pipeline stand-in that writes an output and answers `result`."""

    def enhance(src: Path, dst: Path, settings: Settings) -> object:
        dst.write_bytes(enhanced(src.read_bytes()))
        return result

    return enhance


def test_a_x1_result_with_no_faces_is_skipped_not_stacked(fake, capsys, caplog):
    # Over 4 MP the pipeline only restores faces. With none found, its output is
    # a re-encode of the original, and stacked on top it would hide the better file.
    caplog.set_level(logging.INFO)
    big = queue_photo(fake, b"over-4-MP-and-no-face")
    plain = SimpleNamespace(
        scale=1, faces=0, in_size=(5000, 4000), out_size=(5000, 4000), seconds=40.0
    )
    assert run.main(enhance=returning(plain)) == 0
    assert fake.tag_names(big) == {"enhance-skipped"}
    assert fake.uploads == []
    assert fake.stacks == []
    assert any(NOTHING_TO_ENHANCE in r.getMessage() for r in caplog.records), caplog.text
    summary = summary_of(capsys)
    assert (summary["processed"], summary["skipped"], summary["succeeded"]) == (1, 1, 0)
    assert (summary["failed"], summary["exit_code"]) == (0, 0)


@pytest.mark.parametrize(
    "result",
    [
        SimpleNamespace(scale=1, faces=2),  # faces restored: worth having
        SimpleNamespace(scale=2, faces=0),  # upscaled: worth having
        object(),  # nothing to judge by: uploaded as usual
    ],
    ids=["x1-with-faces", "x2-without-faces", "no-attributes"],
)
def test_any_other_result_is_still_stacked(fake, capsys, result):
    photo = queue_photo(fake, b"photo")
    assert run.main(enhance=returning(result)) == 0
    assert fake.tag_names(photo) == {"enhanced-source"}
    assert len(fake.uploads) == 1
    assert len(fake.stacks) == 1


def test_dry_run_logs_the_nothing_to_enhance_decision(fake, capsys, caplog, monkeypatch):
    monkeypatch.setenv("ENHANCER_DRY_RUN", "true")
    caplog.set_level(logging.INFO)
    big = queue_photo(fake, b"over-4-MP-and-no-face")
    assert run.main(enhance=returning(SimpleNamespace(scale=1, faces=0))) == 0
    assert any(NOTHING_TO_ENHANCE in r.getMessage() for r in caplog.records), caplog.text
    assert changes(fake) == []
    assert fake.tag_names(big) == {"enhance"}
    assert summary_of(capsys)["skipped"] == 1


# -- failures and exit codes (decision 12) ---------------------------------------


def test_a_failed_enhancement_is_parked_and_the_next_asset_still_runs(fake, capsys, tmp_path):
    bad = queue_photo(fake, b"bad")
    good = queue_photo(fake, b"good")
    stub = Stub(fail={b"bad"})
    assert run.main(enhance=stub) == 0
    assert sorted(stub.calls) == [b"bad", b"good"]
    assert fake.tag_names(bad) == {"enhance-failed"}
    assert fake.tag_names(good) == {"enhanced-source"}
    assert list((tmp_path / "work").iterdir()) == []
    summary = summary_of(capsys)
    assert (summary["processed"], summary["succeeded"], summary["failed"]) == (2, 1, 1)
    assert summary["exit_code"] == 0


def test_a_run_where_every_attempt_failed_exits_1(fake, capsys):
    first = queue_photo(fake, b"one")
    second = queue_photo(fake, b"two")
    assert run.main(enhance=Stub(fail={b"one", b"two"})) == 1
    assert fake.tag_names(first) == {"enhance-failed"}
    assert fake.tag_names(second) == {"enhance-failed"}
    summary = summary_of(capsys)
    assert (summary["failed"], summary["exit_code"]) == (2, 1)


def test_a_retry_that_succeeds_drops_the_old_failed_tag(fake, capsys):
    retried = queue_photo(fake, b"photo", tags=("enhance", "enhance-failed"))
    assert run.main(enhance=Stub()) == 0
    assert fake.tag_names(retried) == {"enhanced-source"}


def test_a_failing_ping_exits_1_before_any_asset_is_touched(fake, capsys):
    queued = queue_photo(fake, b"photo")
    fake.ping_status = 503
    stub = Stub()
    assert run.main(enhance=stub) == 1
    assert fake.calls() == ["GET /api/server/ping"] * 6
    assert fake.tag_names(queued) == {"enhance"}
    assert stub.calls == []
    summary = summary_of(capsys)
    assert summary["exit_code"] == 1
    assert "ping" in summary["error"]
    assert summary["processed"] == 0


def test_a_missing_variable_exits_1_with_a_summary_and_no_request(fake, capsys, monkeypatch):
    monkeypatch.delenv("APP_VERSION")
    assert run.main(enhance=Stub()) == 1
    assert fake.log == []
    summary = summary_of(capsys)
    assert summary["exit_code"] == 1
    assert "APP_VERSION" in summary["error"]


def test_a_rejected_key_exits_1_naming_the_call(fake, capsys, monkeypatch):
    queued = queue_photo(fake, b"photo")
    monkeypatch.setenv("IMMICH_API_KEY", "not-the-key")
    assert run.main(enhance=Stub()) == 1
    assert fake.tag_names(queued) == {"enhance"}
    out = capsys.readouterr().out
    assert "not-the-key" not in out
    summary = json.loads([line for line in out.splitlines() if line[:1] == "{"][-1])
    assert "key_permissions" in summary["error"]
    assert "API key" in summary["error"]


def test_a_key_missing_a_permission_exits_1_before_any_asset_is_touched(fake, capsys):
    queued = queue_photo(fake, b"photo")
    fake.permissions = [p for p in NEEDED if p not in {"albumAsset.create", "tag.asset"}]
    assert run.main(enhance=Stub()) == 1
    assert fake.calls() == [
        "GET /api/server/ping",
        "GET /api/server/version",
        "GET /api/api-keys/me",
    ]
    assert fake.tag_names(queued) == {"enhance"}
    error = summary_of(capsys)["error"]
    assert "albumAsset.create" in error
    assert "tag.asset" in error


def test_a_permission_refused_mid_run_stops_the_run_and_exits_1(fake, capsys):
    # A server that cannot say what the key may do: the refusal comes from the
    # call itself, and it is about the key, not the photo.
    fake.permissions = None
    first = queue_photo(fake, b"one")
    second = queue_photo(fake, b"two")
    fake.refusals.append(("POST", r"/api/stacks", 403, "Missing required permission: stack.create"))
    stub = Stub()
    assert run.main(enhance=stub) == 1
    assert stub.calls == [b"one"]
    # Neither photo is parked as failed: both wait for a fixed key.
    assert fake.tag_names(first) == {"enhance"}
    assert fake.tag_names(second) == {"enhance"}
    error = summary_of(capsys)["error"]
    assert "create_stack" in error
    assert "stack.create" in error


# -- duplicates (decision 11) -----------------------------------------------------


def test_an_upload_immich_already_has_is_finished_not_failed(fake, capsys):
    original = queue_photo(fake, b"photo")
    album = fake.add_album("Summer 2004", [original])
    # An earlier run uploaded these enhanced bytes, then died before stacking.
    earlier = fake.add_asset(
        enhanced(b"photo"),
        filename="photo-enhanced.jpg",
        file_created_at=FILE_CREATED_AT,
        file_modified_at=FILE_MODIFIED_AT,
    )
    assert run.main(enhance=Stub()) == 0
    assert len(fake.assets) == 2
    assert [s["assetIds"] for s in fake.stacks] == [[earlier, original]]
    assert fake.tag_names(earlier) == {"enhanced"}
    assert fake.tag_names(original) == {"enhanced-source"}
    assert earlier in fake.albums[album]["assetIds"]
    # Looked up by checksum as Immich gives it: base64 of the SHA-1.
    searches = [e.get("json") for e in fake.log if e["path"] == "/api/search/metadata"]
    assert {"checksum": immich_checksum(enhanced(b"photo"))} in searches
    assert summary_of(capsys)["succeeded"] == 1


def test_a_duplicate_that_search_cannot_see_is_parked_as_failed(fake, capsys, caplog):
    # Immich's duplicate check counts trashed assets; its search does not.
    original = queue_photo(fake, b"photo")
    trashed = fake.add_asset(
        enhanced(b"photo"),
        filename="photo-enhanced.jpg",
        file_created_at=FILE_CREATED_AT,
        file_modified_at=FILE_MODIFIED_AT,
    )
    fake.in_trash.add(trashed)
    assert run.main(enhance=Stub()) == 1
    assert fake.tag_names(original) == {"enhance-failed"}
    assert fake.stacks == []
    assert "trash" in caplog.text


# -- limits ------------------------------------------------------------------------


def test_max_per_run_stops_the_loop(fake, capsys, monkeypatch):
    monkeypatch.setenv("ENHANCER_MAX_PER_RUN", "2")
    photos = [queue_photo(fake, f"photo-{i}".encode()) for i in range(3)]
    stub = Stub()
    assert run.main(enhance=stub) == 0
    assert len(stub.calls) == 2
    assert [fake.tag_names(p) for p in photos] == [
        {"enhanced-source"},
        {"enhanced-source"},
        {"enhance"},
    ]
    summary = summary_of(capsys)
    assert (summary["processed"], summary["succeeded"]) == (2, 2)
    assert summary["stopped"] == "max_per_run"


def test_the_time_budget_stops_new_assets_from_starting(fake, capsys, monkeypatch):
    monkeypatch.setenv("ENHANCER_TIME_BUDGET_S", "100")
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(run, "monotonic", lambda: clock.now)

    def a_minute_passes() -> None:
        clock.now += 60

    photos = [queue_photo(fake, f"photo-{i}".encode()) for i in range(3)]
    stub = Stub(on_call=a_minute_passes)
    assert run.main(enhance=stub) == 0
    # Started at 0 s and 60 s; at 120 s the budget is spent, so the third waits.
    assert len(stub.calls) == 2
    assert fake.tag_names(photos[2]) == {"enhance"}
    summary = summary_of(capsys)
    assert summary["stopped"] == "time_budget"
    assert summary["exit_code"] == 0


def test_dry_run_reads_and_enhances_but_changes_nothing(fake, capsys, monkeypatch):
    monkeypatch.setenv("ENHANCER_DRY_RUN", "true")
    photo = queue_photo(fake, b"photo")
    video = queue_photo(fake, b"video", mime="video/mp4", type="VIDEO")
    stray = queue_photo(fake, b"done", tags=("enhance", "enhanced-source"))
    fake.add_album("Summer 2004", [photo])
    stub = Stub()
    assert run.main(enhance=stub) == 0
    assert stub.calls == [b"photo"]
    assert changes(fake) == []
    assert fake.uploads == []
    assert fake.stacks == []
    assert fake.tag_names(photo) == {"enhance"}
    assert fake.tag_names(video) == {"enhance"}
    assert fake.tag_names(stray) == {"enhance", "enhanced-source"}
    summary = summary_of(capsys)
    assert summary["dry_run"] is True
    assert (summary["succeeded"], summary["skipped"]) == (1, 2)


# -- the regression to fear: does the copy land where the original is? ----------------


def test_timeline_drift_is_warned_with_both_values_and_the_asset_still_succeeds(
    fake, capsys, caplog
):
    original = queue_photo(fake, b"photo", localDateTime=LOCAL_DATE_TIME)

    def drift(asset_id: str, detail: dict) -> dict:
        if asset_id != original:
            detail["localDateTime"] = "2004-06-12T13:03:00.000Z"
        return detail

    fake.on_detail = drift
    assert run.main(enhance=Stub()) == 0
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    drifted = [w for w in warnings if "localDateTime" in w]
    assert len(drifted) == 1, warnings
    assert LOCAL_DATE_TIME in drifted[0]
    assert "2004-06-12T13:03:00.000Z" in drifted[0]
    assert not [w for w in warnings if "fileCreatedAt" in w]
    assert summary_of(capsys)["succeeded"] == 1


def test_the_timeline_check_waits_for_immich_to_read_the_new_file(
    fake, capsys, caplog, monkeypatch
):
    # Straight after an upload Immich has set localDateTime = fileCreatedAt and
    # not read the file yet; comparing then would cry wolf.
    original = queue_photo(
        fake,
        b"photo",
        localDateTime=LOCAL_DATE_TIME,
        exifInfo={"dateTimeOriginal": "2004-06-12T13:03:00+00:00"},
    )
    sleeps: list[float] = []
    monkeypatch.setattr(run, "sleep", sleeps.append)

    def extraction(asset_id: str, detail: dict) -> dict:
        if asset_id == original:
            return detail
        reads = fake.calls().count(f"GET /api/assets/{asset_id}")
        if reads < 3:
            detail.update(
                localDateTime=detail["fileCreatedAt"],
                exifInfo={"dateTimeOriginal": None, "fileSizeInByte": 17},
            )
        else:
            detail.update(
                localDateTime=LOCAL_DATE_TIME,
                exifInfo={"dateTimeOriginal": "2004-06-12T13:03:00+00:00"},
            )
        return detail

    fake.on_detail = extraction
    assert run.main(enhance=Stub()) == 0
    assert len(sleeps) == 2
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_timeline_check_that_cannot_finish_says_so(fake, capsys, caplog):
    original = queue_photo(fake, b"photo")

    def never_read(asset_id: str, detail: dict) -> dict:
        if asset_id != original:
            detail["exifInfo"] = {"dateTimeOriginal": None}
        return detail

    fake.on_detail = never_read
    assert run.main(enhance=Stub()) == 0
    (new,) = [asset_id for asset_id in fake.assets if asset_id != original]
    assert fake.calls().count(f"GET /api/assets/{new}") == run.TIMELINE_POLLS + 1
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert new in warnings[0]
    assert "metadata" in warnings[0]
