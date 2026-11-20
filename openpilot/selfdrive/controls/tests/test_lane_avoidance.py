"""Exercise the roadside exception without an MPC, device, or CAN connection."""

import ast
import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from openpilot.selfdrive.controls.lib.lane_avoidance import LaneAvoidance, LaneAvoidanceGuard


EGO_SPEED = 12.0
STEP = 0.05
START_TIME = 100.0
X = np.linspace(0.0, 80.0, 33)


def line(y):
  return SimpleNamespace(x=X.copy(), y=np.broadcast_to(y, X.shape).copy())


def model(side=1):
  return SimpleNamespace(
    position=line(-0.5 * side),
    laneLines=[line(-5.0), line(-1.9), line(1.9), line(5.0)],
    laneLineProbs=[0.0, 0.99, 0.99, 0.0],
    laneLineStds=[1.0, 0.10, 0.10, 1.0],
    roadEdges=[line(-3.0), line(3.0)],
    roadEdgeStds=[0.10, 0.10],
    velocity=SimpleNamespace(x=np.full(X.shape, EGO_SPEED)),
  )


def point(track_id, source, distance, model_y, *, speed=0.0, lateral_speed=0.0, measured=True):
  return SimpleNamespace(track_id=track_id, source=source, d_rel=distance, y_rel=-model_y,
                         v_rel=speed - EGO_SPEED, v_lead=speed, yv_rel=lateral_speed, measured=measured,
                         radar_track_state=2)


def pair(distance, side=1, *, speed=0.0):
  return (point(1, 'frontRadar', distance, side * 2.15, speed=speed),
          point(2, 'corner235', distance + 0.25, side * 2.20, speed=speed))


def step(guard, md, points, frame, *, radar_time=None, speed=EGO_SPEED, curvature=0.0, **kwargs):
  now = START_TIME + frame * STEP
  return guard.update(now, md, points, now if radar_time is None else radar_time, speed, curvature, **kwargs)


def drive(guard, md, side=1, *, start_distance=18.0, frames=16, object_speed=0.0):
  return [step(guard, md, pair(start_distance - (EGO_SPEED - object_speed) * STEP * n, side,
                               speed=object_speed), n)
          for n in range(frames)]


@pytest.mark.parametrize('side', [-1, 1])
@pytest.mark.parametrize('object_speed', [0.0, 1.5])
def test_stationary_and_creeping_roadside_vehicles_are_mirrored(side, object_speed):
  results = drive(LaneAvoidanceGuard(), model(side), side, object_speed=object_speed)
  assert all(r.status == 'confirming' and r.strength == 0.0 for r in results[:6])
  assert results[-1].status == 'tracked'
  assert results[-1].side == side
  assert results[-1].strength == 1.0
  assert all(a.strength <= b.strength for a, b in zip(results, results[1:]))


@pytest.mark.parametrize('scenario', ['stopped_queue', 'front_only', 'corner_only', 'scc_only', 'unmeasured', 'centered'])
def test_rejects_unsupported_or_non_avoiding_paths(scenario):
  guard, md = LaneAvoidanceGuard(), model()
  if scenario == 'stopped_queue':
    md.velocity.x[4:] = 0.0
  if scenario == 'centered':
    md.position = line(0.0)
  for n in range(16):
    points = pair(18.0 - EGO_SPEED * n * STEP)
    if scenario == 'front_only':
      points = points[:1]
    elif scenario == 'corner_only':
      points = points[1:]
    elif scenario == 'scc_only':
      points = (point(1, 'scc', points[0].d_rel, 2.15), points[1])
    elif scenario == 'unmeasured':
      points[0].measured = False
    assert step(guard, md, points, n).strength == 0.0


def test_repeated_radar_sample_cannot_build_confirmation():
  guard, md = LaneAvoidanceGuard(), model()
  for n in range(8):
    sample_at = START_TIME if n < 4 else START_TIME + 4 * STEP
    assert step(guard, md, pair(18.0 - EGO_SPEED * STEP * n), n, radar_time=sample_at).strength == 0.0
  assert guard.result.status == 'confirming'


def test_near_track_loss_holds_then_returns_smoothly_after_passing():
  guard, md = LaneAvoidanceGuard(), model()
  assert drive(guard, md, start_distance=14.0)[-1].strength == 1.0
  results = [step(guard, md, (), n) for n in range(16, 62)]
  assert results[0].status == 'passing' and results[0].strength == 1.0
  release = [r.strength for r in results if r.status == 'releasing']
  assert len(release) >= 10 and release[0] < 1.0
  assert all(0.0 < a - b <= 0.08 for a, b in zip(release, release[1:]))
  assert results[-1].strength == 0.0


@pytest.mark.parametrize('stale,status', [(False, 'inactive'), (True, 'stale')])
def test_far_or_stale_track_loss_does_not_hold(stale, status):
  guard, md = LaneAvoidanceGuard(), model()
  assert drive(guard, md)[-1].strength == 1.0
  result = step(guard, md, (), 16, radar_time=START_TIME if stale else None)
  assert result.status == status
  assert result.strength == 0.0


@pytest.mark.parametrize('scenario,status', [
  ('driver', 'inhibited'), ('curve', 'speedOrCurve'), ('blindspot', 'traffic'), ('oncoming', 'traffic'),
])
def test_driver_sharp_curve_and_traffic_cancel_existing_exception(scenario, status):
  guard, md = LaneAvoidanceGuard(), model()
  assert drive(guard, md)[-1].strength == 1.0
  points = pair(18.0 - EGO_SPEED * 16 * STEP)
  if scenario == 'oncoming':
    points += (point(3, 'frontRadar', 16.0, -1.75, speed=-EGO_SPEED),)
  result = step(guard, md, points, 16, allowed=scenario != 'driver',
                left_occupied=scenario == 'blindspot', curvature=0.02 if scenario == 'curve' else 0.0)
  assert result.status == status
  assert result.strength == 0.0


@pytest.mark.parametrize('scenario', ['narrow', 'split', 'edge', 'weak_line', 'crossed', 'nonmonotonic', 'short'])
def test_rejects_unreliable_geometry_or_paths_outside_vehicle_envelope(scenario):
  guard, md = LaneAvoidanceGuard(), model()
  if scenario == 'narrow':
    md.laneLines[2] = line(1.05)
  elif scenario == 'split':
    md.laneLines[2] = line(np.interp(X, (0.0, 35.0, 80.0), (1.8, 2.6, 2.6)))
  elif scenario == 'edge':
    md.roadEdges[0] = line(-1.45)
  elif scenario == 'weak_line':
    md.laneLineProbs[1] = 0.65
  elif scenario == 'crossed':
    md.laneLines[1] = line(2.0)
  elif scenario == 'nonmonotonic':
    md.position.x[7] = md.position.x[6]
  elif scenario == 'short':
    md.position.x *= 0.15
  assert all(r.strength == 0.0 for r in drive(guard, md))


@pytest.mark.parametrize('side', [-1, 1])
def test_mix_reduces_only_toward_obstacle_inside_ramped_window(side):
  result = LaneAvoidance('tracked', side, 0.5, 10.0, 30.0)
  x = np.array([0.0, 12.5, 15.0, 20.0, 27.5, 35.0])
  lane_y = side * np.array([1.0, 1.0, 1.0, -1.0, 1.0, 1.0])
  expected = [0.8, 0.6, 0.4, 0.8, 0.6, 0.8]
  np.testing.assert_allclose(result.lane_weight(x, np.zeros_like(x), lane_y, 0.8), expected)


def test_production_lane_blend_still_runs_boundary_clamp():
  class TestFilter:
    def __init__(self, x, *_):
      self.x = x

    def update(self, x):
      self.x = x

  source = Path(__file__).parents[1] / 'lib/lane_planner_2.py'
  tree = ast.parse(source.read_text(encoding='utf8'))
  tree.body = [node for node in tree.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
  namespace = {
    'np': np, 'DT_MDL': STEP, 'FirstOrderFilter': TestFilter,
    'Params': lambda: SimpleNamespace(get_int=lambda *_: 0, get_float=lambda *_: 0.0,
                                     get=lambda *args, **kwargs: None),
  }
  exec(compile(tree, str(source), 'exec'), namespace)
  planner = namespace['LanePlanner']()
  planner.ll_t = X / EGO_SPEED
  planner.ll_x = X
  planner.lll_y, planner.rll_y = line(-1.9).y, line(1.9).y
  planner.lll_prob = planner.rll_prob = 0.99
  planner.lll_std = planner.rll_std = 0.10
  planner.lanefull_mode = True
  planner.d_prob_count = 21
  path = np.column_stack([X, line(-0.70).y, np.zeros_like(X)])
  candidate = LaneAvoidance("tracked", side=1, strength=1.0, start=10.0, end=30.0)

  baseline, old_mode = planner.get_d_path(None, EGO_SPEED, planner.ll_t, path.copy(), 100.0)
  blended, new_mode = planner.get_d_path(None, EGO_SPEED, planner.ll_t, path.copy(), 100.0, avoidance=candidate)
  assert old_mode and new_mode
  np.testing.assert_allclose(baseline[:, 1], 0.0)
  assert blended[8, 1] == pytest.approx(-0.70)
  np.testing.assert_array_equal(blended[X >= 30.0], baseline[X >= 30.0])
  clamped = planner.apply_min_distance_clamp(blended, EGO_SPEED)
  assert clamped[8, 1] == pytest.approx(-0.55)
  assert planner.clamp_max_correction == pytest.approx(0.15)


def test_uncertain_edge_requires_room_for_its_inward_uncertainty_margin():
  md = model()
  md.roadEdgeStds = [1.0, 0.1]
  assert drive(LaneAvoidanceGuard(), md)[-1].strength == 0.0
  md.roadEdges[0] = line(-4.0)
  assert drive(LaneAvoidanceGuard(), md)[-1].strength == 1.0


@pytest.mark.parametrize('side', [-1, 1])
def test_actual_lane_center_gives_way_before_slew_limiting(side):
  source = Path(__file__).parents[1] / 'lib/drive_helpers.py'
  tree = ast.parse(source.read_text(encoding='utf8'))
  nodes = [n for n in tree.body if (
    isinstance(n, ast.ClassDef) and n.name == 'LaneCenterCorrector' or
    isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and
    (n.targets[0].id.startswith('LANE_CENTER_') or n.targets[0].id == 'MIN_SPEED')
  )]
  clock = SimpleNamespace(now=0.0)
  namespace = {'np': np, 'math': math, 'time': SimpleNamespace(monotonic=lambda: clock.now)}
  exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), namespace)
  corrector = namespace['LaneCenterCorrector'](SimpleNamespace(get=lambda *args, **kwargs: None))
  md = model(side)
  opposing = SimpleNamespace(side=side, strength=1.0, approachStart=0.0)
  same_direction = SimpleNamespace(side=-side, strength=1.0, approachStart=0.0)
  far_away = SimpleNamespace(side=side, strength=1.0, approachStart=30.0)

  assert corrector.update(md, EGO_SPEED, False) == 0.0
  for n in range(1, 21):
    clock.now = n * STEP
    baseline = corrector.update(md, EGO_SPEED, False)
  assert side * baseline > 0.0
  clock.now += STEP
  assert corrector.update(md, EGO_SPEED, False, avoidance=same_direction) == baseline
  clock.now += STEP
  assert corrector.update(md, EGO_SPEED, False, avoidance=far_away) == baseline
  clock.now += STEP
  first = corrector.update(md, EGO_SPEED, False, avoidance=opposing)
  assert side * (baseline - first) == pytest.approx(0.01 * STEP)
  for _ in range(20):
    clock.now += STEP
    last = corrector.update(md, EGO_SPEED, False, avoidance=opposing)
  assert last == 0.0
