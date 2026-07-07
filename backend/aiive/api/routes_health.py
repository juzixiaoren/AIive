from fastapi import APIRouter

from aiive.config import settings

router = APIRouter()


@router.get("/health")
async def health_check():
    return {
        "ok": True,
        "service": settings.app_name,
        "version": settings.app_version,
    }
