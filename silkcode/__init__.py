"""Silk Code - an open, model-agnostic AI coding harness."""

from __future__ import annotations

#: What this build is, taken from the distribution metadata that setuptools_scm
#: stamped at build time. Git tags are the source of truth, so a release is
#: exactly its tag and a build from main is `0.2.1.devN+g<commit>` — a number
#: that actually moves, which is what `pip install -U` needs in order to see
#: that there is anything to fetch.
#:
#: The fallback is for running straight out of a source tree that was never
#: installed (`python -m silkcode` in a fresh clone). It has to be a real
#: version string, and it is deliberately on the low side: claiming less than
#: you are is recoverable, claiming a release you are not is not.
_FALLBACK_VERSION = "0.2.0"


def _installed_version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version
        return version("silkcode")
    except Exception:
        return _FALLBACK_VERSION


__version__ = _installed_version()
