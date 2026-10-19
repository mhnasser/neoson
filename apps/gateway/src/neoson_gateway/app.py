from fastapi import APIRouter, FastAPI

from neoson_gateway import __version__

health = APIRouter(prefix="/api/v1/health", tags=["health"])


@health.get("/live")
async def live() -> dict[str, str]:
    """Indica que o processo do gateway está ativo."""
    return {"status": "ok"}


def create_app() -> FastAPI:
    app = FastAPI(title="NeoSon Gateway", version=__version__)
    app.include_router(health)
    return app
