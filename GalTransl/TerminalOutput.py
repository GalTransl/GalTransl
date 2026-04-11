from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from alive_progress import alive_bar


class NullProgressBar:
    def __call__(self, *args: Any, **kwargs: Any) -> None:
        return

    def title(self, *args: Any, **kwargs: Any) -> None:
        return

    def text(self, *args: Any, **kwargs: Any) -> None:
        return


def should_print_translation_logs(project_config: Any) -> bool:
    return bool(getattr(project_config, "print_translation_log_in_terminal", True))


@contextmanager
def terminal_progress(enabled: bool, **kwargs: Any) -> Iterator[Any]:
    if enabled:
        with alive_bar(**kwargs) as bar:
            yield bar
        return
    yield NullProgressBar()
