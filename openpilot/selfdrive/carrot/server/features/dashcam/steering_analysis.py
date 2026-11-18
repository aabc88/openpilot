"""Read-only lateral tracking evaluation of finalized drive logs."""

import asyncio
from contextlib import suppress
from math import isfinite
from threading import Event
from time import monotonic

from aiohttp import web

from .catalog import segment_is_complete, source_rlog
from .paths import segment_dir

MAX_AGE_NS = 150_000_000
SAMPLE_INTERVAL_NS = 50_000_000
MIN_SPEED = 5.0
ABS_TOLERANCE = 0.25
REL_TOLERANCE = 0.20
SERVICES = ("carState", "carControl", "modelV2", "controlsState")
MAX_INCIDENTS = 10
POLL_SECONDS = 15


def tolerance(target):
  return max(ABS_TOLERANCE, abs(target) * REL_TOLERANCE)


def evaluate_segment(frames, segment):
  latest = {}
  samples = matched = 0
  error_sum = 0.0
  last_sample = -SAMPLE_INTERVAL_NS
  incidents = []
  incident = None

  def close_incident():
    nonlocal incident
    if incident and incident["samples"] >= 3:
      incidents.append(incident)
    incident = None

  if not frames:
    return samples, matched, error_sum, incidents
  frames.sort(key=lambda item: item[0])
  origin = next((timestamp for timestamp, service, _ in frames if service == "carState"), frames[0][0])
  for timestamp, service, payload in frames:
    latest[service] = timestamp, payload
    if service != "controlsState":
      continue
    if any(name not in latest or latest[name][1] is None or timestamp - latest[name][0] > MAX_AGE_NS for name in SERVICES):
      close_incident()
      continue
    speed, override = latest["carState"][1]
    active, = latest["carControl"][1]
    model, = latest["modelV2"][1]
    command, actual = payload
    if not active or override or not all(map(isfinite, (speed, model, command, actual))) or speed < MIN_SPEED:
      close_incident()
      continue
    model_accel, command_accel, actual_accel = (value * speed**2 for value in (model, command, actual))
    if max(map(abs, (model_accel, command_accel, actual_accel))) < ABS_TOLERANCE:
      close_incident()
      continue
    if timestamp - last_sample < SAMPLE_INTERVAL_NS:
      continue

    last_sample = timestamp
    samples += 1
    error = abs(model_accel - actual_accel)
    error_sum += error
    if error <= tolerance(model_accel):
      matched += 1
      close_incident()
      continue

    target_gap = abs(model_accel - command_accel) > tolerance(model_accel)
    tracking_gap = abs(command_accel - actual_accel) > tolerance(command_accel)
    stage = "both" if target_gap and tracking_gap else "target" if target_gap else "tracking" if tracking_gap else "combined"
    seconds = (timestamp - origin) / 1e9
    if incident and (stage != incident["stage"] or seconds - incident["endSec"] > .15):
      close_incident()
    peak = {"timeSec": seconds, "model": model, "command": command, "actual": actual, "speedKph": speed * 3.6, "error": error}
    if incident is None:
      incident = {"segment": segment, "stage": stage, "startSec": seconds, "samples": 0, "peak": peak}
    incident["endSec"] = seconds
    incident["samples"] += 1
    if error > incident["peak"]["error"]:
      incident["peak"] = peak
  close_incident()
  incidents.sort(key=lambda item: item["peak"]["error"], reverse=True)
  return samples, matched, error_sum, incidents[:MAX_INCIDENTS]


def read_segment(path, segment, should_stop):
  from .report import _events

  if should_stop():
    return None
  frames = []
  for index, event in enumerate(_events(path)):
    if index % 512 == 0 and should_stop():
      return None
    service = event.which()
    if service not in SERVICES:
      continue
    payload = None
    if event.valid:
      data = getattr(event, service)
      if service == "carState":
        payload = (data.vEgo, data.steeringPressed)
      elif service == "carControl":
        payload = (data.latActive,)
      elif service == "modelV2":
        if data._has("action"):
          payload = (data.action.desiredCurvature,)
      else:
        payload = (data.desiredCurvature, data.curvature)
    frames.append((event.logMonoTime, service, payload))
  return evaluate_segment(frames, segment)


async def steering_context(app):
  from .routes import cached_dashcam_routes

  state = {"status": "waiting"}
  app["steering_analysis"] = state
  stopping = Event()
  results = {}
  retry_after = {}
  current_route = None

  def paused():
    params = app.get("params")
    return stopping.is_set() or params is None or params.get_bool("IsOnroad")

  async def run():
    nonlocal current_route
    while not stopping.is_set():
      if not paused():
        try:
          routes = await asyncio.to_thread(cached_dashcam_routes)
          if routes:
            route = routes[0]
            if route["route"] != current_route:
              current_route = route["route"]
              results.clear()
              retry_after.clear()
              state.clear()
              state.update(status="analyzing", route=current_route)
            segments = route["segmentFolders"]
            for segment in segments:
              if paused():
                break
              if segment in results or monotonic() < retry_after.get(segment, 0):
                continue
              if not await asyncio.to_thread(segment_is_complete, segment):
                continue
              try:
                path, _ = source_rlog(segment_dir(segment))
                result = await asyncio.to_thread(read_segment, path, segment, paused)
              except Exception:
                retry_after[segment] = monotonic() + 60
                continue
              if result is not None:
                results[segment] = result
            available = [segment for segment in segments if segment in results]
            evaluated = [results[segment] for segment in available]
            if evaluated:
              samples = sum(item[0] for item in evaluated)
              incidents = sorted(
                (event for item in evaluated for event in item[3]),
                key=lambda item: item["peak"]["error"], reverse=True,
              )
              state.update(
                status="ready" if samples else "insufficient",
                latestSegment=available[-1], processed=len(available), total=len(segments),
                samples=samples, matched=sum(item[1] for item in evaluated),
                absoluteError=sum(item[2] for item in evaluated), incidents=incidents[:MAX_INCIDENTS],
              )
            elif not paused():
              state.update(status="unavailable" if retry_after else "waiting")
          else:
            current_route = None
            results.clear()
            retry_after.clear()
            state.clear()
            state.update(status="waiting")
        except OSError:
          pass
      await asyncio.sleep(POLL_SECONDS)

  task = asyncio.create_task(run())
  try:
    yield
  finally:
    stopping.set()
    task.cancel()
    with suppress(asyncio.CancelledError):
      await task


async def api_steering_analysis(request):
  return web.json_response(request.app["steering_analysis"], headers={"Cache-Control": "no-store"})
