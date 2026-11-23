# MinerU 同步接口封装（兼容 3.4.5）

本项目封装 MinerU 4.2.3 的文档解析能力，对外提供一套与 **MinerU 3.4.5 兼容的同步接口**（`POST /file_parse` 上传文件、直接返回正文 JSON），供业务从旧版同步调用方式平滑迁移。

## 背景

MinerU 4.2.3 将对外接口重构为异步的 OpenAI 风格 Tasks API（`/v1/tasks`），并移除了旧版中「同步上传文件、直接返回正文 JSON」的 `/file_parse` 接口——4.2.3 的解析结果一律走「文件进 → zip 文件出」的下载模式（zip 内含 `markdown.md` / `structured_content.json` / `middle_json.json` / `images/`）。

为了让依赖旧版同步 JSON 返回方式的调用方无需改动即可继续使用，本项目在 4.2.3 之上增加一层薄封装，恢复 3.4.5 风格的同步 JSON 返回。

## 架构与服务

项目由三个容器化服务组成，三者共用同一个镜像 `mineru:4.2.3-vllm-cu129`，通过 Docker 内部网络 `appnet`（`10.200.1.0/24`）通信：

| 服务 | 容器名 | 端口（宿主 → 容器） | GPU | 功能 |
| --- | --- | --- | --- | --- |
| `mineru-api-4x` | `mineru-api-423` | 28001 → 8000 | 需要（GPU 0,1） | MinerU 4.2.3 异步 Tasks API，真正执行文档解析。`mineru-kit api-server`，VLM 引擎走 vLLM |
| `mineru-file-parse` | `mineru-file-parse` | 28002 → 8003 | 不需要 | 同步单文件封装。`mineru-kit file-parse-server`，把同步请求转发给 api-server，完成后返回 zip |
| `mineru-file-parse-json` | `mineru-file-parse-json` | 28003 → 8004 | 不需要 | 薄封装。调用 file-parse-server → 内存解 zip → 返回 3.4.5 风格的正文 JSON |

数据流：

```
客户端 ──POST /file_parse──▶ mineru-file-parse-json (28003)
                              │ 转发 POST /v1/file_parse
                              ▼
                          mineru-file-parse (28002)
                              │ 转发 (--api-url http://mineru-api-423:8000)
                              ▼
                          mineru-api-4x (28001, Tasks API + vLLM/GPU)
                              │ 解析完成，返回 zip
                              ▼
                          mineru-file-parse-json 解 zip，返回正文 JSON
```

`mineru-file-parse-json` 的代码（`file-parse-json/app.py`）已内置进镜像（`Dockerfile` 末尾 `COPY file-parse-json/app.py /app/app.py`），无需额外挂载。

## 离线运行（纯局域网）

**可以，镜像运行时完全不依赖互联网**，可在纯内网 / 离线环境使用。实现方式：

1. **模型内置**：构建镜像时已通过 `mineru-kit models download` 把 Standard 档所需模型全部下载并打进镜像，运行时无需再从 HuggingFace / ModelScope 拉取。
2. **离线开关**：运行时通过环境变量强制离线——`HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`、`MODELSCOPE_ENVIRONMENT=offline`、`MINERU_MODEL_SOURCE=local`，模型加载器只读本地文件，不发起任何外网请求。
3. **内部网络**：三个服务之间经 Docker 内部网络 `appnet` 通信，不经过外网。

> 注意：仅**运行时**离线。**构建镜像**时仍需联网（拉取 apt 依赖、pip 包、模型）。构建完成后，可将镜像 `docker save` 导出、离线分发到内网环境加载运行。

## 快速开始

```bash
# 1. 构建镜像（需联网；构建后即可离线运行）
DOCKER_BUILDKIT=0 docker build -t mineru:4.2.3-vllm-cu129 .

# 2. 启动三个服务
docker compose up -d

# 3. 调用同步接口
curl -X POST "http://localhost:28003/file_parse" \
  -F "file=@test.pdf" \
  -F "tier=flash"
```

## 接口文档

### Swagger

三个服务各自内置 Swagger UI（离线可用，`MINERU_API_ENABLE_FASTAPI_DOCS=true`）：

| 服务 | Swagger 地址 | 标题 |
| --- | --- | --- |
| `mineru-api-4x` | `http://<host>:28001/docs` | `MinerU API`（异步 Tasks API） |
| `mineru-file-parse` | `http://<host>:28002/docs` | `MinerU File Parse`（同步，返回 zip） |
| `mineru-file-parse-json` | `http://<host>:28003/docs` | `MinerU File Parse JSON`（同步，返回正文 JSON） |

在 28003 的 Swagger 中可直接「Try it out」：`file` 字段有文件选择框，`tier` / `ocr_mode` 有枚举下拉，`page_range` 默认可勾选空值。

### 同步正文 JSON 接口（`mineru-file-parse-json`，端口 28003）

这是本项目对外的主要接口，返回与 3.4.5 一致的正文 JSON。

#### `POST /file_parse`

同步解析单个文件，返回整篇 markdown 正文。

**请求**（`multipart/form-data`）：

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `file` | 文件 | 必填 | 待解析文档（PDF / 图片 / DOCX / PPTX / XLSX） |
| `wait_timeout` | int | 3600 | 同步等待最长秒数（0–3600）。超时返回 202 + `status_url` |
| `tier` | str | standard | 解析档位，仅 PDF/图片生效：`flash` 最快 / `basic` 基础 / `standard` 标准 / `advanced` 最高精度。原生文档始终 flash；留空 = 服务端自动选择（standard→basic→flash→advanced） |
| `ocr_mode` | str | auto | OCR 模式，仅 PDF 生效：`auto` 自动 / `txt` 文本抽取（文本型 PDF）/ `ocr` OCR（扫描件、图片型 PDF） |
| `page_range` | str | 空 | PDF 页码范围（从 1 起、闭区间），如 `1-5,8`、`r1`（最后一页）；空或 `all` = 全部页 |

**成功响应**（`200`）：

```json
{
  "task_id": "job_xxx",
  "status": "completed",
  "backend": "vllm",
  "file_names": ["test"],
  "created_at": "2026-11-23T02:45:18Z",
  "started_at": "2026-11-23T02:45:18Z",
  "completed_at": "2026-11-23T02:45:19Z",
  "error": null,
  "status_url": "http://<host>:28003/file_parse/{request_id}",
  "result_url": "http://<host>:28003/file_parse/{request_id}/result",
  "version": "4.2.3",
  "results": {
    "test": { "md_content": "整篇 markdown 正文" }
  }
}
```

字段说明：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `task_id` | string | 解析任务 ID（对应 api-server 的 task_id） |
| `status` | string | `completed`（成功） |
| `backend` | string | 后端引擎，固定 `vllm` |
| `file_names` | array | 文件名（不含扩展名），多文件时对应多元素 |
| `created_at` / `started_at` / `completed_at` | string | 任务创建 / 开始 / 完成时间（ISO 8601 UTC） |
| `error` | any | 错误信息，成功为 `null` |
| `status_url` | string | 查询该任务状态的地址（超时 202 时用于轮询） |
| `result_url` | string | 下载原始解析产物 zip 的地址 |
| `version` | string | 解析所用 MinerU 版本 |
| `results` | object | 以文件名为 key，值为 `{ "md_content": "整篇 markdown 正文" }` |

**超时响应**（`202`，`wait_timeout` 内未完成时）：

```json
{
  "request_id": "xxx",
  "task_id": "job_xxx",
  "status": "running",
  "status_url": "http://<host>:28003/file_parse/{request_id}",
  "result_url": "http://<host>:28003/file_parse/{request_id}/result",
  "output_formats": ["default"],
  "error": null
}
```

拿到 202 后，用 `status_url` 轮询状态，完成后用 `result_url` 下载 zip。

#### `GET /file_parse/{request_id}`

查询同步解析任务状态。返回结构与上面 202 响应一致；完成后 `status` 变为 `completed`。

#### `GET /file_parse/{request_id}/result`

下载原始解析产物 zip（`application/zip`），内含：

- `markdown.md` — markdown 正文
- `structured_content.json` — 结构化内容（页 / 块级）
- `middle_json.json` — 中间解析结果
- `images/` — 提取的图片

### 底层同步接口（`mineru-file-parse`，端口 28002）

`mineru-kit file-parse-server` 原生提供的同步单文件接口，返回 zip 而非正文 JSON：

- `POST /v1/file_parse` — 同步解析，成功返回 200 + zip；超时返回 202 + `status_url`
- `GET /v1/file_parse/{request_id}` — 查询状态
- `GET /v1/file_parse/{request_id}/result` — 下载 zip

### 底层异步接口（`mineru-api-4x`，端口 28001）

MinerU 4.2.3 原生异步 Tasks API（OpenAI 风格）：

- `POST /v1/tasks` — 创建解析任务（支持多文件、`tier`、`ocr_mode` 等）
- `GET /v1/tasks/{task_id}` — 查询任务状态（含 `created_at` / `started_at` / `finished_at`）
- `GET /v1/tasks/{task_id}/result` — 下载解析产物

## 构建镜像

```bash
DOCKER_BUILDKIT=0 docker build -t mineru:4.2.3-vllm-cu129 .
```

构建要点（`Dockerfile` 已内置）：

- 基础镜像：`vllm/vllm-openai:v0.21.0-cu129`（CUDA 12.9）
- apt 源 / pip 源均切换到南京大学镜像（`mirror.nju.edu.cn`）
- `mineru[torch]==4.2.3`；装完后需再装 `transformers>=5.10.1,<5.23.0` 以兼容 vLLM 0.21.0
- 模型经 `mineru-kit models download -s modelscope` 在构建期下载并内置
- `file-parse-json/app.py` 通过 `COPY` 内置；由于 `COPY` 追加在末尾，改 `app.py` 后重跑构建时前面所有层（含模型下载）全部命中缓存，秒级重建

## 目录结构

```
.
├── Dockerfile                 # 单文件镜像定义（安装 + 模型下载 + 校验 + 内置 app.py）
├── docker-compose.yml         # 三个服务的编排
└── file-parse-json/
    └── app.py                 # 同步正文 JSON 薄封装（内置进镜像）
```
