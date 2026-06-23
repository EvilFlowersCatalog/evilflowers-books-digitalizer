"""Universal resolver for the static files a cover is built from.

One place decides where cover assets live. Everything the renderer draws —
fonts and faculty logos — is a *resource* looked up by a category and a name
(``fonts/DejaVuSans.ttf``, ``logos/fei.png``). Resolution walks a list of
search roots in order and returns the first hit:

1. any directories passed via ``[cover].assets_dir`` (one path or a list), then
2. the bundled :data:`BUNDLED_DIR` that ships inside the package.

So a deployment can drop a parallel ``assets/`` tree somewhere on disk and
override a single font or logo without touching code or the bundled files; an
absolute/relative path given directly is honoured as-is. This is the single
"universal way to define static files" the cover system relies on.
"""

from __future__ import annotations

from pathlib import Path

#: Bundled asset tree (``fonts/``, ``logos/``) shipped with the package.
BUNDLED_DIR = Path(__file__).parent / "assets"


class ResourceResolver:
    """Resolve a cover resource by ``category`` + ``name`` to a real path."""

    def __init__(self, assets_dir: str | Path | list[str | Path] | None = None):
        roots: list[Path] = []
        if isinstance(assets_dir, (str, Path)):
            roots.append(Path(assets_dir).expanduser())
        elif assets_dir:
            roots.extend(Path(d).expanduser() for d in assets_dir)
        roots.append(BUNDLED_DIR)
        self.roots = roots

    def find(self, category: str, name: str) -> Path | None:
        """First existing ``<root>/<category>/<name>`` (or a direct path)."""
        direct = Path(name).expanduser()
        if direct.is_absolute() or direct.exists():
            return direct if direct.exists() else None
        for root in self.roots:
            candidate = root / category / name
            if candidate.exists():
                return candidate
        return None

    def require(self, category: str, name: str) -> Path:
        path = self.find(category, name)
        if path is None:
            searched = ", ".join(str(r / category / name) for r in self.roots)
            raise FileNotFoundError(f"cover resource {category}/{name!r} not found (looked in: {searched})")
        return path
