"""Poll fib-accumulator's `signals/` directory via the GitHub Contents API.

Two requests per run:
  1. `GET /repos/{repo}/contents/signals?ref={branch}` — list files.
  2. For each unseen `.json`, a second GET to its `download_url` for the body.

A 404 on the directory listing means the producer side hasn't shipped yet;
that's the current state of the world and should be treated as no-op, not error.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass

import httpx

from .signal import Signal

log = logging.getLogger(__name__)

_API_ROOT = "https://api.github.com"


@dataclass(frozen=True)
class FetchedSignal:
    signal: Signal
    raw: dict
    source_sha: str


class SignalPoller:
    def __init__(self, repo: str, branch: str, token: str | None) -> None:
        self.repo = repo
        self.branch = branch
        self.token = token

    def _headers(self) -> dict[str, str]:
        h = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "tradebot",
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def list_signals(self, client: httpx.Client) -> list[dict]:
        url = f"{_API_ROOT}/repos/{self.repo}/contents/signals"
        resp = client.get(url, params={"ref": self.branch}, headers=self._headers())
        if resp.status_code == 404:
            log.info("signals/ not found in %s@%s — producer not live yet", self.repo, self.branch)
            return []
        resp.raise_for_status()
        entries = resp.json()
        return [
            e
            for e in entries
            if e.get("type") == "file" and e.get("name", "").endswith(".json")
        ]

    def fetch_signal(self, client: httpx.Client, entry: dict) -> FetchedSignal | None:
        download_url = entry.get("download_url")
        if not download_url:
            log.warning("skipping %s: no download_url", entry.get("name"))
            return None
        resp = client.get(download_url, headers=self._headers())
        resp.raise_for_status()
        raw = json.loads(resp.text)
        try:
            signal = Signal.model_validate(raw)
        except Exception:
            log.exception("failed to validate %s", entry.get("name"))
            return None
        return FetchedSignal(signal=signal, raw=raw, source_sha=entry.get("sha", ""))

    def poll(self) -> Iterator[FetchedSignal]:
        with httpx.Client(timeout=15.0) as client:
            for entry in self.list_signals(client):
                fetched = self.fetch_signal(client, entry)
                if fetched is not None:
                    yield fetched
