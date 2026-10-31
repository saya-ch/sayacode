FROM python:3.13-slim@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b

# 只把运行代码放进镜像；冻结任务、隐藏判分与用户安装配置不进入镜像。
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=3 update \
    && apt-get -o Acquire::Retries=3 install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/sayacode
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv==0.12.5 \
    && uv export --quiet --python /usr/local/bin/python --locked --no-dev --no-emit-project --format requirements.txt -o /tmp/requirements.txt \
    && uv pip install --python /usr/local/bin/python --system --require-hashes -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt
COPY src/sayacode ./src/sayacode
COPY evals/worker.py /opt/evals/worker.py
RUN useradd --uid 1000 --create-home evaluator
USER evaluator
ENV PYTHONPATH=/opt/sayacode/src PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /workspace
CMD ["sleep", "infinity"]
