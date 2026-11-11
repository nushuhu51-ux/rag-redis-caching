"""Entry point: `python -m app.main` starts the API using environment configuration."""
import logging

from .api import make_server
from .config import Settings
from .factory import build_service


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = Settings.from_env()
    service = build_service(s)
    server = make_server(service, s.http_host, s.http_port)
    logging.info("serving on http://%s:%d (cache_enabled=%s, ttl=%ss)",
                 s.http_host, s.http_port, s.cache_enabled, s.cache_ttl_seconds)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if service.cache is not None:
            close = getattr(service.cache.client, "close", None)
            if close:
                close()


if __name__ == "__main__":
    main()
