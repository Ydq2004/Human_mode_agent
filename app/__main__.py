"""允许用 ``python -m app`` 启动本机 Web MVP。"""

from app.server import main


if __name__ == "__main__":
    raise SystemExit(main())
