import logging

from .app import close_runtime_server, create_runtime_server
from .config import load_settings
from .inference import TorchZTA35GComponents, ZTA35GInferenceEngine


def configure_runtime_logging():
    logger = logging.getLogger("materialsagent_zta35g_runtime")
    has_runtime_handler = any(
        getattr(handler, "_materialsagent_runtime_handler", False)
        for handler in logger.handlers
    )
    if not has_runtime_handler:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler._materialsagent_runtime_handler = True
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def run():
    configure_runtime_logging()
    settings = load_settings()
    components = TorchZTA35GComponents(settings.model_root)
    engine = ZTA35GInferenceEngine(components)
    server = create_runtime_server(settings, engine)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        close_runtime_server(server)


if __name__ == "__main__":
    run()
