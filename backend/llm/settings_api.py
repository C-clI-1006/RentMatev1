"""Local operator model configuration. No secret readback or persistence."""
from ipaddress import ip_address
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, SecretStr

from reporting.api import require_scheduler
from .configuration import (
    ModelConfiguration, configuration_source, get_configuration, set_configuration,
)
from .classifier import QwenClassifier


def require_local_operator(request: Request) -> None:
    try:
        local = request.client is not None and ip_address(request.client.host).is_loopback
    except ValueError:
        local = False
    if not local:
        raise HTTPException(403, "Model settings are available on the local computer only")
    origin = request.headers.get("origin")
    if origin and origin != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "Cross-origin model configuration is not allowed")


router = APIRouter(
    prefix="/api/ops/llm", tags=["Local Model Configuration"],
    dependencies=[Depends(require_scheduler), Depends(require_local_operator)],
)


class SettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    base_url: str
    model: str
    api_key: SecretStr | None = None


def _status(response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    config = get_configuration()
    return {
        "base_url": config.base_url,
        "model": config.model,
        "api_key_configured": bool(config.api_key),
        "configured": all((config.base_url, config.model, config.api_key)),
        "source": configuration_source(),
        "persistence": "server_process_only",
    }


@router.get("/settings")
def status(response: Response):
    return _status(response)


@router.put("/settings")
async def update(request: Request, response: Response):
    # Parse manually so validation errors never echo the submitted secret/body.
    try:
        body = SettingsBody.model_validate(await request.json())
        url = body.base_url.strip().rstrip("/")
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or any(c.isspace() or ord(c) < 32 for c in url)
            or len(url) > 2000
        ):
            raise ValueError()
        # Accessing port also validates malformed URL ports.
        parsed.port
        model = body.model.strip()
        current = get_configuration()
        supplied_key = body.api_key.get_secret_value().strip() if body.api_key else ""
        key = supplied_key or current.api_key
        if not model or len(model) > 200 or any(ord(c) < 32 for c in model):
            raise ValueError()
        if not key or len(key) > 8192 or any(ord(c) < 32 for c in key):
            raise ValueError()
        # Never forward a retained key to a different destination by accident.
        if not supplied_key and url != current.base_url.rstrip("/"):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(422, "Provide a valid API base URL, model and key. Re-enter the key when changing the address.") from None
    set_configuration(ModelConfiguration(base_url=url, model=model, api_key=key))
    return _status(response)


@router.delete("/settings")
def restore(response: Response):
    set_configuration(None)
    return _status(response)


@router.post("/test")
def probe(response: Response):
    response.headers["Cache-Control"] = "no-store"
    configuration = get_configuration()
    if not all((configuration.base_url, configuration.model, configuration.api_key)):
        raise HTTPException(409, "Configure the endpoint, model and API key first")
    result = QwenClassifier().classify("Hello, thank you.")
    if result is None:
        raise HTTPException(502, "Model unavailable or response failed validation. Check the address, model, key and VPN.")
    return {"status": "validated_model_response", "source": "llm",
            "category": result.category.value, "confidence": result.confidence}
