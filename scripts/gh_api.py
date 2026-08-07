"""Minimal GitHub REST client shared by this repository's automation scripts.

Standard library only, so it runs on any runner without a setup step. Every
call is explicit about method, path, and Accept header; nothing here guesses a
URL from user input. Tests substitute a fake by overriding `_open`.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
JSON_ACCEPT = "application/vnd.github+json"
RAW_ACCEPT = "application/octet-stream"
USER_AGENT = "pi-launcher-automation"
TIMEOUT = 60


class GitHubError(RuntimeError):
    """Any non-success response, or a response that is not shaped as expected."""


class _AuthStrippingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Drop Authorization when a release-asset download redirects to storage.

    GitHub answers an asset download with a redirect to a signed object-store
    URL that rejects a request carrying an unrelated Authorization header.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        same_host = (
            urllib.parse.urlsplit(newurl).netloc
            == urllib.parse.urlsplit(req.full_url).netloc
        )
        if not same_host:
            for name in [n for n in new.headers if n.lower() == "authorization"]:
                del new.headers[name]
            new.unredirected_hdrs.pop("Authorization", None)
        return new


class GitHubClient:
    def __init__(self, token=None, api_root=API_ROOT):
        self.token = token or None
        self.api_root = api_root.rstrip("/")
        self._opener = urllib.request.build_opener(_AuthStrippingRedirectHandler())

    # -- transport -------------------------------------------------------
    def _url(self, path):
        if path.startswith("https://"):
            return path
        return f"{self.api_root}{path}"

    def _open(self, method, path, accept=JSON_ACCEPT, payload=None):
        """Perform one request; return the response body as bytes."""
        url = self._url(path)
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", accept)
        req.add_header("X-GitHub-Api-Version", API_VERSION)
        req.add_header("User-Agent", USER_AGENT)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with self._opener.open(req, timeout=TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise GitHubError(f"{method} {url}: HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise GitHubError(f"{method} {url}: {exc.reason}") from exc

    # -- typed helpers ---------------------------------------------------
    def get_json(self, path):
        return json.loads(self._open("GET", path).decode())

    def get_bytes(self, path, accept=RAW_ACCEPT):
        return self._open("GET", path, accept=accept)

    def post_json(self, path, payload):
        return json.loads(self._open("POST", path, payload=payload).decode())

    def patch_json(self, path, payload):
        return json.loads(self._open("PATCH", path, payload=payload).decode())

    def put_json(self, path, payload):
        return json.loads(self._open("PUT", path, payload=payload).decode())

    def paginate(self, path, per_page=100, max_pages=20):
        """Yield items from a list endpoint, page by page."""
        joiner = "&" if "?" in path else "?"
        for page in range(1, max_pages + 1):
            batch = self.get_json(f"{path}{joiner}per_page={per_page}&page={page}")
            if not isinstance(batch, list):
                raise GitHubError(f"GET {path}: expected a list response")
            yield from batch
            if len(batch) < per_page:
                return
        raise GitHubError(f"GET {path}: more than {max_pages} pages of results")


def client_from_env():
    return GitHubClient(token=os.environ.get("GITHUB_TOKEN"))


def emit_outputs(values):
    """Write step outputs when running under GitHub Actions; else print them."""
    lines = [f"{key}={value}" for key, value in values.items()]
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    else:
        for line in lines:
            print(f"output: {line}")
