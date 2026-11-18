from fastapi import APIRouter

from app.routers import contract, core

api = APIRouter()
api.include_router(core.router)
api.include_router(contract.router)
