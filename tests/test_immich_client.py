"""ImmichClient against a recording server: every call in the plan's table, the
exact request each one sends, and what it makes of the answer.

The shapes are Immich's OpenAPI at v3.3.5, the deployed version, which are the
ones the contract fake in test_contract.py speaks. Ping and version are public
in Immich; every other call needs the permission named in its test.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import socket
from dataclasses import dataclass, field

import pytest
from werkzeug import Request, Response

from immich_enhancer.immich import ImmichAuthError, ImmichClient, ImmichError

API_KEY = "client-test-key"
FILE_CREATED_AT = "2004-06-12T13:03:00.000Z"
FILE_MODIFIED_AT = "2011-03-02T09:15:27.000Z"


def reply(status: int, payload: object = None) -> Response:
    return Response(json.dumps(payload), status=status, content_type="application/json")


@dataclass
class Recorder:
    """Records every request in full and answers from a table of canned replies.

    A route answers its replies in order; the last one repeats. A request with
    no route answers 599, so a stray call cannot pass for a handled one.
    """

    replies: dict[tuple[str, str], list[Response]] = field(default_factory=dict)
    requests: list[dict] = field(default_factory=list)

    def route(self, method: str, path: str, *responses: Response) -> None:
        self.replies.setdefault((method, path), []).extend(responses)

    def __call__(self, request: Request) -> Response:
        entry: dict = {"method": request.method, "path": request.path}
        if request.args:
            entry["query"] = request.args.to_dict()
        if request.mimetype == "application/json":
            entry["json"] = request.get_json()
        if request.mimetype == "multipart/form-data":
            entry["form"] = request.form.to_dict()
            entry["files"] = {
                name: {"filename": part.filename, "type": part.mimetype, "data": part.read()}
                for name, part in request.files.items()
            }
        entry["key"] = request.headers.get("x-api-key")
        self.requests.append(entry)
        queue = self.replies.get((request.method, request.path))
        if not queue:
            return reply(599, {"message": f"no route for {request.method} {request.path}"})
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def sent(self) -> list[dict]:
        """The requests without the key header, which has its own test."""
        return [{k: v for k, v in e.items() if k != "key"} for e in self.requests]


@pytest.fixture
def server(httpserver) -> Recorder:
    recorder = Recorder()
    httpserver.expect_request(re.compile(r"^/.*$")).respond_with_handler(recorder)
    return recorder


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def client(httpserver, sleeps):
    # A trailing slash on the URL must not matter.
    with ImmichClient(httpserver.url_for("/"), API_KEY, sleep=sleeps.append) as c:
        yield c


# -- transport --------------------------------------------------------------


def test_every_request_goes_under_api_and_carries_the_key(client, server):
    server.route("GET", "/api/server/ping", reply(200, {"res": "pong"}))
    server.route("GET", "/api/assets/a1", reply(200, {"id": "a1"}))
    client.ping()
    client.asset("a1")
    assert [(e["method"], e["path"], e["key"]) for e in server.requests] == [
        ("GET", "/api/server/ping", API_KEY),
        ("GET", "/api/assets/a1", API_KEY),
    ]


# -- ping, retried 6 x 5 s ---------------------------------------------------


def test_ping_retries_until_pong(client, server, sleeps):
    server.route(
        "GET",
        "/api/server/ping",
        reply(503, {"message": "starting"}),
        reply(503, {"message": "starting"}),
        reply(200, {"res": "pong"}),
    )
    client.ping()
    assert len(server.requests) == 3
    assert sleeps == [5.0, 5.0]


def test_a_pong_is_logged(client, server, caplog):
    caplog.set_level(logging.INFO)
    server.route("GET", "/api/server/ping", reply(200, {"res": "pong"}))
    client.ping()
    assert "Immich answered ping" in caplog.text


def test_ping_gives_up_after_six_tries(client, server, sleeps):
    server.route("GET", "/api/server/ping", reply(503, {"message": "starting"}))
    with pytest.raises(ImmichError) as raised:
        client.ping()
    assert len(server.requests) == 6
    assert sleeps == [5.0] * 5
    assert "ping" in str(raised.value)
    assert "503" in str(raised.value)


def test_ping_retries_a_connection_that_never_opens(sleeps):
    # A port nothing listens on: what a pod sees before its netpol is programmed
    # is a drop rather than a refusal, but both are transport errors.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with ImmichClient(f"http://127.0.0.1:{port}", API_KEY, sleep=sleeps.append) as c:
        with pytest.raises(ImmichError) as raised:
            c.ping()
    assert sleeps == [5.0] * 5
    assert "ping" in str(raised.value)


def test_ping_does_not_retry_a_client_error(client, server, sleeps):
    # A 404 means IMMICH_URL is wrong; waiting will not fix it.
    server.route("GET", "/api/server/ping", reply(404, {"message": "Not Found"}))
    with pytest.raises(ImmichError):
        client.ping()
    assert len(server.requests) == 1
    assert sleeps == []


# -- version -----------------------------------------------------------------


def test_version_is_logged(client, server, caplog):
    caplog.set_level(logging.INFO)
    server.route(
        "GET",
        "/api/server/version",
        reply(200, {"major": 3, "minor": 3, "patch": 5, "prerelease": None}),
    )
    assert client.version()["major"] == 3
    assert "3.3.5" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_version_warns_when_the_major_is_not_3(client, server, caplog):
    server.route(
        "GET",
        "/api/server/version",
        reply(200, {"major": 4, "minor": 0, "patch": 1, "prerelease": None}),
    )
    client.version()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "4.0.1" in warnings[0]
    assert "v3" in warnings[0]


# -- the key's own permissions -------------------------------------------------


def test_key_permissions(client, server):
    server.route(
        "GET",
        "/api/api-keys/me",
        reply(
            200, {"id": "k1", "name": "immich-enhancer", "permissions": ["asset.read", "tag.asset"]}
        ),
    )
    assert client.key_permissions() == ["asset.read", "tag.asset"]
    assert server.sent() == [{"method": "GET", "path": "/api/api-keys/me"}]


# -- tags --------------------------------------------------------------------


def test_upsert_tags_maps_each_name_to_its_id(client, server):
    names = ["enhance", "enhanced", "photos/enhance-failed"]
    server.route(
        "PUT",
        "/api/tags",
        reply(
            200,
            [
                {"id": "t1", "name": "enhance", "value": "enhance"},
                {"id": "t2", "name": "enhanced", "value": "enhanced"},
                # Immich answers the leaf: `name` is its last segment, `value` the path.
                {
                    "id": "t3",
                    "name": "enhance-failed",
                    "value": "photos/enhance-failed",
                    "parentId": "t0",
                },
            ],
        ),
    )
    assert client.upsert_tags(names) == {
        "enhance": "t1",
        "enhanced": "t2",
        "photos/enhance-failed": "t3",
    }
    assert server.sent() == [{"method": "PUT", "path": "/api/tags", "json": {"tags": names}}]


def test_upsert_tags_fails_when_a_name_gets_no_id(client, server):
    server.route(
        "PUT", "/api/tags", reply(200, [{"id": "t1", "name": "enhance", "value": "enhance"}])
    )
    with pytest.raises(ImmichError) as raised:
        client.upsert_tags(["enhance", "enhanced"])
    assert "enhanced" in str(raised.value)


def test_tag_and_untag_assets(client, server):
    server.route("PUT", "/api/tags/t1/assets", reply(200, [{"id": "a1", "success": True}]))
    # Removing a tag an asset does not carry answers not_found: not an error.
    server.route(
        "DELETE",
        "/api/tags/t1/assets",
        reply(200, [{"id": "a1", "success": False, "error": "not_found"}]),
    )
    client.tag_assets("t1", ["a1"])
    client.untag_assets("t1", ["a1"])
    assert server.sent() == [
        {"method": "PUT", "path": "/api/tags/t1/assets", "json": {"ids": ["a1"]}},
        {"method": "DELETE", "path": "/api/tags/t1/assets", "json": {"ids": ["a1"]}},
    ]


def test_tagging_an_already_tagged_asset_is_not_an_error(client, server):
    server.route(
        "PUT",
        "/api/tags/t1/assets",
        reply(200, [{"id": "a1", "success": False, "error": "duplicate"}]),
    )
    client.tag_assets("t1", ["a1"])


def test_a_refused_id_in_a_bulk_answer_is_an_error(client, server):
    server.route(
        "PUT",
        "/api/tags/t1/assets",
        reply(200, [{"id": "a1", "success": False, "error": "no_permission"}]),
    )
    with pytest.raises(ImmichError) as raised:
        client.tag_assets("t1", ["a1"])
    assert "tag_assets" in str(raised.value)
    assert "no_permission" in str(raised.value)


# -- the queue -----------------------------------------------------------------


def page(ids: list[str], next_page: str | None) -> Response:
    items = [{"id": i, "type": "IMAGE"} for i in ids]
    return reply(
        200,
        {
            "albums": {"total": 0, "count": 0, "items": [], "facets": []},
            "assets": {
                "total": len(items),
                "count": len(items),
                "items": items,
                "facets": [],
                "nextPage": next_page,
                "nextCursor": None,
            },
        },
    )


def test_search_by_tag_reads_every_page_first(client, server):
    server.route("POST", "/api/search/metadata", page(["a1", "a2"], "2"), page(["a2", "a3"], None))
    items = client.search_assets_by_tag("t1")
    # a2 moved across the page boundary: it comes back once.
    assert [item["id"] for item in items] == ["a1", "a2", "a3"]
    body = {"tagIds": ["t1"], "withStacked": True, "size": 100}
    assert server.sent() == [
        {"method": "POST", "path": "/api/search/metadata", "json": {**body, "page": 1}},
        {"method": "POST", "path": "/api/search/metadata", "json": {**body, "page": 2}},
    ]


def test_search_by_tag_refuses_a_page_that_does_not_advance(client, server):
    server.route("POST", "/api/search/metadata", page(["a1"], "1"))
    with pytest.raises(ImmichError):
        client.search_assets_by_tag("t1")


def test_find_by_checksum(client, server):
    checksum = base64.b64encode(hashlib.sha1(b"enhanced").digest()).decode()
    server.route("POST", "/api/search/metadata", page(["dup"], None))
    assert [item["id"] for item in client.find_by_checksum(checksum)] == ["dup"]
    assert server.sent() == [
        {"method": "POST", "path": "/api/search/metadata", "json": {"checksum": checksum}}
    ]


# -- one asset -----------------------------------------------------------------


def test_asset_detail(client, server):
    detail = {"id": "a1", "type": "IMAGE", "stack": None, "tags": []}
    server.route("GET", "/api/assets/a1", reply(200, detail))
    assert client.asset("a1") == detail
    assert server.sent() == [{"method": "GET", "path": "/api/assets/a1"}]


def test_download_streams_the_original_to_disk(client, server, tmp_path):
    data = bytes(range(256)) * 4096  # 1 MiB, more than one chunk
    server.route(
        "GET", "/api/assets/a1/original", Response(data, status=200, content_type="image/jpeg")
    )
    dest = tmp_path / "a1.jpg"
    assert client.download("a1", dest) == len(data)
    assert dest.read_bytes() == data
    # No `edited` parameter: the original as uploaded, never Immich's edited render.
    assert server.sent() == [{"method": "GET", "path": "/api/assets/a1/original"}]


def test_download_failure_names_the_call_and_writes_nothing(client, server, tmp_path):
    server.route("GET", "/api/assets/a1/original", reply(400, {"message": "Asset not found"}))
    dest = tmp_path / "a1.jpg"
    with pytest.raises(ImmichError) as raised:
        client.download("a1", dest)
    assert "download" in str(raised.value)
    assert "Asset not found" in str(raised.value)
    assert not dest.exists()


@pytest.mark.parametrize(("favorite", "sent_as"), [(True, "true"), (False, "false")])
def test_upload_sends_the_multipart_form(client, server, tmp_path, favorite, sent_as):
    path = tmp_path / "out.jpg"
    path.write_bytes(b"\xff\xd8enhanced")
    server.route("POST", "/api/assets", reply(201, {"id": "new", "status": "created"}))
    answer = client.upload(
        path,
        filename="face-enhanced.jpg",
        file_created_at=FILE_CREATED_AT,
        file_modified_at=FILE_MODIFIED_AT,
        is_favorite=favorite,
    )
    assert answer == {"id": "new", "status": "created"}
    assert server.sent() == [
        {
            "method": "POST",
            "path": "/api/assets",
            "form": {
                "filename": "face-enhanced.jpg",
                "fileCreatedAt": FILE_CREATED_AT,
                "fileModifiedAt": FILE_MODIFIED_AT,
                "isFavorite": sent_as,
            },
            "files": {
                "assetData": {
                    "filename": "face-enhanced.jpg",
                    "type": "image/jpeg",
                    "data": b"\xff\xd8enhanced",
                }
            },
        }
    ]


def test_upload_returns_a_duplicate_as_immich_answers_it(client, server, tmp_path):
    path = tmp_path / "out.jpg"
    path.write_bytes(b"\xff\xd8enhanced")
    server.route("POST", "/api/assets", reply(200, {"id": "dup", "status": "duplicate"}))
    answer = client.upload(
        path,
        filename="face-enhanced.jpg",
        file_created_at=FILE_CREATED_AT,
        file_modified_at=FILE_MODIFIED_AT,
        is_favorite=False,
    )
    assert answer == {"id": "dup", "status": "duplicate"}


def test_create_stack_puts_the_first_id_on_top(client, server):
    server.route(
        "POST", "/api/stacks", reply(201, {"id": "s1", "primaryAssetId": "new", "assets": []})
    )
    assert client.create_stack(["new", "orig"])["primaryAssetId"] == "new"
    assert server.sent() == [
        {"method": "POST", "path": "/api/stacks", "json": {"assetIds": ["new", "orig"]}}
    ]


def test_albums_for_asset_filters_by_asset(client, server):
    server.route("GET", "/api/albums", reply(200, [{"id": "al1", "albumName": "Summer 2004"}]))
    assert client.albums_for_asset("orig") == [{"id": "al1", "albumName": "Summer 2004"}]
    assert server.sent() == [{"method": "GET", "path": "/api/albums", "query": {"assetId": "orig"}}]


def test_add_to_album(client, server):
    server.route("PUT", "/api/albums/al1/assets", reply(200, [{"id": "new", "success": True}]))
    client.add_to_album("al1", ["new"])
    assert server.sent() == [
        {"method": "PUT", "path": "/api/albums/al1/assets", "json": {"ids": ["new"]}}
    ]


def test_adding_an_asset_already_in_the_album_is_not_an_error(client, server):
    server.route(
        "PUT",
        "/api/albums/al1/assets",
        reply(200, [{"id": "new", "success": False, "error": "duplicate"}]),
    )
    client.add_to_album("al1", ["new"])


# -- errors ------------------------------------------------------------------


def test_403_names_the_call_and_the_missing_permission(client, server):
    server.route(
        "PUT",
        "/api/tags/t1/assets",
        reply(
            403,
            {
                "message": "Missing required permission: tag.asset",
                "error": "Forbidden",
                "statusCode": 403,
            },
        ),
    )
    with pytest.raises(ImmichAuthError) as raised:
        client.tag_assets("t1", ["a1"])
    message = str(raised.value)
    assert "tag_assets" in message
    assert "tag.asset" in message
    assert "API key lacks" in message
    assert API_KEY not in message
    assert raised.value.status == 403


def test_401_says_the_key_was_rejected(client, server):
    server.route("PUT", "/api/tags", reply(401, {"message": "Invalid API key"}))
    with pytest.raises(ImmichAuthError) as raised:
        client.upsert_tags(["enhance"])
    message = str(raised.value)
    assert "upsert_tags" in message
    assert "API key" in message
    assert API_KEY not in message


def test_a_server_error_is_an_immich_error_but_not_an_auth_error(client, server):
    server.route("POST", "/api/stacks", reply(500, {"message": "Internal server error"}))
    with pytest.raises(ImmichError) as raised:
        client.create_stack(["new", "orig"])
    assert not isinstance(raised.value, ImmichAuthError)
    assert "create_stack" in str(raised.value)
    assert "500" in str(raised.value)
