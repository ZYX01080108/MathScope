from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, FastAPI, File, HTTPException, Path as ApiPath, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response

from src.api_models import (
    AnswerCheckRequest,
    AnswerCheckResponse,
    AnswerRequest,
    ContextRequest,
    ContextResponse,
    DocumentInfo,
    DocumentListResponse,
    DocumentSearchResponse,
    DocumentAnswerResponse,
    FormulaQualityRequest,
    FormulaQualityResponse,
    GenerationStatus,
    ReviewedFormulaContext,
    ReviewedFormulaAnswer,
    ReviewedAnswerRequest,
    ParseRequest,
    ParseResponse,
    SearchRequest,
    SearchResponse,
    UploadResponse,
)
from src.answer_check import AnswerCheckError, check_answer_draft
from src.answer_generation import generate_answer
from src.baseline import load_optional_baseline
from src.document_catalog import DocumentCatalogError, get_document_info, list_documents
from src.document_files import DocumentFileError, get_document_file
from src.document_processing import DocumentProcessingError, process_document
from src.document_search import DocumentSearchError, DocumentSearchService
from src.document_upload import MAX_UPLOAD_BYTES, UploadValidationError, save_pdf_upload
from src.deepseek_client import DeepSeekClient, DeepSeekSettings, GenerationError
from src.evidence_context import EvidenceContextError, build_evidence_context
from src.formula_quality import assess_document_formula_quality
from src.reviewed_formula import build_reviewed_context, generate_reviewed_answer
from src.settings import Settings


router = APIRouter()
WEB_DIRECTORY = Path(__file__).resolve().parent / "web"
WEB_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
}


@router.get("/", include_in_schema=False)
def workspace_page():
    return FileResponse(WEB_DIRECTORY / "index.html", media_type="text/html", headers=WEB_HEADERS)


@router.get("/ui/app.css", include_in_schema=False)
def workspace_styles():
    return FileResponse(WEB_DIRECTORY / "app.css", media_type="text/css", headers=WEB_HEADERS)


@router.get("/ui/app.js", include_in_schema=False)
def workspace_script():
    return FileResponse(WEB_DIRECTORY / "app.js", media_type="application/javascript", headers=WEB_HEADERS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = app.state.settings
    app.state.retrievers, app.state.baseline_unavailable_reason = load_optional_baseline(settings.baseline_chunks_path)

    app.state.document_search = DocumentSearchService(
        settings.upload_directory, settings.parsed_directory,
        max_cached_indexes=settings.index_cache_size,
    )
    app.state.deepseek = DeepSeekClient(app.state.deepseek_settings)

    try:
        yield
    finally:
        app.state.retrievers.clear()
        app.state.document_search.clear()


@router.get("/health")
def health():
    return {
        "status": "ok",
        "service": "MathScope",
    }


@router.get("/generation-status", response_model=GenerationStatus,
            summary="查看本机模型配置及尝试次数（不调用模型、不验证密钥或余额）")
def generation_status(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return request.app.state.deepseek.public_status()


@router.get(
    "/documents",
    response_model=DocumentListResponse,
    summary="列出文档及磁盘上的处理状态（只读）",
)
def document_list(
    request: Request,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    try:
        settings = request.app.state.settings
        return list_documents(settings.upload_directory, settings.parsed_directory, offset=offset, limit=limit)
    except DocumentCatalogError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=500, detail="文档目录读取失败，请检查服务器存储") from error


@router.get(
    "/documents/{document_id}",
    response_model=DocumentInfo,
    summary="查看单个文档及磁盘上的处理状态（只读）",
    responses={404: {"description": "文档不存在"}},
)
def document_detail(
    document_id: Annotated[str, ApiPath(pattern=r"^[0-9a-f]{32}$")],
    request: Request,
):
    try:
        settings = request.app.state.settings
        return get_document_info(document_id, settings.upload_directory, settings.parsed_directory)
    except DocumentCatalogError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=500, detail="文档信息读取失败，请检查服务器存储") from error


@router.get(
    "/documents/{document_id}/file",
    response_class=Response,
    summary="查看经来源校验的原始 PDF（只读）",
    responses={
        200: {"content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}}},
        404: {"description": "原文件不存在或上传记录不完整"},
        409: {"description": "来源路径、记录或文件指纹不一致"},
        413: {"description": "原文件超过当前读取上限"},
        500: {"description": "原文件读取失败"},
    },
)
def original_document(
    document_id: Annotated[str, ApiPath(pattern=r"^[0-9a-f]{32}$")],
    request: Request,
    sha256: Annotated[str | None, Query(
        pattern=r"^[0-9a-f]{64}$",
        description="可选的预期文件指纹；证据链接携带此值以防打开另一版本",
    )] = None,
):
    try:
        snapshot = get_document_file(
            document_id, request.app.state.settings.upload_directory, expected_sha256=sha256,
        )
    except DocumentFileError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=500, detail="原文件读取失败，请检查服务器存储") from error
    # Serve precisely the checked bytes, not a FileResponse that reopens the path.
    return Response(
        content=snapshot.content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{document_id}.pdf"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Accept-Ranges": "none",
            "ETag": f'"{snapshot.sha256}"',
        },
    )


@router.post(
    "/documents/{document_id}/quality",
    response_model=FormulaQualityResponse,
    summary="只读检查检索证据页的公式提取风险（不恢复公式，不调用模型）",
    responses={404: {"description": "文档不存在"}, 409: {"description": "尚未解析或来源改变"},
               413: {"description": "文档数据超限"}, 422: {"description": "检索参数不合法"}},
)
def document_formula_quality(
    document_id: Annotated[str, ApiPath(pattern=r"^[0-9a-f]{32}$")],
    body: FormulaQualityRequest,
    request: Request,
):
    try:
        retrieved = request.app.state.document_search.search(document_id, body.query, method=body.method, k=body.k)
        packet = build_evidence_context(retrieved, max_context_chars=body.max_context_chars)
        return assess_document_formula_quality(packet, request.app.state.settings.upload_directory)
    except (DocumentSearchError, EvidenceContextError, AnswerCheckError, DocumentFileError) as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=500, detail="文档质量检查读取失败，请检查服务器存储") from error


@router.post(
    "/documents",
    response_model=UploadResponse,
    status_code=201,
    summary="上传并保存 PDF（尚未解析）",
    responses={
        400: {"description": "空文件"},
        413: {"description": "文件超过 10 MiB 大小上限"},
        415: {"description": "文件名或文件头不符合 PDF 要求"},
        500: {"description": "文件保存失败"},
    },
)
def upload_document(
    file: Annotated[UploadFile, File(description="单个 PDF，最多 10 MiB")],
    request: Request,
):
    try:
        if file.size is not None and file.size > MAX_UPLOAD_BYTES:
            raise UploadValidationError(413, "文件超过上传大小上限（默认 10 MiB）")
        return save_pdf_upload(
            file.file, file.filename, request.app.state.settings.upload_directory,
            max_bytes=MAX_UPLOAD_BYTES,
        )
    except UploadValidationError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=500, detail="文件保存失败，请检查服务器存储") from error
    finally:
        file.file.close()


@router.post(
    "/documents/{document_id}/parse",
    response_model=ParseResponse,
    summary="按文档编号提取页面与分块（尚未加入索引）",
    responses={
        404: {"description": "上传文档不存在"},
        409: {"description": "文件指纹改变、记录冲突或已有解析结果"},
        413: {"description": "超过页数或提取文本上限"},
        422: {"description": "参数不合法、PDF 损坏或加密"},
        500: {"description": "上传记录损坏或解析结果保存失败"},
    },
)
def parse_document(
    document_id: Annotated[str, ApiPath(pattern=r"^[0-9a-f]{32}$")],
    body: ParseRequest,
    request: Request,
):
    try:
        settings = request.app.state.settings
        return process_document(
            document_id, settings.upload_directory, settings.parsed_directory,
            chunk_size=body.chunk_size,
        )
    except DocumentProcessingError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=500, detail="解析结果读取或保存失败，请检查服务器存储") from error


@router.post(
    "/documents/{document_id}/search",
    response_model=DocumentSearchResponse,
    summary="只检索指定的已解析文档",
    responses={
        404: {"description": "上传文档不存在"},
        409: {"description": "尚未解析、数据不完整或来源版本不一致"},
        413: {"description": "解析数据超过当前加载或索引上限"},
        422: {"description": "参数不合法或没有可检索的词"},
        500: {"description": "数据读取失败"},
    },
)
def search_document(
    document_id: Annotated[str, ApiPath(pattern=r"^[0-9a-f]{32}$")],
    body: SearchRequest,
    request: Request,
):
    try:
        return request.app.state.document_search.search(
            document_id, body.query, method=body.method, k=body.k,
        )
    except DocumentSearchError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=500, detail="文档数据读取失败，请检查服务器存储") from error


@router.post(
    "/documents/{document_id}/context",
    response_model=ContextResponse,
    summary="检索并按字符预算打包编号证据（不调用模型）",
    responses={
        404: {"description": "上传文档不存在"},
        409: {"description": "尚未解析、来源数据或检索结果不一致"},
        413: {"description": "文档数据超过检索加载上限"},
        422: {"description": "查询、检索参数或字符预算不合法"},
        500: {"description": "数据读取失败"},
    },
)
def document_context(
    document_id: Annotated[str, ApiPath(pattern=r"^[0-9a-f]{32}$")],
    body: ContextRequest,
    request: Request,
):
    try:
        retrieved = request.app.state.document_search.search(
            document_id, body.query, method=body.method, k=body.k,
        )
        return build_evidence_context(retrieved, max_context_chars=body.max_context_chars)
    except (DocumentSearchError, EvidenceContextError) as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=500, detail="文档数据读取失败，请检查服务器存储") from error


@router.post(
    "/documents/{document_id}/check-answer",
    response_model=AnswerCheckResponse,
    summary="核验草稿格式和字面引用（不调用模型，不核验语义）",
    responses={
        404: {"description": "文档不存在"},
        409: {"description": "来源或证据包已改变；请重新获取 context"},
        413: {"description": "文档数据超过检索加载上限"},
        422: {"description": "参数或草稿字段格式不合法"},
        500: {"description": "数据读取失败"},
    },
)
def check_document_answer(
    document_id: Annotated[str, ApiPath(pattern=r"^[0-9a-f]{32}$")],
    body: AnswerCheckRequest,
    request: Request,
):
    try:
        retrieved = request.app.state.document_search.search(
            document_id, body.query, method=body.method, k=body.k,
        )
        packet = build_evidence_context(retrieved, max_context_chars=body.max_context_chars)
        if packet["context_id"] != body.context_id:
            raise AnswerCheckError(409, "当前问题、参数或证据包已改变，请重新获取 context")
        return check_answer_draft(packet, body.draft)
    except (DocumentSearchError, EvidenceContextError, AnswerCheckError) as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=500, detail="文档数据读取失败，请检查服务器存储") from error


@router.post(
    "/documents/{document_id}/answer",
    response_model=DocumentAnswerResponse,
    summary="先检查公式风险，通过后发送证据到 DeepSeek（可能收费，不核验语义）",
    responses={
        404: {"description": "文档不存在"},
        409: {"description": "来源异常或模型调用期间证据包已改变，回答不交付"},
        413: {"description": "文档数据超过检索加载上限"},
        422: {"description": "参数、外发许可或字符预算不合法"},
        429: {"description": "已有模型请求进行中或当前实例调用次数用尽"},
        502: {"description": "上游请求或响应不合法，不自动重试"},
        503: {"description": "未启用、未配置密钥、权限/余额异常或上游限流"},
        504: {"description": "上游超时；可能已计费，不自动重试"},
    },
)
def answer_document(
    document_id: Annotated[str, ApiPath(pattern=r"^[0-9a-f]{32}$")],
    body: AnswerRequest,
    request: Request,
):
    try:
        service = request.app.state.document_search
        retrieved = service.search(document_id, body.query, method=body.method, k=body.k)
        packet = build_evidence_context(retrieved, max_context_chars=body.max_context_chars)
        quality = assess_document_formula_quality(packet, request.app.state.settings.upload_directory)
        answer = generate_answer(packet, request.app.state.deepseek, formula_quality=quality)
        # The upstream round trip may outlast a local data edit. Don't hand off
        # citations as current after the evidence/version changed during the call.
        if answer["generation"]["model_called"]:
            current = service.search(document_id, body.query, method=body.method, k=body.k)
            current_packet = build_evidence_context(current, max_context_chars=body.max_context_chars)
            if current_packet["context_id"] != packet["context_id"]:
                raise AnswerCheckError(409, "模型调用期间证据包已改变，回答已丢弃；本次可能已产生费用")
        return answer
    except (DocumentSearchError, EvidenceContextError, AnswerCheckError, DocumentFileError, GenerationError) as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=500, detail="文档数据读取失败，请检查服务器存储") from error


@router.post(
    "/search",
    response_model=SearchResponse,
    summary="检索可选的默认 lecture 实验基线",
    responses={503: {"description": "默认基线缺失、为空、损坏或没有可检索词；文档服务仍可用"}},
)
def search(body: SearchRequest, request: Request):
    retriever = request.app.state.retrievers.get(body.method)
    if retriever is None:
        raise HTTPException(
            status_code=503,
            detail=request.app.state.baseline_unavailable_reason or "默认基线尚未就绪",
        )

    try:
        results = retriever.search(body.query, k=body.k)
    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail=str(error),
        ) from error

    return {
        "query": body.query,
        "method": body.method,
        "k": body.k,
        "returned_count": len(results),
        "results": results,
    }


@router.post(
    "/documents/{document_id}/reviewed-context",
    response_model=ReviewedFormulaContext,
    summary="只读预览与检索证据匹配的已核对公式注释（不调用模型）",
    responses={404: {"description": "文档不存在"}, 409: {"description": "来源或核对记录不一致"},
               413: {"description": "文档数据超限"}, 422: {"description": "参数不合法"}},
)
def reviewed_document_context(
    document_id: Annotated[str, ApiPath(pattern=r"^[0-9a-f]{32}$")],
    body: FormulaQualityRequest,
    request: Request,
):
    try:
        retrieved = request.app.state.document_search.search(document_id, body.query, method=body.method, k=body.k)
        raw_packet = build_evidence_context(retrieved, max_context_chars=body.max_context_chars)
        return build_reviewed_context(raw_packet, request.app.state.settings.upload_directory)
    except (DocumentSearchError, EvidenceContextError, AnswerCheckError, DocumentFileError) as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=500, detail="公式核对证据读取失败，请检查服务器存储") from error


@router.post(
    "/documents/{document_id}/answer-reviewed",
    response_model=ReviewedFormulaAnswer,
    summary="仅从已核对公式注释逐字选择结论（可能收费，不新增证明）",
    responses={404: {"description": "文档不存在"}, 409: {"description": "来源/注释不一致或调用期间版本改变"},
               413: {"description": "文档数据超限"}, 422: {"description": "参数或外发许可不合法"},
               429: {"description": "模型请求进行中或共用调用次数用尽"}, 502: {"description": "上游响应异常"},
               503: {"description": "模型配置/权限/余额/限流异常"}, 504: {"description": "模型超时，可能已收费"}},
)
def answer_reviewed_document(
    document_id: Annotated[str, ApiPath(pattern=r"^[0-9a-f]{32}$")],
    body: ReviewedAnswerRequest,
    request: Request,
):
    try:
        service = request.app.state.document_search
        retrieved = service.search(document_id, body.query, method=body.method, k=body.k)
        raw_packet = build_evidence_context(retrieved, max_context_chars=body.max_context_chars)
        packet = build_reviewed_context(raw_packet, request.app.state.settings.upload_directory)
        if body.expected_reviewed_context_id is not None and body.expected_reviewed_context_id != packet["reviewed_context_id"]:
            raise AnswerCheckError(409, "核对证据与预览版本不同；本次未调用模型，请重新预检")
        answer = generate_reviewed_answer(packet, request.app.state.deepseek)
        if answer["generation"]["model_called"]:
            current = service.search(document_id, body.query, method=body.method, k=body.k)
            current_raw = build_evidence_context(current, max_context_chars=body.max_context_chars)
            current_packet = build_reviewed_context(current_raw, request.app.state.settings.upload_directory)
            if current_packet["reviewed_context_id"] != packet["reviewed_context_id"]:
                raise AnswerCheckError(409, "模型调用期间来源、原文证据或核对注释已改变；回答已丢弃，本次可能已收费")
        return answer
    except (DocumentSearchError, EvidenceContextError, AnswerCheckError, DocumentFileError, GenerationError) as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=500, detail="公式核对证据读取失败，请检查服务器存储") from error


def create_app(settings: Settings | None = None, *, deepseek_settings: DeepSeekSettings | None = None):
    settings = Settings.from_env() if settings is None else settings
    if not isinstance(settings, Settings):
        raise TypeError("settings 必须是 Settings 对象")
    deepseek_settings = DeepSeekSettings.from_env() if deepseek_settings is None else deepseek_settings
    if not isinstance(deepseek_settings, DeepSeekSettings):
        raise TypeError("deepseek_settings 必须是 DeepSeekSettings 对象")
    application = FastAPI(title="MathScope API", lifespan=lifespan)
    application.state.settings = settings
    application.state.deepseek_settings = deepseek_settings
    application.include_router(router)
    return application


# Keep the existing `python -m uvicorn src.api:app ...` entry point.
app = create_app()
