import pytest

from openpilot.selfdrive.carrot.server.features.dashcam.steering_analysis import evaluate_segment


def frames_at(t, *, model=0.01, command=0.01, actual=0.01, speed=10.0, active=True, override=False):
  timestamp = int(t * 1e9)
  return [
    (timestamp, "modelV2", (model,)),
    (timestamp, "carState", (speed, override)),
    (timestamp, "carControl", (active,)),
    (timestamp, "controlsState", (command, actual)),
  ]


@pytest.mark.parametrize(("command", "actual", "stage"), [
  (0.004, 0.004, "target"),
  (0.01, 0.004, "tracking"),
  (0.007, 0.002, "both"),
  (0.008, 0.006, "combined"),
])
def test_reports_sustained_mismatch_and_its_stage(command, actual, stage):
  frames = frames_at(1.0)
  for timestamp in (1.10, 1.16, 1.22):
    frames += frames_at(timestamp, command=command, actual=actual)
  samples, matched, error, incidents = evaluate_segment(frames, "route--0")

  assert (samples, matched) == (4, 1)
  assert error == pytest.approx(3 * abs(0.01 - actual) * 100)
  assert len(incidents) == 1
  assert incidents[0]["stage"] == stage
  assert incidents[0]["startSec"] == pytest.approx(0.10)
  assert incidents[0]["endSec"] == pytest.approx(0.22)


def test_omits_ineligible_and_stale_frames():
  frames = frames_at(1.0, override=True) + frames_at(1.1, active=False) + frames_at(1.2, speed=2)
  frames += frames_at(1.3, model=0, command=0, actual=0)
  frames += [(1_550_000_000, "controlsState", (0.01, 0.01))]
  frames += frames_at(1.7, actual=0.008)

  assert evaluate_segment(frames, "route--0")[:2] == (1, 1)


def test_invalid_or_missing_model_breaks_a_mismatch():
  frames = frames_at(1.0, actual=0.005) + frames_at(1.06, actual=0.005)
  frames += [(1_070_000_000, "modelV2", None), (1_080_000_000, "controlsState", (0.01, 0.005))]
  frames += frames_at(1.12, actual=0.005) + frames_at(1.18, actual=0.005)

  samples, matched, _, incidents = evaluate_segment(frames, "route--0")
  assert (samples, matched, incidents) == (4, 0, [])


def test_reads_compressed_cereal_log_and_skips_absent_model_action(tmp_path):
  import zstandard

  from openpilot.cereal import log
  from openpilot.selfdrive.carrot.server.features.dashcam.steering_analysis import read_segment

  messages = []
  for index in range(4):
    timestamp = 1_000_000_000 + index * 60_000_000
    for service in ("carState", "carControl", "modelV2", "controlsState"):
      event = log.Event.new_message(logMonoTime=timestamp, valid=True)
      data = event.init(service)
      if service == "carState":
        data.vEgo = 10
      elif service == "carControl":
        data.latActive = True
      elif service == "modelV2":
        if index:
          data.init("action").desiredCurvature = 0.01
      else:
        data.desiredCurvature = 0.01
        data.curvature = 0.003
      messages.append(event.to_bytes())

  path = tmp_path / "rlog.zst"
  path.write_bytes(zstandard.ZstdCompressor().compress(b"".join(messages)))
  samples, matched, _, incidents = read_segment(str(path), "route--0", lambda: False)

  assert (samples, matched) == (3, 0)
  assert incidents[0]["stage"] == "tracking"
  assert incidents[0]["startSec"] == pytest.approx(0.06)
  assert read_segment(str(path), "route--0", lambda: True) is None


def test_background_analysis_waits_for_offroad_and_final_log(monkeypatch):
  import asyncio
  from contextlib import asynccontextmanager
  from types import SimpleNamespace

  from openpilot.selfdrive.carrot.server.features.dashcam import routes, steering_analysis

  segment = "00000001--0123456789--0"
  calls = []
  flags = {"onroad": True, "complete": False}
  app = {"params": SimpleNamespace(get_bool=lambda _: flags["onroad"])}

  monkeypatch.setattr(steering_analysis, "POLL_SECONDS", 0.005)
  monkeypatch.setattr(routes, "cached_dashcam_routes", lambda: [
    {"route": "00000001--0123456789", "segmentFolders": [segment]},
  ])
  monkeypatch.setattr(steering_analysis, "segment_is_complete", lambda _: flags["complete"])
  monkeypatch.setattr(steering_analysis, "segment_dir", lambda _: "folder")
  monkeypatch.setattr(steering_analysis, "source_rlog", lambda _: ("folder/rlog", "rlog"))

  def read_log(path, name, should_stop):
    assert not should_stop()
    calls.append((path, name))
    return 5, 4, 0.6, []

  monkeypatch.setattr(steering_analysis, "read_segment", read_log)

  async def check():
    async with asynccontextmanager(steering_analysis.steering_context)(app):
      await asyncio.sleep(0.025)
      assert not calls
      flags["onroad"] = False
      await asyncio.sleep(0.025)
      assert not calls
      flags["complete"] = True
      async with asyncio.timeout(2):
        while app["steering_analysis"]["status"] != "ready":
          await asyncio.sleep(0.005)
      await asyncio.sleep(0.025)
      assert calls == [("folder/rlog", segment)]
      response = await steering_analysis.api_steering_analysis(SimpleNamespace(app=app))
      assert response.status == 200
      assert app["steering_analysis"]["matched"] == 4
      assert app["steering_analysis"]["latestSegment"] == segment

  asyncio.run(check())
