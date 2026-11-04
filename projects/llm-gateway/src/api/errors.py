from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette import status as http_status

from contracts import ErrorResponse
from errors import GatewayError, ProviderCallError


def error_response(state: dict, status: int, code: str, message: str, headers=None) -> JSONResponse:
    state["error_code"] = code
    return JSONResponse(
        status_code=status, headers=headers,
        content=ErrorResponse(code=code, message=message).model_dump(),
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        return error_response(request.scope["state"], http_status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_request", "请求参数不合法")

    @app.exception_handler(GatewayError)
    async def gateway_error(request: Request, exc: GatewayError):
        return error_response(request.scope["state"], exc.status_code, exc.code, exc.message)

    @app.exception_handler(ProviderCallError)
    async def provider_error(request: Request, exc: ProviderCallError):
        return error_response(request.scope["state"], exc.status_code, exc.code, exc.message)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        code, message = {
            http_status.HTTP_404_NOT_FOUND: ("not_found", "路径不存在"),
            http_status.HTTP_405_METHOD_NOT_ALLOWED: ("method_not_allowed", "请求方法不允许"),
        }.get(exc.status_code, ("http_error", "HTTP 请求失败"))
        return error_response(request.scope["state"], exc.status_code, code, message, exc.headers)
