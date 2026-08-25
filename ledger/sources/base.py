"""HTTP client for public disclosure endpoints.

The politeness rules here are the operating contract, not optimisations:

* One request at a time per host, with a floor on the interval between them.
* `robots.txt` is honoured.
* A 403 is a decision by the site operator and stops the run. It is never
  routed around, retried behind a different agent, or treated as a rate limit.
* The User-Agent identifies the project and carries a contact address, so an
  operator can reach us instead of silently blocking. SEC.gov requires this.
* Every response is cached on disk. Re-running an ingest costs the source
  nothing.
"""
from __future__ import annotations

import hashlib
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.robotparser import RobotFileParser


class Blocked(RuntimeError):
    """The operator refused us. Terminal by design - do not work around it."""


class FetchError(RuntimeError):
    pass


@dataclass
class PoliteClient:
    user_agent: str                      # "Project name contact@example.com"
    min_interval: float = 1.5            # seconds between requests to one host
    timeout: float = 30.0
    cache_dir: Path | None = None
    max_retries: int = 3
    respect_robots: bool = True
    _last: dict[str, float] = field(default_factory=dict, repr=False)
    _robots: dict[str, RobotFileParser] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.cache_dir:
            self.cache_dir = Path(self.cache_dir)
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    # -- politeness -----------------------------------------------------------

    def _wait(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            gap = time.monotonic() - last
            if gap < self.min_interval:
                time.sleep(self.min_interval - gap)
        self._last[host] = time.monotonic()

    def _allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parts = urllib.parse.urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"
        rp = self._robots.get(host)
        if rp is None:
            rp = RobotFileParser()
            rp.set_url(f"{host}/robots.txt")
            try:
                rp.read()
            except Exception:
                # Unreadable robots.txt is not permission; assume allowed only
                # for the fetch itself, and keep the interval floor.
                rp.parse([])
            self._robots[host] = rp
        return rp.can_fetch(self.user_agent, url)

    def _cache_path(self, url: str) -> Path | None:
        if not self.cache_dir:
            return None
        return self.cache_dir / (hashlib.sha256(url.encode()).hexdigest()[:32] + ".bin")

    # -- fetch ----------------------------------------------------------------

    def get(self, url: str, *, use_cache: bool = True) -> bytes:
        cp = self._cache_path(url)
        if use_cache and cp and cp.exists():
            return cp.read_bytes()
        if not self._allowed(url):
            raise Blocked(f"robots.txt disallows {url}")

        host = urllib.parse.urlsplit(url).netloc
        delay = self.min_interval
        last_err: Exception | None = None

        for attempt in range(self.max_retries):
            self._wait(host)
            req = urllib.request.Request(url, headers={
                "User-Agent": self.user_agent,
                "Accept-Encoding": "gzip, deflate",
                "Accept": "*/*",
            })
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    body = r.read()
                if cp:
                    cp.write_bytes(body)
                return body
            except urllib.error.HTTPError as e:
                if e.code == 403:
                    raise Blocked(
                        f"{host} returned 403 - the operator has refused this client. "
                        "Stop and seek access rather than working around it.") from e
                if e.code in (429, 503):
                    retry_after = e.headers.get("Retry-After")
                    delay = float(retry_after) if (retry_after or "").isdigit() else delay * 2
                    last_err = e
                    time.sleep(delay)
                    continue
                if 500 <= e.code < 600:
                    last_err = e
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise FetchError(f"{url} -> HTTP {e.code}") from e
            except urllib.error.URLError as e:
                last_err = e
                time.sleep(delay)
                delay *= 2
        raise FetchError(f"{url} failed after {self.max_retries} attempts: {last_err}")


class Source:
    """Common surface for every ingest source.

    `discover` lists what is available; `fetch` returns raw bytes for one
    document; `parse` turns those bytes into transaction dicts. Keeping the
    three separate is what lets the raw document be archived verbatim and
    re-parsed later when the extractor improves.
    """

    name: str = "source"
    doc_type: str = "filing"

    def __init__(self, client: PoliteClient):
        self.client = client

    def discover(self, **kw) -> list[dict]:
        raise NotImplementedError

    def fetch(self, ref: dict) -> bytes:
        raise NotImplementedError

    def parse(self, raw: bytes, ref: dict) -> list[dict]:
        raise NotImplementedError
