from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.api.dependencies import current_principal
from app.archives.disclosure_schemas import (
    DisclosureProjectCreate,
    PackageSubmissionCreate,
    PackageSubmit,
)
from app.archives.disclosure_service import DisclosurePackageService
from app.core.security import Principal
from app.database import get_connection, transaction

router = APIRouter(prefix="/api/disclosure-packages", tags=["交底包登记"])


@router.post("/projects", status_code=status.HTTP_201_CREATED)
def create_project(payload: DisclosureProjectCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return DisclosurePackageService(connection).create_project(principal, payload.model_dump())


@router.get("/projects/{project_code}")
def get_project(project_code: str, principal: Principal = Depends(current_principal)):
    return DisclosurePackageService(get_connection()).get_project(principal, project_code)


@router.post("/projects/{project_code}/submissions", status_code=status.HTTP_201_CREATED)
def submit_carriers(project_code: str, payload: PackageSubmissionCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return DisclosurePackageService(connection).submit_carriers(principal, project_code, payload.model_dump())


@router.post("/projects/{project_code}/submit")
def submit_version(project_code: str, payload: PackageSubmit, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return DisclosurePackageService(connection).submit_version(principal, project_code, payload.model_dump())


@router.get("/projects/{project_code}/versions")
def list_versions(project_code: str, principal: Principal = Depends(current_principal)):
    return DisclosurePackageService(get_connection()).list_versions(principal, project_code)


@router.get("/projects/{project_code}/versions/{version_no}")
def version_detail(project_code: str, version_no: int, principal: Principal = Depends(current_principal)):
    return DisclosurePackageService(get_connection()).version_detail(principal, project_code, version_no)


@router.get("/projects/{project_code}/events")
def project_events(project_code: str, principal: Principal = Depends(current_principal)):
    return DisclosurePackageService(get_connection()).project_events(principal, project_code)


@router.get("/carriers/{carrier_id}/verify")
def verify_carrier(carrier_id: int, principal: Principal = Depends(current_principal)):
    return DisclosurePackageService(get_connection()).verify_carrier(principal, carrier_id)
