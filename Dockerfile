# MinerU 4.2.3 + vLLM 0.21.0 (CUDA 12.9) 单文件镜像
# 合并原先三步构建（基础安装 -> 离线 Swagger 校验 -> transformers 版本修复）为一次构建
FROM docker.m.daocloud.io/vllm/vllm-openai:v0.21.0-cu129

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Shanghai

# 系统依赖：apt 源切换到南京大学镜像
RUN sed -i \
        -e 's@http://archive.ubuntu.com/ubuntu@https://mirror.nju.edu.cn/ubuntu@g' \
        -e 's@http://security.ubuntu.com/ubuntu@https://mirror.nju.edu.cn/ubuntu@g' \
        /etc/apt/sources.list && \
    apt-get update && \
    apt-get install -y \
        libgl1 \
        fonts-noto-core \
        fonts-noto-cjk \
        fontconfig && \
    fc-cache -fv && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 安装 MinerU 4.2.3（复用基础镜像中的 vLLM），使用南京大学 pip 源
RUN python3 -m pip install -U \
        'mineru[torch]==4.2.3' \
        -i https://mirror.nju.edu.cn/pypi/web/simple \
        --break-system-packages && \
    python3 -m pip cache purge

# transformers 版本修复：与 vLLM 0.21.0 兼容
RUN python3 -m pip install --no-cache-dir \
        'transformers>=5.10.1,<5.23.0' \
        -i https://mirror.nju.edu.cn/pypi/web/simple \
        --break-system-packages

# 固定推理后端与离线运行环境
ENV MINERU_MODEL_SMALL_BACKEND=torch \
    MINERU_MODEL_VLM_ENGINE=vllm \
    MINERU_MODEL_SOURCE=local \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    MODELSCOPE_ENVIRONMENT=offline \
    DO_NOT_TRACK=1

# 下载 MinerU Standard 档所需模型（modelscope）
RUN mineru-kit models download \
    --tier standard \
    --small-backend torch \
    --vlm-engine vllm \
    -s modelscope

# 校验：MinerU 版本固定 4.2.3、离线 Swagger 实现、transformers/vLLM 兼容范围
RUN python3 - <<'PY'
from importlib.metadata import version
from pathlib import Path

import transformers
import vllm
from packaging.version import Version

import mineru.parser.api_server as api

print("MinerU:", version("mineru"))
print("fastapi_offline:", version("fastapi_offline"))
print("Transformers:", transformers.__version__)
print("vLLM:", vllm.__version__)
print("API module:", api.__file__)

assert version("mineru") == "4.2.3"
assert Version("5.10.1") <= Version(transformers.__version__) < Version("5.23.0")
assert Version(vllm.__version__.split("+")[0]) == Version("0.21.0")

source = Path(api.__file__).read_text()
assert "from fastapi_offline import FastAPIOffline" in source
assert "FastAPIOffline if enable_docs else FastAPI" in source

import fastapi_offline

static_dir = Path(fastapi_offline.__file__).parent / "static"
assert static_dir.is_dir()
assert (static_dir / "swagger-ui-bundle.js").is_file()
assert (static_dir / "swagger-ui.css").is_file()

print("Version compatibility range: OK")
print("Offline Swagger source: OK")
print("Offline Swagger static assets: OK")
PY

# 内置同步 JSON 封装服务（file-parse-json）：把 file-parse-server 返回的 zip 解出 markdown，返回旧 2.5 风格 JSON
COPY file-parse-json/app.py /app/app.py

ENTRYPOINT ["/bin/bash", "-c", "export MINERU_MODEL_SOURCE=local && exec \"$@\"", "--"]
