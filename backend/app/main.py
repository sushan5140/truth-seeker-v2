from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.models import ClaimRequest, ClaimResponse, ToneRequest, ToneResponse, TruthStatusResponse
from app.services.tone_checker import check_tone, is_gemini_configured
from app.services.truth.pipeline import ClaimVerificationService


async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    first = errors[0] if errors else {}
    error_type = first.get("type", "")
    if error_type == "string_too_long":
        message = "Text must be 5,000 characters or fewer."
    elif error_type in {"string_too_short", "missing"}:
        message = "Text cannot be empty."
    else:
        message = "The request is invalid. Send JSON with a text field containing 1 to 5,000 characters."
    return JSONResponse(status_code=422, content={"status": "invalid", "message": message, "errors": errors})


def get_truth_service(request: Request) -> ClaimVerificationService:
    """Resolve the app-scoped service so tests never need the runtime cache."""
    service = request.app.state.truth_service
    if service is None:
        service = ClaimVerificationService()
        request.app.state.truth_service = service
    return service


TruthServiceDependency = Annotated[ClaimVerificationService, Depends(get_truth_service)]


def create_app(service: ClaimVerificationService | None = None) -> FastAPI:
    application = FastAPI(title="Truth & Tone Guardian", version="2.0.0")
    application.state.truth_service = service
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type"],
    )
    application.add_exception_handler(RequestValidationError, validation_error_handler)

    @application.get("/health")
    def health() -> dict[str, str | bool]:
        return {
            "status": "ok",
            "service": "truth-tone-guardian",
            "backend_running": True,
            "gemini_configured": is_gemini_configured(),
        }

    @application.get("/api/truth/status", response_model=TruthStatusResponse)
    def truth_status(active_service: TruthServiceDependency) -> TruthStatusResponse:
        return active_service.status()

    @application.post("/api/check-claim", response_model=ClaimResponse)
    def claim_endpoint(request: ClaimRequest, active_service: TruthServiceDependency) -> ClaimResponse:
        return active_service.check(request)

    @application.delete("/api/truth/cache")
    def clear_truth_cache(active_service: TruthServiceDependency) -> dict[str, int | str]:
        return {"status": "cleared", "entries_removed": active_service.cache.clear()}

    @application.post("/api/check-tone", response_model=ToneResponse)
    def tone_endpoint(request: ToneRequest) -> ToneResponse:
        return check_tone(request.text, request.sensitivity)

    return application


app = create_app()
