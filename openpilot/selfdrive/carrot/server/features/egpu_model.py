from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from functools import partial
import logging
from threading import Event
from typing import Any

from aiohttp import web
from openpilot.common.jetlink_status import diagnostics as jetlink_diagnostics
from openpilot.common.egpu_recovery_status import read_status as read_egpu_recovery

from openpilot.selfdrive.modeld.big_model import active_manifest, active_model_compiled, active_model_path, model_cache_dir
from openpilot.selfdrive.modeld.big_model_status import read_big_model_status, write_big_model_status
from openpilot.selfdrive.modeld.egpu_worker_progress import current_failure, saved_failure
from openpilot.selfdrive.modeld.egpu_rejection import rejection_status, request_retry

from ..services.params import HAS_PARAMS, Params


logger = logging.getLogger(__name__)


class InternalPreparation:
  def __init__(self):
    self.lock = asyncio.Lock()
    self.closing = Event()
    self.task: asyncio.Task[None] | None = None
    self.error: str | None = None

  def running(self) -> bool:
    return self.task is not None and not self.task.done()


INTERNAL_PREPARATION = web.AppKey('internal_model_preparation', InternalPreparation)


def _internal_offroad(params: Any, preparation: InternalPreparation) -> bool:
  try:
    return (not preparation.closing.is_set() and params is not None and params.get_bool('IsOffroad') and
            not params.get_bool('IsOnroad') and not params.get_bool('IsEngaged'))
  except Exception:
    return False


async def _internal_status(preparation: InternalPreparation, offroad: Callable[[], bool]) -> dict[str, Any]:
  from openpilot.selfdrive.modeld import internal_model
  status = await asyncio.to_thread(internal_model.status, offroad())
  if preparation.running():
    status['preparing'] = True
    status['can_change'] = False
    status['can_prepare'] = False
  if preparation.error is not None:
    status['error'] = preparation.error
  return status


async def _prepare_internal(preparation: InternalPreparation, offroad: Callable[[], bool]) -> None:
  from openpilot.selfdrive.modeld import internal_model
  try:
    await asyncio.to_thread(internal_model.prepare, offroad)
  except Exception:
    if offroad():
      preparation.error = 'preparation_failed'
      logger.exception('Polaris Vega preparation failed')
    else:
      preparation.error = 'preparation_interrupted'


def _params_bool(params: Any, key: str) -> bool:
  try:
    return bool(params.get_bool(key))
  except Exception:
    return False


def build_status_payload(params: Any | None = None) -> dict[str, Any]:
  params = params if params is not None else (Params() if HAS_PARAMS and Params is not None else None)
  hardware_seen = _params_bool(params, "UsbGpuHardwareSeen") if params is not None else False
  if not hardware_seen:
    return {"ok": True, "available": False}

  status = read_big_model_status(model_cache_dir()) or {}
  manifest = active_manifest()
  # Status describes the model being updated; active_manifest can still refer
  # to the previous model until the new download passes verification.
  same_model = manifest is not None and (not status or status.get("sha256") == manifest.sha256)
  phase = status.get("state")
  updating = phase in {"checking", "downloading", "verifying", "compiling", "error", "waiting_for_network", "installing"}
  try:
    compiled = active_model_compiled() if same_model and not updating else False
  except Exception:
    compiled = False
  if updating:
    state = phase
  elif compiled:
    state = "installed" if phase == "installed" else "compiled"
  elif same_model:
    state = "waiting_for_ignition" if phase == "waiting_for_ignition" else "ready"
  else:
    state = "checking"
  fallback_size = manifest.size if same_model else 0
  downloaded = int(status.get("downloaded_bytes", fallback_size))
  total = int(status.get("total_bytes", fallback_size))
  progress = round(min(100.0, max(0.0, downloaded * 100.0 / total)), 1) if total > 0 else None
  engaged = _params_bool(params, "IsEngaged") if params is not None else False
  active = (_params_bool(params, "UsbGpuActive") and not _params_bool(params, "UsbGpuLoading")) if params is not None else False
  error_code, detail = status.get('error_code'), status.get('detail')
  blocked = manifest is not None and (model_cache_dir() / 'precompiled' / manifest.sha256 / 'rejected').exists()
  rejection = rejection_status(model_cache_dir() / 'precompiled' / manifest.sha256) if same_model else {'blocked': False}
  if params is not None and _params_bool(params, 'UsbGpuStartupFailed') and not active:
    failure = current_failure(model_cache_dir(), manifest.sha256) if manifest else {}
    state, error_code = 'error', failure.get('error_code', 'runtime')
    worker = failure.get('worker') or {}
    stage = worker.get('stage')
    detail = f'Last worker stage: {stage}' if isinstance(stage, str) else None

  recovery = read_egpu_recovery() if not active else {}
  if recovery and recovery['state'] in ('waiting', 'recovering', 'ready'):
    state = 'recovery_ready' if recovery['state'] == 'ready' else 'recovering'
    error_code, detail = None, None
  elif recovery.get('state') == 'stopped':
    state, error_code, detail = 'recovery_stopped', None, None

  if blocked and same_model and not active:
    compiled = False
    state, error_code = 'error', 'rejected'
    detail = rejection.get('error')
  rejection['can_request'] = bool(rejection.get('can_request') and same_model and not active and not engaged and
                                 params is not None and _params_bool(params, 'IsOffroad') and
                                 not _params_bool(params, 'IsOnroad') and not _params_bool(params, 'UsbGpuLoading') and
                                 phase not in {'checking', 'downloading', 'verifying', 'compiling', 'installing', 'waiting_for_network'} and
                                 recovery.get('state') not in {'waiting', 'recovering', 'ready'})

  from openpilot.common.big_model_catalog import catalog
  displayed_sha = status.get('sha256') if status else (manifest.sha256 if manifest is not None else None)
  entry = next((row for row in catalog().values() if row['egpu']['sha256'] == displayed_sha), None)
  return {
    "ok": True,
    "available": True,
    "display_name": entry['name'] if entry else None,
    "state": state,
    "model_id": status.get("model_id") if status else (manifest.model_id if manifest is not None else None),
    "sha256": status.get("sha256") if status else (manifest.sha256 if manifest is not None else None),
    "downloaded_bytes": downloaded,
    "total_bytes": total,
    "progress": progress,
    "detail": detail,
    "error_code": error_code,
    "retry_count": status.get("retry_count", 0),
    "retry_in_seconds": status.get("retry_in_seconds", 0),
    "active": active,
    "recovery": recovery,
    "rejection": rejection,
    "started_at": status.get("started_at"),
    "updated_at": status.get("updated_at"),
    "compiled": compiled,
    "engaged": engaged,
    "can_restart": not blocked and error_code != 'rejected' and not recovery and same_model and not compiled and not engaged and state not in {
      "checking", "downloading", "verifying", "compiling", "waiting_for_network", "installing",
    },
  }


async def api_status(request: web.Request) -> web.Response:
  from openpilot.selfdrive.modeld.model_library import status
  params = Params() if HAS_PARAMS and Params is not None else None
  preparation = request.app[INTERNAL_PREPARATION]
  return web.json_response({**build_status_payload(params), 'jetlink': jetlink_diagnostics(),
                            'library': await asyncio.to_thread(status),
                            'internal': await _internal_status(preparation, partial(_internal_offroad, params, preparation))})


async def api_internal_model(request: web.Request) -> web.Response:
  from openpilot.selfdrive.modeld import internal_model
  params = Params() if HAS_PARAMS and Params is not None else None
  preparation = request.app[INTERNAL_PREPARATION]
  offroad = partial(_internal_offroad, params, preparation)

  try:
    body = await request.json()
  except (ValueError, TypeError):
    body = None

  error = None
  if not isinstance(body, dict) or set(body) != {'action', 'model'}:
    error = 'invalid_request'
  elif body['action'] not in ('prepare', 'select') or body['model'] not in ('cd210', 'polaris-vega'):
    error = 'invalid_request'
  else:
    action, model = body['action'], body['model']
    async with preparation.lock:
      if preparation.running():
        error = 'preparation_busy'
      elif not offroad():
        error = 'offroad_required'
      elif action == 'select':
        try:
          await asyncio.to_thread(internal_model.select, model, offroad)
        except (OSError, RuntimeError, ValueError) as exc:
          error = 'selection_failed' if offroad() else 'offroad_required'
          if not isinstance(exc, ValueError):
            logger.exception('Failed to select the internal model')
      elif model != 'polaris-vega' or not internal_model.can_prepare():
        error = 'preparation_unavailable'
      else:
        preparation.error = None
        preparation.task = asyncio.create_task(_prepare_internal(preparation, offroad), name='internal-model-preparation')

  response = {'ok': error is None, 'internal': await _internal_status(preparation, offroad)}
  if error is not None:
    response['error'] = error
  return web.json_response(response, status=200 if error is None else 409)


async def api_diagnostics(_request: web.Request) -> web.Response:
  manifest = active_manifest()
  failure = saved_failure(model_cache_dir(), manifest.sha256) if manifest else {}
  if not failure:
    return web.json_response({'ok': False, 'error': 'No matching saved eGPU failure report.'}, status=404)
  return web.json_response({'model_id': manifest.model_id, 'sha256': manifest.sha256, 'failure': failure},
                           headers={'Cache-Control': 'no-store',
                                    'Content-Disposition': 'attachment; filename="egpu-diagnostics.json"'})


async def api_retry_verification(request: web.Request) -> web.Response:
  """Queue verification after reboot; do not unblock a running session."""
  if not HAS_PARAMS or Params is None:
    return web.json_response({'ok': False, 'error': 'params unavailable'}, status=503)
  try:
    body = await request.json()
    manifest = active_manifest()
    if not isinstance(body, dict) or set(body) != {'sha256'} or manifest is None or body['sha256'] != manifest.sha256:
      raise ValueError('model changed; refresh before requesting verification')
    params = Params()
    status = build_status_payload(params)
    if not status.get('rejection', {}).get('can_request'):
      raise ValueError('turn ignition off and wait for offroad; matching blocked-model diagnostics are required')
    # No await between the fresh gate and the small request write. The actual
    # rejection is retained even if ignition changes immediately afterwards.
    request_retry(model_cache_dir() / 'precompiled' / manifest.sha256)
    return web.json_response({'ok': True, 'reboot_required': True})
  except (OSError, ValueError, KeyError, TypeError) as exc:
    return web.json_response({'ok': False, 'error': str(exc)}, status=409)


async def api_compile_restart(_request: web.Request) -> web.Response:
  if not HAS_PARAMS or Params is None:
    return web.json_response({"ok": False, "error": "params unavailable"}, status=500)

  params = Params()
  payload = build_status_payload(params)
  if not payload.get("available"):
    return web.json_response({"ok": False, "error": "eGPU has never been connected"}, status=404)
  if payload.get("engaged"):
    return web.json_response({"ok": False, "error": "disengage openpilot and park before restarting"}, status=409)
  if not payload.get("can_restart"):
    return web.json_response({"ok": False, "error": "model is not ready for compilation", "state": payload.get("state")}, status=409)

  from openpilot.selfdrive.modeld.helpers import usbgpu_present

  if not usbgpu_present():
    return web.json_response({"ok": False, "error": "turn ignition on and wait for eGPU power"}, status=409)

  manifest = active_manifest()
  if manifest is None or active_model_path() is None:
    return web.json_response({"ok": False, "error": "verified model is unavailable"}, status=409)
  write_big_model_status(model_cache_dir(), "waiting_for_ignition", model_id=manifest.model_id,
                         sha256=manifest.sha256, downloaded_bytes=manifest.size, total_bytes=manifest.size,
                         detail="restart requested; compilation will begin during boot")
  params.put_bool("DoReboot", True)
  return web.json_response({"ok": True, "reboot_requested": True})


async def api_library(request: web.Request) -> web.Response:
  from openpilot.common import big_model_catalog as catalog
  from openpilot.selfdrive.modeld import model_library as library
  try:
    body = await request.json()
    if not isinstance(body, dict) or set(body) != {'action', 'model'}:
      raise ValueError('invalid model action')
    action, key = body['action'], body['model']
    if action != 'select' or not isinstance(key, str) or key not in catalog.available():
      raise ValueError('unknown model action')
    await asyncio.to_thread(catalog.select, key)
    return web.json_response({'ok': True, 'library': await asyncio.to_thread(library.status)})
  except (ValueError, OSError) as exc:
    return web.json_response({'ok': False, 'error': str(exc)}, status=409)


def maintain_library_once():
  from openpilot.selfdrive.modeld.model_library import reconcile_retired
  if HAS_PARAMS and Params is not None:
    return reconcile_retired(Params())
  return []


async def library_maintenance(app):
  async def run():
    while True:
      try:
        await asyncio.to_thread(maintain_library_once)
      except (OSError, ValueError):
        pass  # retry on the next offroad pass
      await asyncio.sleep(30)
  task = asyncio.create_task(run())
  try:
    yield
  finally:
    task.cancel()
    with suppress(asyncio.CancelledError):
      await task


async def internal_preparation_context(app: web.Application):
  preparation = app[INTERNAL_PREPARATION]
  try:
    yield
  finally:
    preparation.closing.set()
    if preparation.task is not None:
      await preparation.task


def register(app: web.Application) -> None:
  app[INTERNAL_PREPARATION] = InternalPreparation()
  app.cleanup_ctx.append(internal_preparation_context)
  app.cleanup_ctx.append(library_maintenance)
  app.router.add_get("/api/egpu/model", api_status)
  app.router.add_post("/api/egpu/model/internal", api_internal_model)
  app.router.add_post("/api/egpu/model/library", api_library)
  app.router.add_get("/api/egpu/model/diagnostics", api_diagnostics)
  app.router.add_post("/api/egpu/model/retry-verification", api_retry_verification)
  app.router.add_post("/api/egpu/model/compile-restart", api_compile_restart)
