"""配置加载与诊断。"""
import json
import logging
from pathlib import Path

LOGGER = logging.getLogger(__name__)


def load_config(path, environ):
    config = {"endpoint": "https://example.invalid", "token": ""}
    if environ.get("APP_ENDPOINT"):
        config["endpoint"] = environ["APP_ENDPOINT"]
    if environ.get("APP_TOKEN"):
        config["token"] = environ["APP_TOKEN"]
    if Path(path).exists():
        config.update(json.loads(Path(path).read_text(encoding="utf-8")))
    return config


def diagnostic(config):
    LOGGER.info("effective configuration: %s", config)
    return {"endpoint": config["endpoint"], "has_token": bool(config["token"])}
