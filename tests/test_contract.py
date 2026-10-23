"""The red pin: one tagged photo in, one stacked and enhanced sibling out.

A fake Immich (pytest-httpserver) holds one `enhance`-tagged 160x120 JPEG that
carries `DateTimeOriginal 2004:06:12 14:03:00` and `Orientation 6`, in one album.
After `run.main()`:

- the fake holds two assets, and the original's bytes were never PUT, PATCHed
  or DELETEd;
- the new asset is a JPEG, 480x640 (rotated upright, x4), `Orientation 1`, with
  the same `DateTimeOriginal`, uploaded with the original's `fileCreatedAt` and
  `fileModifiedAt`;
- exactly one stack exists, with `assetIds == [new, original]`;
- the original carries `enhanced-source` and not `enhance`; the new asset
  carries `enhanced`;
- the new asset was added to the original's one album.

The test runs the real pipeline, so the weights must be in
`ENHANCER_MODEL_DIR` (CI fetches them; locally see the README).
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import piexif
from PIL import Image
from werkzeug import Request, Response

FIXTURE = Path(__file__).parent / "fixtures" / "face-160x120.jpg"
API_KEY = "contract-test-key"
DATE_TIME_ORIGINAL = b"2004:06:12 14:03:00"
# These differ from each other and from "now", so a swapped, recomputed or
# defaulted timestamp cannot pass.
FILE_CREATED_AT = "2004-06-12T13:03:00.000Z"
FILE_MODIFIED_AT = "2011-03-02T09:15:27.000Z"


def immich_checksum(data: bytes) -> str:
    """Immich identifies a file by base64(sha1(bytes))."""
    return base64.b64encode(hashlib.sha1(data).digest()).decode()


def tagged_fixture() -> bytes:
    """The fixture with the EXIF the plan describes, written with piexif."""
    exif = piexif.dump(
        {
            "0th": {piexif.ImageIFD.Orientation: 6},
            "Exif": {piexif.ExifIFD.DateTimeOriginal: DATE_TIME_ORIGINAL},
        }
    )
    out = io.BytesIO()
    piexif.insert(exif, FIXTURE.read_bytes(), out)
    return out.getvalue()


@dataclass
class FakeImmich:
    """In-memory Immich, covering the plan's call table and nothing else.

    Routes live under /api and require the `x-api-key` header. Any other route
    answers 404 and is recorded in `unrouted`.
    """

    api_key: str
    assets: dict[str, dict] = field(default_factory=dict)
    files: dict[str, bytes] = field(default_factory=dict)
    tags: dict[str, dict] = field(default_factory=dict)
    tag_members: dict[str, list[str]] = field(default_factory=dict)
    albums: dict[str, dict] = field(default_factory=dict)
    stacks: list[dict] = field(default_factory=list)
    uploads: list[dict] = field(default_factory=list)
    log: list[dict] = field(default_factory=list)
    unrouted: list[str] = field(default_factory=list)

    # -- seeding -----------------------------------------------------------

    def ensure_tag(self, name: str) -> str:
        for tag_id, tag in self.tags.items():
            if tag["value"] == name:
                return tag_id
        tag_id = str(uuid.uuid4())
        self.tags[tag_id] = {
            "id": tag_id,
            "name": name,
            "value": name,
            "createdAt": "2026-10-23T00:00:00.000Z",
            "updatedAt": "2026-10-23T00:00:00.000Z",
        }
        self.tag_members[tag_id] = []
        return tag_id

    def add_asset(
        self,
        data: bytes,
        *,
        filename: str,
        file_created_at: str,
        file_modified_at: str,
        mime: str = "image/jpeg",
        favorite: bool = False,
    ) -> str:
        asset_id = str(uuid.uuid4())
        self.files[asset_id] = data
        self.assets[asset_id] = {
            "originalFileName": filename,
            "originalMimeType": mime,
            "fileCreatedAt": file_created_at,
            "fileModifiedAt": file_modified_at,
            "isFavorite": favorite,
            "checksum": immich_checksum(data),
        }
        return asset_id

    def add_album(self, name: str, asset_ids: list[str]) -> str:
        album_id = str(uuid.uuid4())
        self.albums[album_id] = {"id": album_id, "albumName": name, "assetIds": list(asset_ids)}
        return album_id

    # -- views -------------------------------------------------------------

    def tag_names(self, asset_id: str) -> set[str]:
        return {
            self.tags[tag_id]["value"]
            for tag_id, members in self.tag_members.items()
            if asset_id in members
        }

    def detail(self, asset_id: str) -> dict:
        asset = self.assets[asset_id]
        stack = next((s for s in self.stacks if asset_id in s["assetIds"]), None)
        return {
            "id": asset_id,
            "type": "IMAGE",
            "ownerId": "owner-ben",
            "originalFileName": asset["originalFileName"],
            "originalMimeType": asset["originalMimeType"],
            "fileCreatedAt": asset["fileCreatedAt"],
            "fileModifiedAt": asset["fileModifiedAt"],
            "isFavorite": asset["isFavorite"],
            "isArchived": False,
            "isTrashed": False,
            "visibility": "timeline",
            "checksum": asset["checksum"],
            "tags": [
                self.tags[tag_id]
                for tag_id, members in self.tag_members.items()
                if asset_id in members
            ],
            "stack": None
            if stack is None
            else {
                "id": stack["id"],
                "primaryAssetId": stack["assetIds"][0],
                "assetCount": len(stack["assetIds"]),
            },
        }

    def summary(self) -> str:
        calls = [f"{e['method']} {e['path']}" for e in self.log]
        return f"requests={calls} unrouted={self.unrouted}"

    # -- HTTP --------------------------------------------------------------

    def dispatch(self, request: Request) -> Response:
        entry: dict = {"method": request.method, "path": request.path, "args": dict(request.args)}
        if request.mimetype == "application/json":
            entry["json"] = request.get_json(silent=True)
        self.log.append(entry)
        if request.headers.get("x-api-key") != self.api_key:
            return self._json(401, {"message": "Invalid API key"})
        routes = [
            ("GET", r"/api/server/ping", self._ping),
            ("GET", r"/api/server/version", self._version),
            ("PUT", r"/api/tags", self._upsert_tags),
            ("POST", r"/api/search/metadata", self._search_metadata),
            ("GET", r"/api/assets/([^/]+)/original", self._download),
            ("GET", r"/api/assets/([^/]+)", self._asset),
            ("POST", r"/api/assets", self._upload),
            ("POST", r"/api/stacks", self._create_stack),
            ("GET", r"/api/albums", self._albums),
            ("PUT", r"/api/albums/([^/]+)/assets", self._add_to_album),
            ("PUT", r"/api/tags/([^/]+)/assets", self._tag_assets),
            ("DELETE", r"/api/tags/([^/]+)/assets", self._untag_assets),
        ]
        for method, pattern, handler in routes:
            match = re.fullmatch(pattern, request.path)
            if match and request.method == method:
                return handler(request, *match.groups())
        self.unrouted.append(f"{request.method} {request.path}")
        return self._json(404, {"message": f"no fake route for {request.method} {request.path}"})

    @staticmethod
    def _json(status: int, payload: object) -> Response:
        return Response(json.dumps(payload), status=status, content_type="application/json")

    def _ping(self, request: Request) -> Response:
        return self._json(200, {"res": "pong"})

    def _version(self, request: Request) -> Response:
        return self._json(200, {"major": 3, "minor": 3, "patch": 5})

    def _upsert_tags(self, request: Request) -> Response:
        names = request.get_json()["tags"]
        return self._json(200, [self.tags[self.ensure_tag(name)] for name in names])

    def _search_metadata(self, request: Request) -> Response:
        query = request.get_json() or {}
        ids = list(self.assets)
        if query.get("tagIds"):
            ids = [a for a in ids if all(a in self.tag_members.get(t, []) for t in query["tagIds"])]
        if query.get("checksum"):
            ids = [a for a in ids if self.assets[a]["checksum"] == query["checksum"]]
        items = [self.detail(a) for a in ids]
        return self._json(
            200,
            {
                "albums": {"total": 0, "count": 0, "items": [], "facets": []},
                "assets": {
                    "total": len(items),
                    "count": len(items),
                    "items": items,
                    "facets": [],
                    "nextPage": None,
                },
            },
        )

    def _asset(self, request: Request, asset_id: str) -> Response:
        if asset_id not in self.assets:
            return self._json(404, {"message": "Asset not found"})
        return self._json(200, self.detail(asset_id))

    def _download(self, request: Request, asset_id: str) -> Response:
        if asset_id not in self.files:
            return self._json(404, {"message": "Asset not found"})
        mime = self.assets[asset_id]["originalMimeType"]
        return Response(self.files[asset_id], status=200, content_type=mime)

    def _upload(self, request: Request) -> Response:
        upload = request.files.get("assetData")
        if upload is None:
            return self._json(400, {"message": "assetData is required"})
        form = request.form.to_dict()
        for name in ("fileCreatedAt", "fileModifiedAt"):
            if name not in form:
                return self._json(400, {"message": f"{name} is required"})
        data = upload.read()
        self.uploads.append({"form": form, "filename": upload.filename, "data": data})
        checksum = immich_checksum(data)
        for asset_id, asset in self.assets.items():
            if asset["checksum"] == checksum:
                return self._json(200, {"id": asset_id, "status": "duplicate"})
        new_id = self.add_asset(
            data,
            filename=form.get("filename") or upload.filename or "upload.jpg",
            file_created_at=form["fileCreatedAt"],
            file_modified_at=form["fileModifiedAt"],
            mime=upload.mimetype or "image/jpeg",
            favorite=form.get("isFavorite") == "true",
        )
        return self._json(201, {"id": new_id, "status": "created"})

    def _create_stack(self, request: Request) -> Response:
        asset_ids = request.get_json()["assetIds"]
        if len(asset_ids) < 2 or any(a not in self.assets for a in asset_ids):
            return self._json(400, {"message": "a stack needs at least two known assets"})
        stack = {"id": str(uuid.uuid4()), "assetIds": list(asset_ids)}
        self.stacks.append(stack)
        return self._json(
            201,
            {
                "id": stack["id"],
                "primaryAssetId": asset_ids[0],
                "assets": [self.detail(a) for a in asset_ids],
            },
        )

    def _albums(self, request: Request) -> Response:
        asset_id = request.args.get("assetId")
        albums = [
            {"id": a["id"], "albumName": a["albumName"], "assetCount": len(a["assetIds"])}
            for a in self.albums.values()
            if asset_id is None or asset_id in a["assetIds"]
        ]
        return self._json(200, albums)

    def _add_to_album(self, request: Request, album_id: str) -> Response:
        if album_id not in self.albums:
            return self._json(404, {"message": "Album not found"})
        members = self.albums[album_id]["assetIds"]
        results = []
        for asset_id in request.get_json()["ids"]:
            if asset_id in members:
                results.append({"id": asset_id, "success": False, "error": "duplicate"})
            else:
                members.append(asset_id)
                results.append({"id": asset_id, "success": True})
        return self._json(200, results)

    def _tag_assets(self, request: Request, tag_id: str) -> Response:
        if tag_id not in self.tags:
            return self._json(404, {"message": "Tag not found"})
        members = self.tag_members[tag_id]
        results = []
        for asset_id in request.get_json()["ids"]:
            if asset_id not in members:
                members.append(asset_id)
            results.append({"id": asset_id, "success": True})
        return self._json(200, results)

    def _untag_assets(self, request: Request, tag_id: str) -> Response:
        if tag_id not in self.tags:
            return self._json(404, {"message": "Tag not found"})
        members = self.tag_members[tag_id]
        results = []
        for asset_id in request.get_json()["ids"]:
            if asset_id in members:
                members.remove(asset_id)
            results.append({"id": asset_id, "success": True})
        return self._json(200, results)


def test_tagged_asset_becomes_stacked_enhanced_sibling(httpserver, monkeypatch, tmp_path):
    original_bytes = tagged_fixture()
    fake = FakeImmich(api_key=API_KEY)
    enhance = fake.ensure_tag("enhance")
    original = fake.add_asset(
        original_bytes,
        filename="face-160x120.jpg",
        file_created_at=FILE_CREATED_AT,
        file_modified_at=FILE_MODIFIED_AT,
    )
    fake.tag_members[enhance].append(original)
    album = fake.add_album("Summer 2004", [original])
    httpserver.expect_request(re.compile(r"^/.*$")).respond_with_handler(fake.dispatch)

    # Only what this test decides; every other setting keeps its default.
    # ENHANCER_MODEL_DIR is left alone: it names where the weights are.
    for name in list(os.environ):
        if name.startswith("ENHANCER_") and name != "ENHANCER_MODEL_DIR":
            monkeypatch.delenv(name)
    monkeypatch.setenv("IMMICH_URL", httpserver.url_for("/").rstrip("/"))
    monkeypatch.setenv("IMMICH_API_KEY", API_KEY)
    monkeypatch.setenv("APP_VERSION", "contract-test")
    monkeypatch.setenv("ENHANCER_WORK_DIR", str(tmp_path / "work"))

    # Imported here, not at module top: until WP-B lands, this import is the
    # red, and it must fail the test rather than the collection.
    from immich_enhancer.run import main

    exit_code = main()
    httpserver.check_handler_errors()
    assert exit_code == 0, fake.summary()

    # Two assets; the original was never written to.
    assert len(fake.assets) == 2, fake.summary()
    (new,) = [asset_id for asset_id in fake.assets if asset_id != original]
    writes = [
        e
        for e in fake.log
        if e["method"] in {"PUT", "PATCH", "DELETE"}
        and e["path"].startswith("/api/assets")
        and (original in e["path"] or original in json.dumps(e.get("json")))
    ]
    assert writes == [], writes
    assert fake.files[original] == original_bytes
    assert fake.assets[original]["checksum"] == immich_checksum(original_bytes)

    # The new asset: JPEG, upright and x4, Orientation 1, same capture time.
    data = fake.files[new]
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "JPEG"
        assert image.size == (480, 640)
    exif = piexif.load(data)
    assert exif["0th"].get(piexif.ImageIFD.Orientation) == 1
    assert exif["Exif"].get(piexif.ExifIFD.DateTimeOriginal) == DATE_TIME_ORIGINAL

    # Uploaded with the original's timestamps, verbatim.
    new_checksum = fake.assets[new]["checksum"]
    uploads = [u for u in fake.uploads if immich_checksum(u["data"]) == new_checksum]
    assert uploads, fake.summary()
    for upload in uploads:
        assert upload["form"]["fileCreatedAt"] == FILE_CREATED_AT
        assert upload["form"]["fileModifiedAt"] == FILE_MODIFIED_AT

    # One stack, the enhanced asset first (primary).
    assert [s["assetIds"] for s in fake.stacks] == [[new, original]]

    # Tags moved: the original is the source now, the new asset is the result.
    assert "enhanced-source" in fake.tag_names(original)
    assert "enhance" not in fake.tag_names(original)
    assert "enhanced" in fake.tag_names(new)
    assert "enhance" not in fake.tag_names(new)

    # The new asset joined the original's one album.
    assert new in fake.albums[album]["assetIds"]
    assert original in fake.albums[album]["assetIds"]
