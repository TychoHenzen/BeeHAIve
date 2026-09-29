from __future__ import annotations

import uvicorn

from .app import create_app
from .config import CoreConfig


def main() -> None:
    config = CoreConfig.from_environment()
    uvicorn.run(create_app(config), host="127.0.0.1", port=8001)


if __name__ == "__main__":
    main()
