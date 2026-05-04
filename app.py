from __future__ import annotations

from mq01.bootstrap import bootstrap_source_root

bootstrap_source_root()

from mq01.ui_runtime_v2 import render_app


def main() -> None:
    render_app()


if __name__ == "__main__":
    main()
