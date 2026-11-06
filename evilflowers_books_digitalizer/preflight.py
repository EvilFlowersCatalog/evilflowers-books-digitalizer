"""``check``: preflight a deployment before it digitizes or publishes anything.

Each check yields one :class:`Check` (printed as one line). The k8s Job runs
``evilflowers-digitalizer check`` (plus ``--catalog`` before a publishing run)
and stops on a non-zero exit, so a bad mount, a read-only volume, a missing
binary or a wrong API key shows up in seconds rather than an hour into a book.

Checks:

* every ``[orchestration].sources`` entry: its source directory is readable;
* ``output_dir`` and ``cache_dir``: writable (a temp file is created, then deleted);
* free space on ``cache_dir`` ≥ ``[orchestration].min_free_gb``;
* binaries on ``PATH``: tesseract (with the ``slk`` model), scantailor
  (``[scantailor].binary``), jbig2, jpegoptim, qpdf, plus recode_pdf;
* with ``--catalog``: the API key is set, ``client.ping()`` passes, and every
  target catalog (per source, plus Excel per-book overrides) resolves.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evilflowers_books_digitalizer.runtime import RuntimeConfig, build_catalog

#: Tools every book needs (the scantailor binary name comes from the config).
REQUIRED_BINARIES = ("tesseract", "jbig2", "jpegoptim", "qpdf")
REQUIRED_TESSERACT_LANGS = ("slk",)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""

    def line(self) -> str:
        return f"{'OK  ' if self.ok else 'FAIL'}  {self.name}" + (f"  {self.detail}" if self.detail else "")


Which = Callable[[str], str | None]
RunText = Callable[[list[str]], str]


def _run_text(cmd: list[str]) -> str:
    """stdout + stderr of ``cmd`` (tesseract prints ``--list-langs`` to either)."""
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    return f"{result.stdout}\n{result.stderr}"


def check_source_dirs(rt: RuntimeConfig) -> Iterator[Check]:
    backend = rt.source.get("backend", "filesystem")
    for key in rt.source_keys:
        name = f"source {key}"
        if backend != "filesystem":
            yield Check(name, True, f"backend {backend!r}: not checked")
            continue
        root = rt.source.get("root")
        if not root:
            yield Check(name, False, "[source].root is not set")
            continue
        path = Path(root).expanduser() / rt.source.get("paths", {}).get(key, key)
        if not path.is_dir():
            yield Check(name, False, f"{path} is not a directory")
        elif not os.access(path, os.R_OK | os.X_OK):
            yield Check(name, False, f"{path} is not readable")
        else:
            try:
                count = sum(1 for entry in path.iterdir() if entry.is_dir())
            except OSError as exc:
                yield Check(name, False, f"{path}: {exc}")
            else:
                yield Check(name, True, f"{path} ({count} book dirs)")


def check_writable(name: str, path: Path) -> Check:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path, prefix=".preflight-", delete=True) as fh:
            fh.write(b"ok")
            fh.flush()
    except OSError as exc:
        return Check(f"{name} writable", False, f"{path}: {exc}")
    return Check(f"{name} writable", True, str(path))


def check_free_space(rt: RuntimeConfig) -> Check:
    minimum = float(rt.orchestration.get("min_free_gb", 40.0))
    try:
        free = shutil.disk_usage(rt.cache_dir).free / 1e9
    except OSError as exc:
        return Check("free space", False, f"{rt.cache_dir}: {exc}")
    return Check("free space", free >= minimum,
                 f"{free:.0f} GB free on {rt.cache_dir}, need {minimum:.0f} GB")


def _recode_pdf(which: Which) -> str | None:
    beside_python = Path(sys.executable).parent / "recode_pdf"
    return str(beside_python) if beside_python.exists() else which("recode_pdf")


def check_binaries(
    rt: RuntimeConfig, *, which: Which | None = None, run: RunText | None = None
) -> Iterator[Check]:
    which = which or shutil.which
    run = run or _run_text
    scantailor = str(rt.config.get("scantailor", {}).get("binary", "scantailor-deviant-cli"))
    for binary in (*REQUIRED_BINARIES, scantailor):
        found = which(binary)
        yield Check(f"binary {binary}", found is not None, found or "not on PATH")
    found = _recode_pdf(which)
    yield Check("binary recode_pdf", found is not None, found or "not found")

    if which("tesseract") is None:
        return
    try:
        listing = run(["tesseract", "--list-langs"])
    except (OSError, subprocess.SubprocessError) as exc:
        yield Check("tesseract languages", False, str(exc))
        return
    langs = {line.strip() for line in listing.splitlines()}
    missing = [lang for lang in REQUIRED_TESSERACT_LANGS if lang not in langs]
    yield Check("tesseract languages", not missing,
                f"missing {', '.join(missing)}" if missing else ", ".join(REQUIRED_TESSERACT_LANGS))


def check_catalog(rt: RuntimeConfig, *, client_factory: Callable[[Any], Any] | None = None) -> Iterator[Check]:
    """The ``--catalog`` checks. ``client_factory(settings)`` is injectable for tests."""
    from evilflowers_protocol.errors import CatalogError

    from evilflowers_books_digitalizer.catalog.publisher import CatalogSettings

    settings = CatalogSettings.from_runtime(rt)
    if not settings.base_url:
        yield Check("catalog base_url", False, "[catalog].base_url is not set")
        return
    if not settings.api_key:
        yield Check("catalog token", False, f"{settings.api_key_env} is not set")
        return
    yield Check("catalog token", True, f"{settings.api_key_env} is set")

    client = (client_factory or (lambda s: s.client()))(settings)
    try:
        client.ping()
    except CatalogError as exc:
        yield Check("catalog ping", False, str(exc))
        return
    yield Check("catalog ping", True, settings.base_url)

    targets: dict[str, str] = {}
    for key in rt.source_keys:
        try:
            targets.setdefault(settings.target_for(key), f"source {key}")
        except ValueError as exc:
            yield Check(f"catalog for {key}", False, str(exc))
    metadata = build_catalog(rt.metadata, base_dir=rt.config_dir)
    if metadata is not None:
        for override in metadata.catalog_overrides():
            targets.setdefault(override, "Excel override")
    for target, origin in targets.items():
        try:
            catalog = client.resolve_catalog(target)
        except CatalogError as exc:
            yield Check(f"catalog {target}", False, f"{origin}: {exc}")
        else:
            yield Check(f"catalog {target}", True, f"{origin} -> {catalog.id}")


def run_checks(
    rt: RuntimeConfig,
    *,
    catalog: bool = False,
    which: Which | None = None,
    run: RunText | None = None,
    client_factory: Callable[[Any], Any] | None = None,
) -> list[Check]:
    checks: list[Check] = [Check("config", True, str(rt.config_path))]
    checks += check_source_dirs(rt)
    checks.append(check_writable("output_dir", rt.output_dir))
    cache = check_writable("cache_dir", rt.cache_dir)
    checks.append(cache)
    checks.append(check_free_space(rt) if cache.ok else Check("free space", False, "cache_dir unusable"))
    checks += check_binaries(rt, which=which, run=run)
    if catalog:
        checks += check_catalog(rt, client_factory=client_factory)
    return checks
