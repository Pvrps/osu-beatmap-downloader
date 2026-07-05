"""Shared rich console, kept thin so the package imports cleanly even before
rich is installed (CLI entry points guard the real usage)."""

from __future__ import annotations

try:
    from rich.console import Console

    console = Console()
except Exception:  # pragma: no cover - fallback if rich missing
    import sys

    class _Plain:
        def print(self, *args, **kwargs) -> None:
            # Strip rich-style markup tags for plain output.
            import re

            text = " ".join(str(a) for a in args)
            text = re.sub(r"\[/?[a-zA-Z0-9 _=+#./:]+\]", "", text)
            sys.stdout.write(text + "\n")

    console = _Plain()
