from fastapi import Request
from fastapi.responses import JSONResponse

class AppError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code, self.message, self.status = code, message, status

async def app_error_handler(_request: Request, exc: AppError):
    return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}})
