from fastapi import APIRouter

from aiive.supervisor.launcher import Launcher

router = APIRouter(prefix="/api/selfdev")


@router.get("/slots")
def list_slots():
    launcher = Launcher()
    return launcher.get_status()


@router.post("/slots/health-check")
def health_check():
    launcher = Launcher()
    return launcher.health_check()
