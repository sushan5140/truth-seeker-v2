from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.models import ClaimRequest, ClaimResponse, ToneRequest, ToneResponse, TruthStatusResponse
from app.services.tone_checker import check_tone, is_gemini_configured
from app.services.truth.pipeline import ClaimVerificationService


app = FastAPI(title="Truth & Tone Guardian", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type"],
)


@app.exception_handler(RequestValidationError)
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


@app.get("/health")
def health() -> dict[str, str | bool]:
    return {
        "status": "ok",
        "service": "truth-tone-guardian",
        "backend_running": True,
        "gemini_configured": is_gemini_configured(),
    }


truth_service = ClaimVerificationService()


@app.get("/api/truth/status", response_model=TruthStatusResponse)
def truth_status() -> TruthStatusResponse:
    return truth_service.status()


@app.post("/api/check-claim", response_model=ClaimResponse)
def claim_endpoint(request: ClaimRequest) -> ClaimResponse:
    return truth_service.check(request)


@app.delete("/api/truth/cache")
def clear_truth_cache() -> dict[str, int | str]:
    return {"status": "cleared", "entries_removed": truth_service.cache.clear()}


@app.post("/api/check-tone", response_model=ToneResponse)
def tone_endpoint(request: ToneRequest) -> ToneResponse:
    return check_tone(request.text, request.sensitivity)
