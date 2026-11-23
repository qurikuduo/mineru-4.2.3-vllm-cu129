import io
import json
import os
import zipfile
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import FastAPI, File, Form, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

FILE_PARSE_URL = os.environ.get("MINERU_FILE_PARSE_URL", "http://mineru-file-parse:8003").rstrip("/")
API_URL = os.environ.get("MINERU_API_URL", "http://mineru-api-423:8000").rstrip("/")

TIERS = ["flash", "basic", "standard", "advanced"]
OCR_MODES = ["auto", "txt", "ocr"]

FILE_DESC = "待解析文档（PDF / 图片 / DOCX / PPTX / XLSX）"
WAIT_TIMEOUT_DESC = "同步等待的最长秒数（0-3600，默认 3600）"
TIER_DESC = (
    "解析档位（仅 PDF/图片生效，原生文档始终 flash）："
    "flash=最快 / basic=基础 / standard=标准 / advanced=最高精度。"
    "留空=服务端自动选择（顺序 standard→basic→flash→advanced）。"
)
OCR_MODE_DESC = (
    "OCR 模式（仅 PDF 生效）：auto=自动判断 / txt=文本抽取（适合文本型 PDF）/ "
    "ocr=OCR（适合扫描件、图片型 PDF）。默认 auto。"
)
PAGE_RANGE_DESC = (
    "PDF 页码范围（从 1 起、闭区间），如 '1-5,8'；'r1' 表示最后一页；"
    "留空或 'all' 表示全部页。默认留空。"
)

app = FastAPI(title="MinerU File Parse JSON", version="1")


class FileParseResult(BaseModel):
    md_content: str


class FileParseResponse(BaseModel):
    task_id: Optional[str] = None
    status: str
    backend: str
    file_names: list[str]
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[Any] = None
    status_url: Optional[str] = None
    result_url: Optional[str] = None
    version: str
    results: dict[str, FileParseResult]


def _extract(zip_bytes: bytes) -> tuple[str, str]:
    markdown = ""
    version = "4.2.3"
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            base = name.split("/")[-1]
            raw = zf.read(name)
            if base == "markdown.md":
                markdown = raw.decode("utf-8", errors="replace")
            elif base in ("structured_content.json", "middle_json.json"):
                try:
                    data = json.loads(raw.decode("utf-8"))
                    v = data.get("metadata", {}).get("producer", {}).get("version")
                    if v:
                        version = str(v)
                except Exception:
                    pass
    return markdown, version


async def _task_timestamps(task_id: Optional[str]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    if not task_id:
        return None, None, None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{API_URL}/v1/tasks/{task_id}")
        if r.status_code == 200:
            d = r.json()
            return d.get("created_at"), d.get("started_at"), d.get("finished_at")
    except Exception:
        pass
    return None, None, None


@app.post(
    "/file_parse",
    response_model=FileParseResponse,
    summary="Synchronously parse one file and return its markdown",
)
async def file_parse(
    request: Request,
    response: Response,
    file: UploadFile = File(..., json_schema_extra={"format": "binary"}, description=FILE_DESC),
    wait_timeout: int = Form(3600, description=WAIT_TIMEOUT_DESC),
    tier: str = Form("standard", json_schema_extra={"enum": TIERS, "nullable": True, "allowEmptyValue": True}, description=TIER_DESC),
    ocr_mode: str = Form("auto", json_schema_extra={"enum": OCR_MODES, "nullable": True, "allowEmptyValue": True}, description=OCR_MODE_DESC),
    page_range: str = Form("", json_schema_extra={"nullable": True, "allowEmptyValue": True}, description=PAGE_RANGE_DESC),
):
    stem = Path(file.filename or "file").stem

    files = {
        "file": (
            file.filename or "file",
            await file.read(),
            file.content_type or "application/octet-stream",
        )
    }
    data = {"wait_timeout": str(wait_timeout)}
    # Only forward non-empty values; empty/omitted fields fall back to upstream defaults.
    for key in ("tier", "ocr_mode", "page_range"):
        value = locals()[key]
        if value:
            data[key] = str(value)

    timeout = httpx.Timeout(connect=10.0, read=3660.0, write=3660.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        upstream = await client.post(f"{FILE_PARSE_URL}/v1/file_parse", files=files, data=data)

    task_id = upstream.headers.get("x-task-id")
    request_id = upstream.headers.get("x-request-id")
    headers = {k: v for k, v in (("X-Request-ID", request_id), ("X-Task-ID", task_id)) if v}

    if upstream.status_code == 200:
        ctype = upstream.headers.get("content-type", "")
        body = upstream.content
        if "zip" in ctype or body[:2] == b"PK":
            markdown, version = _extract(body)
        elif "html" in ctype or "text" in ctype:
            markdown, version = body.decode("utf-8", errors="replace"), "4.2.3"
        else:
            return JSONResponse(
                {"error": f"Unsupported upstream content-type: {ctype}"},
                status_code=502,
            )

        created_at, started_at, finished_at = await _task_timestamps(task_id)
        base = str(request.base_url).rstrip("/")

        response.headers["X-Request-ID"] = request_id or ""
        response.headers["X-Task-ID"] = task_id or ""

        # Match the legacy synchronous /file_parse result shape.
        return {
            "task_id": task_id,
            "status": "completed",
            "backend": "vllm",
            "file_names": [stem],
            "created_at": created_at,
            "started_at": started_at,
            "completed_at": finished_at,
            "error": None,
            "status_url": f"{base}/file_parse/{request_id}" if request_id else None,
            "result_url": f"{base}/file_parse/{request_id}/result" if request_id else None,
            "version": version,
            "results": {stem: {"md_content": markdown}},
        }

    try:
        body = upstream.json()
    except Exception:
        body = {"detail": upstream.text}
    if isinstance(body, dict) and request_id:
        base = str(request.base_url).rstrip("/")
        body["status_url"] = f"{base}/file_parse/{request_id}"
        body["result_url"] = f"{base}/file_parse/{request_id}/result"
    return JSONResponse(body, status_code=upstream.status_code, headers=headers)


@app.get("/file_parse/{request_id}", summary="Query a synchronous parse status")
async def file_parse_status(request: Request, request_id: str):
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{FILE_PARSE_URL}/v1/file_parse/{request_id}")
    try:
        body = r.json()
    except Exception:
        body = {"detail": r.text}
    if isinstance(body, dict):
        base = str(request.base_url).rstrip("/")
        body["status_url"] = f"{base}/file_parse/{request_id}"
        body["result_url"] = f"{base}/file_parse/{request_id}/result"
    return JSONResponse(body, status_code=r.status_code)


@app.get("/file_parse/{request_id}/result", summary="Download a synchronous parse result")
async def file_parse_result(request_id: str):
    async with httpx.AsyncClient(timeout=3660.0) as client:
        r = await client.get(f"{FILE_PARSE_URL}/v1/file_parse/{request_id}/result")
    return Response(
        content=r.content,
        media_type=r.headers.get("content-type", "application/octet-stream"),
        status_code=r.status_code,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8004)
