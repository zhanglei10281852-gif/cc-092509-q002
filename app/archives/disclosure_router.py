from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.database import get_connection, transaction
from app.archives.disclosure_packages import DisclosurePackageService
from app.archives.disclosure_schemas import CarriersSubmit, InventionProjectCreate

router = APIRouter(prefix="/api/disclosure-packages", tags=["发明交底包登记"])


@router.post("/projects", status_code=status.HTTP_201_CREATED)
def create_project(payload: InventionProjectCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return DisclosurePackageService(connection).create_project(principal, payload.model_dump())


@router.get("/projects")
def list_projects(principal: Principal = Depends(current_principal)):
    return DisclosurePackageService(get_connection()).list_projects(principal)


@router.get("/projects/{project_id}")
def project_detail(project_id: int, principal: Principal = Depends(current_principal)):
    return DisclosurePackageService(get_connection()).project_detail(principal, project_id)


@router.post("/projects/{project_id}/carriers", status_code=status.HTTP_201_CREATED)
def submit_carriers(project_id: int, payload: CarriersSubmit, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return DisclosurePackageService(connection).submit_carriers(principal, project_id, payload.model_dump())


@router.post("/projects/{project_id}/versions/{version_id}/submit")
def submit_version(project_id: int, version_id: int, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return DisclosurePackageService(connection).submit_version(principal, project_id, version_id)


@router.get("/projects/{project_id}/versions/{version_id}")
def get_version(project_id: int, version_id: int, principal: Principal = Depends(current_principal)):
    return DisclosurePackageService(get_connection()).get_version(principal, project_id, version_id)


@router.get("/projects/{project_id}/versions/by-no/{version_no}")
def get_version_by_no(project_id: int, version_no: int, principal: Principal = Depends(current_principal)):
    return DisclosurePackageService(get_connection()).get_version_by_no(principal, project_id, version_no)
