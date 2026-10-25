"""Native homeegram access for authenticated SHB clients.

This router belongs to the normal, admin-authenticated server API, not to the
separately permissioned MCP interface. Both interfaces use the same adapter's
confirmed deletion and durable backup/idempotency journal.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator


class HomeegramDeletion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation_name: str = Field(min_length=1, max_length=500)
    request_id: UUID

    @field_validator("confirmation_name")
    @classmethod
    def require_visible_name(cls, value):
        if not value.strip():
            raise ValueError("Der vollständige Homeegramm-Name muss bestätigt werden")
        # The displayed name is already decoded. Preserve literal percent
        # sequences and leading/trailing spaces for exact identity comparison.
        return value


class HomeegramAccess:
    def __init__(self, database, runtime):
        self.database = database
        self.runtime = runtime
        self.router = APIRouter(prefix="/api/v1/integrations")
        self._routes()

    def source(self, integration_id):
        integration = self.database.integration(integration_id)
        if not integration:
            raise HTTPException(status_code=404, detail="Integration wurde nicht gefunden")
        if integration.get("module_id") != "homee":
            raise HTTPException(status_code=400, detail="Diese Integration ist keine homee-Quelle")
        if not integration.get("enabled"):
            raise HTTPException(status_code=409, detail="Die homee-Integration ist deaktiviert")
        adapter = self.runtime.adapters.get(integration_id)
        if adapter is None or getattr(adapter, "socket", None) is None:
            raise HTTPException(status_code=503, detail="Die homee-Serverintegration ist nicht verbunden")
        return adapter

    def is_current_source(self, integration_id, adapter):
        integration = self.database.integration(integration_id)
        return bool(integration and integration.get("module_id") == "homee"
                    and integration.get("enabled")
                    and self.runtime.adapters.get(integration_id) is adapter
                    and getattr(adapter, "socket", None) is not None)

    @staticmethod
    def upstream_error(error):
        if isinstance(error, TimeoutError):
            return HTTPException(status_code=504, detail=str(error))
        if isinstance(error, ConnectionError):
            return HTTPException(status_code=503, detail=str(error))
        if isinstance(error, (ValueError, KeyError)):
            return HTTPException(status_code=409, detail=str(error))
        return HTTPException(status_code=502, detail=str(error))

    def _routes(self):
        @self.router.get("/{integration_id}/homeegrams")
        async def list_homeegrams(integration_id: str, response: Response):
            response.headers["Cache-Control"] = "no-store"
            adapter = self.source(integration_id)
            refresh = getattr(adapter, "_refresh_homeegram_snapshot", None)
            if not callable(refresh):
                raise HTTPException(status_code=501, detail="Das homee-Servermodul muss aktualisiert werden")
            try:
                await refresh(adapter.socket)
            except Exception as error:
                raise self.upstream_error(error) from error
            if not self.is_current_source(integration_id, adapter):
                raise HTTPException(status_code=409, detail="Die homee-Quelle wurde während des Abrufs geändert")
            rows = getattr(adapter, "resources", {}).get("homeegrams")
            if not isinstance(rows, list):
                raise HTTPException(status_code=502, detail="homee hat keine vollständige Homeegramm-Liste geliefert")
            return {"integration_id": integration_id, "homeegrams": deepcopy(rows)}

        @self.router.post("/{integration_id}/homeegrams/{homeegram_id}/delete")
        async def delete_homeegram(integration_id: str,
                                  homeegram_id: Annotated[int, Path(gt=0)],
                                  payload: HomeegramDeletion, response: Response):
            response.headers["Cache-Control"] = "no-store"
            adapter = self.source(integration_id)
            delete = getattr(adapter, "delete_homeegram_confirmed", None)
            if not callable(delete):
                raise HTTPException(status_code=501, detail="Das homee-Servermodul unterstützt bestätigtes Löschen noch nicht; Server aktualisieren")
            request_id = str(payload.request_id)
            try:
                result = await delete(homeegram_id, payload.confirmation_name, request_id,
                                      authorize=lambda: self.is_current_source(integration_id, adapter))
            except Exception as error:
                raise self.upstream_error(error) from error
            if (not isinstance(result, dict) or result.get("deleted") is not True
                    or result.get("status") != "deleted"
                    or result.get("homeegram_id") != homeegram_id
                    or result.get("name") != payload.confirmation_name):
                raise HTTPException(status_code=502, detail="homee hat das Löschen dieses Homeegramms nicht eindeutig bestätigt")
            # Backups can contain notification text and webhook secrets. Keep
            # them in the adapter journal; return deletion metadata only.
            public = {key: result[key] for key in
                      ("homeegram_id", "name", "deleted", "status", "backup_saved", "replayed")
                      if key in result}
            return {**public, "integration_id": integration_id, "request_id": request_id}
