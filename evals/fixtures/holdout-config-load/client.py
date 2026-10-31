"""构造请求参数。"""


def request_options(config):
    return {"url": config["endpoint"], "headers": {"Authorization": "Bearer " + config["token"]}}
