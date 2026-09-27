"""Frozen model responses: record once, replay offline, prove the table is stable.

Why this module exists
----------------------
The project's loudest claim is that a stranger can recompute our published
number with one command. That is true of the rule layer, which is pure code.
It is *not* true of ``rule_v4+llm``: reproducing that column means calling an
endpoint the stranger — and quite possibly the judge — does not have.

So the model responses are frozen into the repository. ``eval score --record``
writes one file per call, and ``eval score --replay`` re-derives the same table
from those files with no network at all. The point is not caching; it is that
the published table stops depending on our API key.

What auditable actually means here
----------------------------------
A frozen response is only auditable if you can tell what question produced it.
Every record therefore carries the post id, the judge version, the model name,
the endpoint, a timestamp, the sha256 of the exact prompt, and the raw response
text. Drop any one of those and the file is a cache, not evidence: you could not
tell whether a response was produced by the prompt we claim, or by the version
we claim, or by the model we claim.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MANIFEST_NAME = "manifest.json"
CALLS_NAME = "calls.jsonl"

# The fields every record MUST carry. Anything less is a cache, not evidence.
REQUIRED_FIELDS: tuple[str, ...] = (
    "post_id",
    "judge_version",
    "model",
    "endpoint",
    "recorded_at",
    "prompt_sha256",
    "response",
)


def prompt_digest(system: str, user: str) -> str:
    """sha256 of the exact prompt pair, so a changed prompt cannot replay silently."""
    blob = f"{system}\n{user}".encode()
    return hashlib.sha256(blob).hexdigest()


def _utc_now() -> str:
    """ISO-8601 UTC timestamp, second resolution, timezone-explicit."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class FrozenCall:
    """One model answer, with everything needed to say what question it answered."""

    post_id: str
    judge_version: str
    model: str
    endpoint: str
    recorded_at: str
    prompt_sha256: str
    response: str

    @property
    def key(self) -> tuple[str, str, str]:
        """Identity used for replay lookup: post, judge version, prompt digest."""
        return (self.post_id, self.judge_version, self.prompt_sha256)

    def to_dict(self) -> dict[str, Any]:
        """Serialise for the JSONL file."""
        return {
            "post_id": self.post_id,
            "judge_version": self.judge_version,
            "model": self.model,
            "endpoint": self.endpoint,
            "recorded_at": self.recorded_at,
            "prompt_sha256": self.prompt_sha256,
            "response": self.response,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any], *, source: str = "") -> FrozenCall:
        """Deserialise, refusing a record that is missing any required field."""
        from intentradar.errors import ConfigError

        missing = [name for name in REQUIRED_FIELDS if not str(payload.get(name) or "")]
        if missing:
            where = f" in {source}" if source else ""
            raise ConfigError(
                f"replay record{where} is missing required field(s) {missing} — "
                "a frozen response without provenance is a cache, not evidence"
            )
        return cls(
            post_id=str(payload["post_id"]),
            judge_version=str(payload["judge_version"]),
            model=str(payload["model"]),
            endpoint=str(payload["endpoint"]),
            recorded_at=str(payload["recorded_at"]),
            prompt_sha256=str(payload["prompt_sha256"]),
            response=str(payload["response"]),
        )


@dataclass
class ReplayRecorder:
    """Writes the frozen responses produced by one scoring run."""

    directory: Path
    testset_id: str = ""
    layer: str = ""
    judge_version: str = ""
    note: str = ""
    _calls: list[FrozenCall] = field(default_factory=list, repr=False)
    _closed: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        """Accept a str path as well as a Path."""
        if not isinstance(self.directory, Path):
            self.directory = Path(self.directory)

    def add(self, call: FrozenCall) -> None:
        """Append one frozen call. Ignored after :meth:`close`."""
        if self._closed:
            return
        self._calls.append(call)

    @property
    def call_count(self) -> int:
        """How many responses this recorder has captured."""
        return len(self._calls)

    def close(self) -> Path:
        """Write ``calls.jsonl`` + ``manifest.json``; return the directory."""
        if self._closed:
            return self.directory
        self.directory.mkdir(parents=True, exist_ok=True)
        calls_path = self.directory / CALLS_NAME
        with calls_path.open("w", encoding="utf-8") as handle:
            for call in self._calls:
                handle.write(json.dumps(call.to_dict(), ensure_ascii=False) + "\n")

        models = sorted({c.model for c in self._calls if c.model})
        endpoints = sorted({c.endpoint for c in self._calls if c.endpoint})
        versions = sorted({c.judge_version for c in self._calls if c.judge_version})
        manifest = {
            "testset_id": self.testset_id,
            "layer": self.layer,
            "judge_version": ", ".join(versions),
            "model": ", ".join(models) or "(unknown)",
            "endpoint": ", ".join(endpoints) or "(unknown)",
            "recorded_at": _utc_now(),
            "calls": len(self._calls),
            "note": self.note,
            "fields": list(REQUIRED_FIELDS),
        }
        (self.directory / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._closed = True
        return self.directory


@dataclass
class ReplayStore:
    """Frozen responses read back from disk, for an offline replay run."""

    directory: Path
    manifest: dict[str, Any]
    calls: dict[tuple[str, str, str], FrozenCall]
    _used: set[tuple[str, str, str]] = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        """Accept a str path as well as a Path."""
        if not isinstance(self.directory, Path):
            self.directory = Path(self.directory)

    @classmethod
    def load(cls, directory: str | Path) -> ReplayStore:
        """Read a recorded directory. Raises when it is not a usable recording."""
        from intentradar.errors import ConfigError

        path = Path(directory)
        manifest_path = path / MANIFEST_NAME
        calls_path = path / CALLS_NAME
        if not calls_path.exists():
            raise ConfigError(
                f"no replay recording at {path} — expected {CALLS_NAME}. "
                "Create one with `eval score --record <dir>` on a machine that "
                "has an endpoint; a replay without a recording is not a measurement."
            )

        manifest: dict[str, Any] = {}
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        calls: dict[tuple[str, str, str], FrozenCall] = {}
        for number, line in enumerate(
            calls_path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ConfigError(
                    f"replay recording {calls_path} line {number} is not valid JSON: {exc}"
                ) from exc
            call = FrozenCall.from_dict(payload, source=f"{calls_path}:{number}")
            calls[call.key] = call

        if not calls:
            raise ConfigError(f"replay recording at {path} contains no calls")
        return cls(directory=path, manifest=manifest, calls=calls)

    def lookup(self, post_id: str, judge_version: str, prompt_sha256: str) -> FrozenCall:
        """Return the frozen response, or fail loudly.

        A miss is never filled from anywhere else. Silently substituting a live
        call (or a mock answer) would turn a replay into a different measurement
        wearing the replay's label.
        """
        from intentradar.errors import ConfigError

        key = (post_id, judge_version, prompt_sha256)
        call = self.calls.get(key)
        if call is None:
            known_versions = sorted({k[1] for k in self.calls})
            same_post = [k for k in self.calls if k[0] == post_id]
            if same_post and not any(k[1] == judge_version for k in same_post):
                detail = (
                    f"the recording has this post under judge_version "
                    f"{sorted({k[1] for k in same_post})}, not {judge_version!r}"
                )
            elif same_post:
                detail = (
                    "the recording has this post but under a DIFFERENT prompt "
                    "(prompt_sha256 differs) — the prompt changed since the recording"
                )
            else:
                detail = (
                    f"the recording has no entry for this post at all "
                    f"({len(self.calls)} calls, judge_version {known_versions})"
                )
            raise ConfigError(
                f"replay miss for post {post_id} under {judge_version}: {detail}. "
                "Re-record with `eval score --record <dir>`; a gap here is not "
                "filled from anywhere, because a replay that quietly calls a live "
                "endpoint is not a replay."
            )
        self._used.add(key)
        return call

    @property
    def model(self) -> str:
        """Model name from the manifest, falling back to the records themselves."""
        name = str(self.manifest.get("model") or "")
        if name and name != "(unknown)":
            return name
        models = sorted({c.model for c in self.calls.values() if c.model})
        return ", ".join(models) or "(unknown)"

    @property
    def recorded_at(self) -> str:
        """When the recording was made, from the manifest."""
        return str(self.manifest.get("recorded_at") or "(unknown date)")

    @property
    def unused(self) -> list[FrozenCall]:
        """Recorded calls the replay never needed — a stale recording's fingerprint."""
        return [c for key, c in self.calls.items() if key not in self._used]

    def banner(self) -> str:
        """The line that goes on top of a replayed report."""
        return (
            f"REPLAYED — frozen model responses from {self.recorded_at}, "
            f"model={self.model}. Not a live run."
        )
