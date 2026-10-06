"""The calls to an embedding or chat API go over a connection that is kept.

Every call opened a connection of its own: a TCP and a TLS handshake before
each batch of an index run, and before each search of a server that stays up
for days. Measured against a real endpoint, a call on a new connection took
about 385 ms and one on a kept connection about 240 ms.

A kept connection can have been closed by the other end while it sat idle.
The calls already wait patiently when an API cannot be reached (ten or
twenty seconds between attempts); that wait is for a network that is down,
not for a connection that went stale, so a stale one is tried again at once
on a fresh connection.

These tests talk to a real HTTP server on the loopback interface: what is
being changed is what happens on the wire."""

import http.server
import json
import socketserver
import threading

import pytest
import requests

import conftest
from griot import common


class _Server:
    """An HTTP/1.1 server that counts connections and remembers requests."""

    def __init__(self, close_after_each=False, set_cookie=False, status=200):
        self.connections, self.requests = 0, []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                outer.connections += 1
                super().setup()

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.requests.append({"headers": dict(self.headers), "body": json.loads(body or b"{}")})
                answer = json.dumps({"data": [{"index": 0, "embedding": [0.5]}], "usage": {"total_tokens": 1},
                                     "embeddings": [{"values": [0.5]}]}).encode()
                self.send_response(status)
                if status in (301, 302, 307):
                    self.send_header("Location", "http://127.0.0.1:1/elsewhere")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(answer)))
                if set_cookie:
                    self.send_header("Set-Cookie", "session=tracked; Path=/")
                self.end_headers()
                self.wfile.write(answer)
                if close_after_each:
                    self.close_connection = True  # the other end hangs up without saying so

            def log_message(self, *args):
                pass

        class Quick(http.server.ThreadingHTTPServer):
            def server_bind(self):
                # HTTPServer.server_bind() asks the resolver for this
                # machine's full name, which took 35 s where reverse lookups
                # are slow. The name is never used here.
                socketserver.TCPServer.server_bind(self)
                self.server_name, self.server_port = "127.0.0.1", self.server_address[1]

        self.httpd = Quick(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1/embeddings"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    started = []

    def start(**kwargs):
        started.append(_Server(**kwargs))
        return started[-1]

    yield start
    for one in started:
        one.stop()


@pytest.fixture(autouse=True)
def real_session(request, monkeypatch):
    """The suite forbids a real HTTP session (see conftest); here it is the
    thing under test, and it only ever reaches 127.0.0.1."""
    if request.node.get_closest_marker("as_every_other_test"):
        yield
        return
    monkeypatch.setattr(common, "_new_http_session", conftest.REAL_NEW_HTTP_SESSION)
    yield
    common._drop_http_session()


def _no_waiting(monkeypatch):
    monkeypatch.setattr(common.time, "sleep", lambda seconds: pytest.fail(f"waited {seconds}s"))


# --- one connection for many calls ------------------------------------------------------------


def test_calls_to_the_same_host_share_a_connection(server):
    api = server()
    for _ in range(4):
        assert common._http_post(api.url, json={"input": ["x"]}, timeout=5, allow_redirects=False).status_code == 200
    assert len(api.requests) == 4 and api.connections == 1


def test_the_way_it_was_opened_one_per_call(server):
    """What `requests.post` does, for the record of what changed."""
    api = server()
    for _ in range(3):
        requests.post(api.url, json={}, timeout=5)
    assert api.connections == 3


def test_the_embedding_calls_of_an_openai_compatible_profile_share_it(server, monkeypatch):
    api = server()
    _no_waiting(monkeypatch)
    for _ in range(3):
        data = common._openai_compatible_post_with_retry(api.url, {"Authorization": "Bearer not-a-real-key"}, {"input": ["x"]})
        assert data["data"][0]["embedding"] == [0.5]
    assert api.connections == 1


def test_the_calls_to_gemini_share_it(server, monkeypatch):
    api = server()
    _no_waiting(monkeypatch)
    monkeypatch.setattr(common, "GEMINI_API_BASE", api.url.rsplit("/v1/", 1)[0])
    monkeypatch.setattr(common, "GEMINI_TOKEN", "not-a-real-token")
    for _ in range(3):
        assert common._gemini_post_with_retry("models/m:batchEmbedContents", {"requests": []})["embeddings"]
    assert api.connections == 1 and api.requests[0]["headers"]["x-goog-api-key"] == "not-a-real-token"


# --- a kept connection that went stale --------------------------------------------------------


def test_a_connection_the_other_end_closed_costs_no_wait(server, monkeypatch):
    api = server(close_after_each=True)
    _no_waiting(monkeypatch)
    for _ in range(3):
        common._openai_compatible_post_with_retry(api.url, {"Authorization": "Bearer not-a-real-key"}, {"input": ["x"]})
    assert len(api.requests) == 3


class _Flaky:
    """A session whose `post` fails as told, counting the sessions made."""

    def __init__(self, outcomes):
        self.outcomes, self.made, self.posts = list(outcomes), 0, 0

    def session(self):
        self.made += 1
        return self

    def post(self, url, **kwargs):
        self.posts += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self):
        pass


def _flaky(monkeypatch, outcomes):
    flaky = _Flaky(outcomes)
    monkeypatch.setattr(common, "_new_http_session", flaky.session)
    common._drop_http_session()
    return flaky


def test_after_a_call_that_worked_a_dropped_connection_is_tried_again_at_once(monkeypatch):
    flaky = _flaky(monkeypatch, ["ok", requests.ConnectionError("reset by peer"), "ok again"])
    assert common._http_post("http://example.invalid/") == "ok"
    assert common._http_post("http://example.invalid/") == "ok again"
    assert flaky.posts == 3 and flaky.made == 2, "on a fresh connection, not on the one that failed"


def test_a_host_that_cannot_be_reached_at_all_is_not_tried_twice_in_a_row(monkeypatch):
    """Nothing was kept, so nothing went stale: the patient wait of the
    caller is the right answer to a network that is down."""
    flaky = _flaky(monkeypatch, [requests.ConnectionError("no route to host"), "never reached"])
    with pytest.raises(requests.ConnectionError):
        common._http_post("http://example.invalid/")
    assert flaky.posts == 1


def test_a_second_failure_in_a_row_goes_to_the_caller(monkeypatch):
    flaky = _flaky(monkeypatch, ["ok", requests.ConnectionError("reset"), requests.ConnectionError("still down"), "x"])
    common._http_post("http://example.invalid/")
    with pytest.raises(requests.ConnectionError, match="still down"):
        common._http_post("http://example.invalid/")
    assert flaky.posts == 3


def test_a_call_that_timed_out_is_not_sent_again_at_once(monkeypatch):
    """It waited two minutes for an answer: sending it again would wait two
    more, and may bill the same request twice."""
    flaky = _flaky(monkeypatch, ["ok", requests.ReadTimeout("no answer"), "x"])
    common._http_post("http://example.invalid/")
    with pytest.raises(requests.Timeout):
        common._http_post("http://example.invalid/")
    assert flaky.posts == 2
    flaky = _flaky(monkeypatch, ["ok", requests.ConnectTimeout("no connection"), "x"])
    common._http_post("http://example.invalid/")
    with pytest.raises(requests.Timeout):
        common._http_post("http://example.invalid/")
    assert flaky.posts == 2


@pytest.mark.parametrize("error", [requests.exceptions.SSLError("certificate verify failed"),
                                   requests.exceptions.ProxyError("proxy refused")])
def test_a_failure_that_would_repeat_is_not_sent_again_at_once(monkeypatch, error):
    """A certificate that does not verify, a proxy that refuses: the same
    on a fresh connection as on a kept one."""
    flaky = _flaky(monkeypatch, ["ok", error, "x"])
    common._http_post("http://example.invalid/")
    with pytest.raises(requests.ConnectionError):
        common._http_post("http://example.invalid/")
    assert flaky.posts == 2


def test_a_call_that_ends_after_its_session_was_dropped_does_not_vouch_for_the_next_one(monkeypatch):
    """Whether a connection can have gone stale is a fact about ONE session.
    A slow call on the old session, finishing after it was replaced, used to
    mark the new one as having worked."""
    release, started = threading.Event(), threading.Event()
    posts = {"old": 0, "new": 0}

    class Old:
        def post(self, url, **kwargs):
            posts["old"] += 1
            started.set()
            release.wait(5)
            return "slow answer"

        def close(self):
            pass

    class New:
        def post(self, url, **kwargs):
            posts["new"] += 1
            raise requests.ConnectionError("no route to host")

        def close(self):
            pass

    sessions = [Old(), New(), New()]
    monkeypatch.setattr(common, "_new_http_session", lambda: sessions.pop(0))
    common._drop_http_session()
    slow = threading.Thread(target=lambda: common._http_post("http://example.invalid/"))
    slow.start()
    assert started.wait(5)
    common._drop_http_session()   # the session is replaced while the call is in flight
    common.http_session()         # and the new one is already there when the slow call ends
    release.set()
    slow.join(timeout=5)
    with pytest.raises(requests.ConnectionError):
        common._http_post("http://example.invalid/")
    assert posts == {"old": 1, "new": 1}, "the new session never worked: its failure is not tried twice"


# --- what a kept session must not keep --------------------------------------------------------


def test_a_cookie_the_server_sets_is_not_sent_back(server):
    api = server(set_cookie=True)
    common._http_post(api.url, json={}, timeout=5)
    common._http_post(api.url, json={}, timeout=5)
    assert all("Cookie" not in request["headers"] for request in api.requests)


def test_a_credential_given_to_one_call_is_not_carried_to_the_next(server):
    api = server()
    common._http_post(api.url, headers={"Authorization": "Bearer not-a-real-key"}, json={}, timeout=5)
    common._http_post(api.url, json={}, timeout=5)
    assert "Authorization" in api.requests[0]["headers"] and "Authorization" not in api.requests[1]["headers"]


def test_a_redirect_is_still_not_followed(server, monkeypatch):
    api = server(status=302)
    _no_waiting(monkeypatch)
    with pytest.raises(common.DirectAPIUnavailable, match="redirect"):
        common._openai_compatible_post_with_retry(api.url, {"Authorization": "Bearer not-a-real-key"}, {"input": ["x"]})
    assert len(api.requests) == 1


# --- the suite itself ---------------------------------------------------------------------------


@pytest.mark.as_every_other_test
def test_no_other_test_can_open_a_real_session():
    """A test that forgot to stand in for the API would call it for real,
    with whatever credential the machine has."""
    with pytest.raises(AssertionError, match="real HTTP"):
        common._http_post("http://127.0.0.1:1/")


# --- the session is shared under a public name ------------------------------------------------

def test_the_kept_session_is_public_and_kept_once_per_process():
    """doctor's PyPI check reaches the kept session through a public name.
    That the API calls ride this same session is the reuse tests' job above;
    this one only pins the public name and that it is kept, not rebuilt."""
    session = common.http_session()
    assert isinstance(session, requests.Session)
    assert common.http_session() is session, "one session for the process, not one per call"


def _private_http_names_of_common() -> set:
    """Every private module-level name common.py defines for its HTTP session
    machinery, read from common.py itself so a name added later (a new
    helper, a renamed lock) is covered without editing this test."""
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(common.__file__).read_text(encoding="utf-8"))
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(t.id for t in targets if isinstance(t, ast.Name))
    return {n for n in names if n.startswith("_") and "http" in n.lower()}


def test_the_private_http_names_are_read_from_common():
    """The guard below is only as wide as this set: if reading common.py
    stopped finding the session's private names, it would pass on nothing."""
    assert {"_new_http_session", "_drop_http_session", "_http_post",
            "_http_lock", "_http_session_kept"} <= _private_http_names_of_common()


def test_no_module_outside_common_reaches_a_private_session_name():
    """A private name of common.py used across a module boundary breaks
    silently when common.py renames it; the shared session has a public one.
    The rule covers the whole private HTTP machinery, not only the name that
    was once crossed (_http_session), so a later helper such as
    _new_http_session cannot be reached from outside either."""
    import ast
    import pathlib

    private = _private_http_names_of_common()
    package = pathlib.Path(common.__file__).parent
    offenders = []
    for path in sorted(package.rglob("*.py")):
        if path.name == "common.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and node.attr in private:
                offenders.append(f"{path.relative_to(package)}:{node.lineno} {node.attr}")
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("common"):
                offenders.extend(f"{path.relative_to(package)}:{node.lineno} {a.name}"
                                 for a in node.names if a.name in private)
    assert offenders == []
