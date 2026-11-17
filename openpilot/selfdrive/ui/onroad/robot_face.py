"""Robot face onroad view (ej).

ej 탭의 로봇 얼굴 전환 토글(RobotFaceUI)을 켜면 주행 화면(카메라·모델·HUD)
대신 차량 상태에 따라 표정이 바뀌는 로봇 얼굴을 표시한다. 표정은 눈(큰 캡슐형
2개)의 모양·크기·움직임만으로 구분한다 (입·홍조·몸통 없음 — 미리보기에서
확정한 디자인).

표정 우선순위 (위쪽이 이김):
  운전자 주의 > 급제동 > 후진 > 차선변경 > 깜빡이 > 클램프 보정 > 출발 >
  주의 해제 > 디스인게이지 > 신호 대기 > 인게이지 > 평소
출발/주의 해제/디스인게이지는 전이 후 3초간만 표시되는 일시 표정이다.
충전 중 수면은 offroad 상태라 실시간으로는 뜨지 않고 테스트로만 볼 수 있다.

RobotFaceTest 파라미터가 0이 아니면 실시간 상태와 무관하게 해당 표정을
표시한다 (표정 테스트). 토글이 꺼진 상태에서 테스트를 선택하면 10초간만
보여주고 파라미터를 0으로 되돌려 일반 주행 화면으로 복귀한다. 토글이 켜진
상태에서는 선택한 표정이 실시간을 고를 때까지 계속 유지된다.

안전 알림은 얼굴 위에 그대로 표시한다 (AlertRenderer 재사용). 급한 알림이
뜨면 얼굴보다 알림이 먼저 보인다.
"""

import math
import time
from dataclasses import dataclass
from enum import IntEnum

import pyray as rl

from openpilot.cereal import log
from openpilot.selfdrive.ui.carrot_param_cache import RobotFaceParamSnapshot, TimedSnapshotCache, read_robot_face_params
from openpilot.selfdrive.ui.onroad.alert_renderer import AlertRenderer
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.text_draw import draw_text_ui_style
from openpilot.system.ui.widgets import Widget

EventName = log.OnroadEvent.EventName
LaneChangeState = log.LaneChangeState
LaneChangeDirection = log.LaneChangeDirection


# 표정 번호 = RobotFaceTest 파라미터 값 (0은 실시간). carrot_settings.json의
# RobotFaceTest options 순서와 반드시 일치해야 한다.
class FaceState(IntEnum):
  IDLE = 1           # 평소
  ENGAGED = 2        # 인게이지
  DISENGAGED = 3     # 디스인게이지
  BLINKER_LEFT = 4   # 왼쪽 깜빡이
  BLINKER_RIGHT = 5  # 오른쪽 깜빡이
  LANE_CHANGE = 6    # 차선변경
  DEPARTING = 7      # 출발
  HARD_BRAKE = 8     # 급제동
  DM_ALERT = 9       # 운전자 주의
  DM_CLEAR = 10      # 주의 해제
  CLAMP = 11         # 클램프 보정 작동
  WAITING = 12       # 신호 대기
  REVERSE = 13       # 후진
  SLEEP = 14         # 충전 중 수면


FACE_NAMES = {
  FaceState.IDLE: "평소",
  FaceState.ENGAGED: "인게이지",
  FaceState.DISENGAGED: "디스인게이지",
  FaceState.BLINKER_LEFT: "왼쪽 깜빡이",
  FaceState.BLINKER_RIGHT: "오른쪽 깜빡이",
  FaceState.LANE_CHANGE: "차선변경",
  FaceState.DEPARTING: "출발",
  FaceState.HARD_BRAKE: "급제동",
  FaceState.DM_ALERT: "운전자 주의",
  FaceState.DM_CLEAR: "주의 해제",
  FaceState.CLAMP: "클램프 보정 작동",
  FaceState.WAITING: "신호 대기",
  FaceState.REVERSE: "후진",
  FaceState.SLEEP: "충전 중 수면",
}

# 운전자 주의 표정을 띄우는 이벤트 (selfdrived가 발행하는 onroadEvents 이름)
DM_EVENT_NAMES = frozenset({
  EventName.driverDistracted1, EventName.driverDistracted2, EventName.driverDistracted3,
  EventName.driverUnresponsive1, EventName.driverUnresponsive2, EventName.driverUnresponsive3,
  EventName.tooDistracted,
})

HARD_BRAKE_AEGO = -3.0        # m/s², 이보다 강한 감속이면 급제동 표정
TRANSIENT_SEC = 3.0           # 출발/주의 해제/디스인게이지 표시 시간
TEST_PREVIEW_SEC = 10.0       # 토글 OFF 상태에서 표정 테스트 표시 시간
BLINK_PERIOD_SEC = 2.2        # 눈 깜빡임 주기
BLINK_DURATION_SEC = 0.12     # 눈이 감겼다 뜨는 시간

EYE_COLOR = rl.Color(125, 211, 252, 255)   # 하늘색 (미리보기 참고 사진과 동일 계열)
EYE_GLOW_COLOR = rl.Color(125, 211, 252, 36)

BORDER_COLORS = {
  FaceState.IDLE: rl.Color(0x3B, 0x82, 0xF6, 0xFF),
  FaceState.ENGAGED: rl.Color(0x16, 0x7F, 0x40, 0xFF),
  FaceState.DISENGAGED: rl.Color(0x89, 0x92, 0x8D, 0xFF),
  FaceState.BLINKER_LEFT: rl.ORANGE,
  FaceState.BLINKER_RIGHT: rl.ORANGE,
  FaceState.LANE_CHANGE: rl.ORANGE,
  FaceState.DEPARTING: rl.Color(0x16, 0x7F, 0x40, 0xFF),
  FaceState.HARD_BRAKE: rl.Color(0xC9, 0x22, 0x31, 0xFF),
  FaceState.DM_ALERT: rl.Color(0xC9, 0x22, 0x31, 0xFF),
  FaceState.DM_CLEAR: rl.Color(0x16, 0x7F, 0x40, 0xFF),
  FaceState.CLAMP: rl.Color(0x22, 0xD3, 0xEE, 0xFF),
  FaceState.WAITING: rl.Color(0xF5, 0xC5, 0x42, 0xFF),
  FaceState.REVERSE: rl.Color(0xF5, 0xC5, 0x42, 0xFF),
  FaceState.SLEEP: rl.Color(0x3B, 0x5B, 0xDB, 0xFF),
}


@dataclass
class _Signals:
  v_ego: float = 0.0
  a_ego: float = 0.0
  standstill: bool = False
  left_blinker: bool = False
  right_blinker: bool = False
  reverse: bool = False
  engaged: bool = False
  dm_alert: bool = False
  clamp_active: bool = False
  lane_changing: bool = False
  lane_change_dir: int = 0  # -1 좌, +1 우, 0 모름


class RobotFaceView(Widget):
  def __init__(self):
    super().__init__()
    self._params = TimedSnapshotCache(
      RobotFaceParamSnapshot(),
      lambda: read_robot_face_params(ui_state.params),
    )
    self._alert_renderer = self._child(AlertRenderer())
    self._face_state = FaceState.IDLE
    self._lane_change_dir = 0

    # 전이 추적. 첫 프레임에는 None으로 두고 스퓨리어스 전이를 막는다.
    self._engaged_prev: bool | None = None
    self._dm_prev: bool | None = None
    self._standstill_prev: bool | None = None
    self._depart_time = -1e9
    self._dm_clear_time = -1e9
    self._disengage_time = -1e9

    # 테스트 자동 복귀 추적
    self._test_prev = 0
    self._test_since: float | None = None

  @property
  def is_face_mode(self) -> bool:
    """얼굴 화면을 보여줄지 여부. 토글이 켜져 있거나 표정 테스트 중이면 True."""
    snap = self._params.refresh(time.monotonic())
    return snap.enabled or snap.test != 0

  def _update_state(self):
    now = time.monotonic()
    snap = self._params.refresh(now)
    self._handle_test_timeout(snap, now)

    sig = self._read_signals()
    self._update_transitions(sig, now)
    self._lane_change_dir = sig.lane_change_dir

    if snap.test != 0:
      try:
        self._face_state = FaceState(snap.test)
      except ValueError:
        self._face_state = FaceState.IDLE
      return
    self._face_state = self._pick_state(sig, now)

  def _read_signals(self) -> _Signals:
    sig = _Signals()
    sm = ui_state.sm
    try:
      cs = sm["carState"]
      sig.v_ego = float(cs.vEgo)
      sig.standstill = bool(cs.standstill)
      sig.left_blinker = bool(cs.leftBlinker)
      sig.right_blinker = bool(cs.rightBlinker)
      # hud_renderer와 동일한 방식: cereal enum을 문자열로 바꿔 비교
      sig.reverse = "REVERSE" in str(cs.gearShifter).split(".")[-1].upper()
      try:
        sig.a_ego = float(cs.aEgo)
      except Exception:
        pass
    except Exception:
      pass
    try:
      sig.engaged = bool(ui_state.engaged)
    except Exception:
      pass
    try:
      sig.dm_alert = any(ev.name in DM_EVENT_NAMES for ev in sm["onroadEvents"])
    except Exception:
      pass
    try:
      sig.clamp_active = bool(getattr(sm["controlsState"], "clampSteerActive", False))
    except Exception:
      pass
    try:
      meta = sm["modelV2"].meta
      sig.lane_changing = meta.laneChangeState != LaneChangeState.off
      direction = meta.laneChangeDirection
      if direction == LaneChangeDirection.left:
        sig.lane_change_dir = -1
      elif direction == LaneChangeDirection.right:
        sig.lane_change_dir = 1
      elif sig.left_blinker:
        sig.lane_change_dir = -1
      elif sig.right_blinker:
        sig.lane_change_dir = 1
    except Exception:
      pass
    return sig

  def _update_transitions(self, sig: _Signals, now: float) -> None:
    if self._engaged_prev is None:
      self._engaged_prev = sig.engaged
    elif self._engaged_prev and not sig.engaged:
      self._disengage_time = now
      self._engaged_prev = False
    else:
      self._engaged_prev = sig.engaged

    if self._dm_prev is None:
      self._dm_prev = sig.dm_alert
    elif self._dm_prev and not sig.dm_alert:
      self._dm_clear_time = now
      self._dm_prev = False
    else:
      self._dm_prev = sig.dm_alert

    if self._standstill_prev is None:
      self._standstill_prev = sig.standstill
    elif self._standstill_prev and not sig.standstill:
      self._depart_time = now
      self._standstill_prev = False
    else:
      self._standstill_prev = sig.standstill

  def _pick_state(self, sig: _Signals, now: float) -> FaceState:
    if sig.dm_alert:
      return FaceState.DM_ALERT
    if sig.a_ego < HARD_BRAKE_AEGO:
      return FaceState.HARD_BRAKE
    if sig.reverse:
      return FaceState.REVERSE
    if sig.lane_changing:
      return FaceState.LANE_CHANGE
    if sig.left_blinker:
      return FaceState.BLINKER_LEFT
    if sig.right_blinker:
      return FaceState.BLINKER_RIGHT
    if sig.clamp_active:
      return FaceState.CLAMP
    if now - self._depart_time < TRANSIENT_SEC:
      return FaceState.DEPARTING
    if now - self._dm_clear_time < TRANSIENT_SEC:
      return FaceState.DM_CLEAR
    if now - self._disengage_time < TRANSIENT_SEC:
      return FaceState.DISENGAGED
    if sig.standstill or sig.v_ego < 0.1:
      return FaceState.WAITING
    if sig.engaged:
      return FaceState.ENGAGED
    return FaceState.IDLE

  def _handle_test_timeout(self, snap: RobotFaceParamSnapshot, now: float) -> None:
    if snap.test != self._test_prev:
      self._test_prev = snap.test
      self._test_since = now if snap.test != 0 else None
    if snap.enabled or snap.test == 0:
      # 토글이 켜져 있으면 테스트가 계속 유지됨. 타이머는 돌지 않는다.
      self._test_since = None
      return
    if self._test_since is None:
      self._test_since = now
    if now - self._test_since < TEST_PREVIEW_SEC:
      return
    # 10초 경과: 파라미터를 0으로 되돌려 일반 주행 화면으로 복귀
    try:
      ui_state.params.put_int_nonblocking("RobotFaceTest", 0)
      self._params.store_pending(RobotFaceParamSnapshot(enabled=snap.enabled, test=0), now)
    except Exception:
      pass
    self._test_prev = 0
    self._test_since = None

  def _render(self, rect: rl.Rectangle):
    if not ui_state.started:
      return
    now = time.monotonic()
    state = self._face_state
    snap = self._params.value

    # 배경 (검은 화면)
    rl.draw_rectangle_rec(rect, rl.BLACK)

    # 상태 테두리
    border_color = BORDER_COLORS.get(state, BORDER_COLORS[FaceState.IDLE])
    rl.draw_rectangle_rounded_lines_ex(rect, 0.02, 8, 10.0, border_color)

    # 눈
    self._draw_eyes(rect, state, now)

    # 테스트 모드 안내 문구
    if snap.test != 0:
      label = f"표정 테스트: {FACE_NAMES.get(state, '')}"
      if not snap.enabled and self._test_since is not None:
        remaining = max(0, int(math.ceil(TEST_PREVIEW_SEC - (now - self._test_since))))
        label += f" ({remaining}초 후 자동 복귀)"
      draw_text_ui_style(label, rect.x + rect.width / 2.0, rect.y + rect.height - 30.0,
                         40.0, rl.WHITE, align="center_bottom")

    # 안전 알림은 얼굴 위에 그대로 표시
    self._alert_renderer.render(rect)

  def _eye_transform(self, state: FaceState, now: float, eye_w: float, eye_h: float):
    """(scale_x, scale_y, off_x, off_y, gaze_x)를 반환한다."""
    if state == FaceState.ENGAGED:
      return 1.0, 0.72, 0.0, -eye_h * 0.04, 0.0
    if state == FaceState.DISENGAGED:
      return 1.0, 0.60, 0.0, eye_h * 0.03, 0.0
    if state == FaceState.BLINKER_LEFT:
      return 1.0, 1.0, 0.0, 0.0, -eye_w * 0.35
    if state == FaceState.BLINKER_RIGHT:
      return 1.0, 1.0, 0.0, 0.0, eye_w * 0.35
    if state == FaceState.LANE_CHANGE:
      d = self._lane_change_dir
      if d != 0:
        return 1.0, 1.0, d * 20.0, 0.0, d * eye_w * 0.4
      return 1.0, 1.0, 25.0 * math.sin(2.0 * math.pi * now / 0.8), 0.0, 0.0
    if state == FaceState.DEPARTING:
      return 1.05, 1.05, 0.0, -30.0 * abs(math.sin(2.0 * math.pi * now / 0.8)), 0.0
    if state == FaceState.HARD_BRAKE:
      jx = 6.0 * math.sin(2.0 * math.pi * now * 13.0)
      jy = 4.0 * math.sin(2.0 * math.pi * now * 17.0 + 1.0)
      return 1.35, 1.35, jx, jy, 0.0
    if state == FaceState.DM_ALERT:
      return 1.2, 1.2, 0.0, 0.0, 25.0 * math.sin(2.0 * math.pi * now / 0.7)
    if state == FaceState.DM_CLEAR:
      return 1.0, 0.70, 0.0, -eye_h * 0.03, 0.0
    if state == FaceState.CLAMP:
      nudge = 18.0 * math.sin(2.0 * math.pi * now / 0.9)
      return 1.0, 1.0, nudge, 0.0, nudge * 0.5
    if state == FaceState.WAITING:
      breathe = 1.0 + 0.02 * math.sin(2.0 * math.pi * now / 2.0)
      return breathe, 0.55 * breathe, 0.0, 0.0, 0.0
    if state == FaceState.REVERSE:
      return 1.0, 0.85, 0.0, eye_h * 0.08, 0.0
    if state == FaceState.SLEEP:
      breathe_y = 6.0 * math.sin(2.0 * math.pi * now / 2.5)
      return 1.0, 0.06, 0.0, breathe_y, 0.0
    return 1.0, 1.0, 0.0, 0.0, 0.0  # IDLE

  def _draw_eyes(self, rect: rl.Rectangle, state: FaceState, now: float) -> None:
    cx = rect.x + rect.width / 2.0
    cy = rect.y + rect.height / 2.0

    # 눈 기본 크기: 화면 높이의 34%를 높이로 하는 세로 캡슐 (미리보기 46x74 비율)
    eye_h = rect.height * 0.34
    eye_w = eye_h * 0.62
    gap = eye_w * 0.9  # 두 눈 사이 간격
    half_span = eye_w / 2.0 + gap / 2.0

    scale_x, scale_y, off_x, off_y, gaze_x = self._eye_transform(state, now, eye_w, eye_h)

    # 깜빡임 (급제동·운전자 주의·수면은 눈을 크게 뜨거나 감은 상태라 깜빡임 없음)
    if state not in (FaceState.HARD_BRAKE, FaceState.DM_ALERT, FaceState.SLEEP):
      t = now % BLINK_PERIOD_SEC
      if t > BLINK_PERIOD_SEC - BLINK_DURATION_SEC:
        p = (t - (BLINK_PERIOD_SEC - BLINK_DURATION_SEC)) / BLINK_DURATION_SEC
        scale_y *= 1.0 - 0.96 * math.sin(math.pi * p)

    w = eye_w * scale_x
    h = max(eye_h * scale_y, 4.0)  # 완전히 사라지지 않게 최소 높이 유지
    base_y = cy - h / 2.0 + off_y

    for sign in (-1, 1):
      ex = cx + sign * half_span - w / 2.0 + off_x + gaze_x
      # 은은한 빛 번짐 (글로우)
      glow = rl.Rectangle(ex - w * 0.12, base_y - h * 0.08, w * 1.24, h * 1.16)
      rl.draw_rectangle_rounded(glow, 1.0, 16, EYE_GLOW_COLOR)
      rl.draw_rectangle_rounded(rl.Rectangle(ex, base_y, w, h), 1.0, 16, EYE_COLOR)
