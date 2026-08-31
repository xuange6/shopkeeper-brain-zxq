"""ASGI and command-line entry point for the knowledge base service."""

from __future__ import annotations

from knowledge.app import create_app
from knowledge.core.app_config import get_app_config


app = create_app()


def main() -> None:
    import uvicorn

    config = get_app_config()
    uvicorn.run(
        "knowledge.main:app",
        host=config.host,
        port=config.port,
        reload=config.reload,
    )


if __name__ == "__main__":
    main()
