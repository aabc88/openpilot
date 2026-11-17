"""Robot face view (ej).

ej 탭의 로봇 얼굴 전환 토글(RobotFaceUI)을 켜면 차량 상태에 따라 표정이
바뀌는 로봇 얼굴을 표시한다. 주행 중에는 주행 화면(카메라·모델·HUD) 대신,
offroad(책상)에서는 홈 화면 대신 얼굴이 나온다 — 시스템 강제 onroad 없이
UI만으로 동작한다. 표정은 눈 2개의 모양·색·움직임만으로 구분한다
(입·홍조·몸통·테두리 없음 — 미리보기에서 확정한 디자인). offroad에서는
차량 신호가 없어 실시간 표정은 평소로 고정되고, 표정 테스트로는 모든
표정을 볼 수 있다.

눈은 "반짝이 눈" 스타일이다: 따뜻한 그라데이션(밝은 중심→은은한 가장자리),
윗꺼풀 그림자 띠, 주변 빛무리(글로우), 흰색 하이라이트 2개, 간헐적으로
뜨는 네 갈래 반짝이. 인게이지하면 잠깐 별눈이 반짝인 뒤 민트색 집중 눈이
된다. 커브에서는 도는 방향의 반대로 얼굴이 쏠리고 현재 횡가속도를 보여준다.
인게이지 상태에서는 하단에 최소 HUD(핸들·현재 속도·목표 속도·차간거리)가
나온다.

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

화면 크기 대응: BIG_UI(2160x1080)와 mici(콤마4, 536x240) 두 화면에서
공통으로 쓴다. 모든 절대 픽셀값(폰트·선 두께·HUD 아이콘·표정 오프셋)은
REFERENCE_HEIGHT(1080) 기준값이고, _render에서 rect 높이에 비례한
스케일을 곱한다. BIG_UI에서는 스케일 1.0으로 기존과 동일하게 그려지고,
mici에서는 약 0.22배로 축소된다. 알림 렌더러는 화면별로 다르므로
생성자에서 주입받는다 (기본값은 BIG_UI AlertRenderer, mici는
mici/onroad/alert_renderer.AlertRenderer를 넘긴다).
"""

import math
import time
from dataclasses import dataclass
from enum import IntEnum

import pyray as rl

from openpilot.cereal import log
from openpilot.selfdrive.ui.carrot_param_cache import (RobotFaceParamSnapshot, TimedSnapshotCache,
                                                      read_robot_face_params, write_robot_face_test)
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
STAR_FLASH_SEC = 0.8          # 인게이지 시 별눈 표시 시간
BREATHE_PERIOD_SEC = 2.5      # 호흡(미세한 커짐/작아짐) 주기
SPARKLE_COUNT = 3             # 반짝이 개수
SPARKLE_PERIOD_SEC = 3.0      # 반짝이 깜빡임 사이클
LEAN_MAX_PX = 80.0            # 커브 쏠림 최대 이동량 (px)
LEAN_PER_LAT_ACCEL = 28.0     # 횡가속도 1 m/s²당 쏠림 (px)
LAT_ACCEL_SHOW_MIN = 0.5      # 횡가속도 표시 최소값 (m/s²)
HUD_GAP_REFRESH_SEC = 0.5     # 차간거리 파라미터 읽기 주기

# 절대 픽셀값의 기준 화면 높이 (BIG_UI 1080). _render에서
# scale = rect.height / REFERENCE_HEIGHT를 계산해 모든 절대값에 곱한다.
# mici(240)에서는 약 0.22배로 축소된다. 0.1~2.0으로 클램프한다.
REFERENCE_HEIGHT = 1080.0
SCALE_MIN = 0.1
SCALE_MAX = 2.0
MS_TO_KPH = 3.6
MS_TO_MPH = 2.23694
KPH_TO_MPH = 0.621371

# 눈 색상 팔레트: (중심색, 가장자리색, 글로우색). 중심이 밝고 가장자리로
# 갈수록 은은하게 짙어지는 방사형 그라데이션을 3겹 레이어로 흉내 낸다.
EYE_PALETTES = {
  FaceState.IDLE: (rl.Color(255, 248, 235, 255), rl.Color(255, 200, 150, 255), rl.Color(255, 170, 80, 36)),
  FaceState.ENGAGED: (rl.Color(220, 255, 240, 255), rl.Color(110, 230, 170, 255), rl.Color(80, 220, 150, 36)),
  FaceState.DISENGAGED: (rl.Color(235, 235, 235, 255), rl.Color(165, 165, 165, 255), rl.Color(140, 140, 140, 36)),
  FaceState.BLINKER_LEFT: (rl.Color(255, 245, 220, 255), rl.Color(255, 180, 60, 255), rl.Color(255, 160, 40, 36)),
  FaceState.BLINKER_RIGHT: (rl.Color(255, 245, 220, 255), rl.Color(255, 180, 60, 255), rl.Color(255, 160, 40, 36)),
  FaceState.LANE_CHANGE: (rl.Color(255, 245, 220, 255), rl.Color(255, 180, 60, 255), rl.Color(255, 160, 40, 36)),
  FaceState.DEPARTING: (rl.Color(220, 255, 240, 255), rl.Color(110, 230, 170, 255), rl.Color(80, 220, 150, 36)),
  FaceState.HARD_BRAKE: (rl.Color(255, 255, 255, 255), rl.Color(255, 230, 230, 255), rl.Color(255, 120, 120, 40)),
  FaceState.DM_ALERT: (rl.Color(255, 220, 220, 255), rl.Color(255, 100, 100, 255), rl.Color(255, 60, 60, 40)),
  FaceState.DM_CLEAR: (rl.Color(220, 255, 240, 255), rl.Color(110, 230, 170, 255), rl.Color(80, 220, 150, 36)),
  FaceState.CLAMP: (rl.Color(220, 250, 255, 255), rl.Color(80, 210, 235, 255), rl.Color(60, 200, 230, 36)),
  FaceState.WAITING: (rl.Color(255, 248, 220, 255), rl.Color(245, 200, 80, 255), rl.Color(240, 190, 60, 36)),
  FaceState.REVERSE: (rl.Color(255, 248, 220, 255), rl.Color(245, 200, 80, 255), rl.Color(240, 190, 60, 36)),
  FaceState.SLEEP: (rl.Color(180, 200, 255, 255), rl.Color(90, 110, 220, 255), rl.Color(70, 90, 200, 30)),
}

SHADOW_BAND_COLOR = rl.Color(120, 80, 50, 45)   # 윗꺼풀 그림자 띠
HIGHLIGHT_COLOR = rl.Color(255, 255, 255, 235)  # 큰 하이라이트
HIGHLIGHT_SMALL_COLOR = rl.Color(255, 255, 255, 130)  # 작은 하이라이트
STAR_COLOR = rl.Color(255, 248, 235, 255)       # 별눈 테두리 (크림빛 흰색)
CHEVRON_COLOR = rl.ORANGE                        # 깜빡이 << >> 색
HUD_GREEN = rl.Color(0x16, 0x7F, 0x40, 255)      # HUD 핸들·차간 막대
HUD_TEXT = rl.Color(255, 255, 255, 230)          # HUD 글자
HUD_DIM = rl.Color(255, 255, 255, 160)           # HUD 보조 글자


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
  curvature: float = 0.0    # 1/m, 양수 = 우회전 (controlsd 규약)
  lat_accel: float = 0.0    # m/s², v² × 곡률
  v_ego_display: float = 0.0  # HUD 표시용 속도 (m/s, 클러스터 우선)
  v_cruise_kph: float = 0.0   # 크루즈 목표 속도 (km/h, 0이면 미설정)


class RobotFaceView(Widget):
  def __init__(self, alert_renderer_cls=None):
    """alert_renderer_cls: 알림을 그릴 렌더러 클래스. None이면 BIG_UI
    AlertRenderer를 쓰고, mici 레이아웃에서는 mici AlertRenderer를 넘긴다
    (폰트·아이콘이 작은 화면용으로 따로 맞춰져 있다)."""
    super().__init__()
    self._params = TimedSnapshotCache(
      RobotFaceParamSnapshot(),
      lambda: read_robot_face_params(ui_state.params),
    )
    if alert_renderer_cls is None:
      alert_renderer_cls = AlertRenderer
    self._alert_renderer = self._child(alert_renderer_cls())
    self._face_state = FaceState.IDLE
    self._lane_change_dir = 0
    self._sig = _Signals()

    # 전이 추적. 첫 프레임에는 None으로 두고 스퓨리어스 전이를 막는다.
    self._engaged_prev: bool | None = None
    self._dm_prev: bool | None = None
    self._standstill_prev: bool | None = None
    self._depart_time = -1e9
    self._dm_clear_time = -1e9
    self._disengage_time = -1e9
    self._engage_time = -1e9

    # 테스트 자동 복귀 추적
    self._test_prev = 0
    self._test_since: float | None = None

    # HUD 상태
    self._gap_level = 3
    self._gap_next_refresh = 0.0
    self._v_ego_cluster_seen = False

  @property
  def is_face_mode(self) -> bool:
    """얼굴 화면을 보여줄지 여부. 토글이 켜져 있거나 표정 테스트 중이면 True."""
    snap = self._params.refresh(time.monotonic())
    return snap.enabled or snap.test != 0

  def _update_state(self):
    now = time.monotonic()
    snap = self._params.refresh(now)
    self._handle_test_timeout(snap, now)
    self._refresh_gap(now)

    sig = self._read_signals()
    self._sig = sig
    self._update_transitions(sig, now)
    self._lane_change_dir = sig.lane_change_dir

    if snap.test != 0:
      try:
        self._face_state = FaceState(snap.test)
      except ValueError:
        self._face_state = FaceState.IDLE
      return
    self._face_state = self._pick_state(sig, now)

  def _refresh_gap(self, now: float) -> None:
    # 차간거리 단계: hud_renderer와 동일하게 LongitudinalPersonality + 1.
    # 0.5초마다만 읽는다 (매 프레임 파라미터 파일 읽기 금지).
    if now < self._gap_next_refresh:
      return
    self._gap_next_refresh = now + HUD_GAP_REFRESH_SEC
    try:
      personality = int(ui_state.params.get_int("LongitudinalPersonality"))
      self._gap_level = max(1, min(4, personality + 1))
    except Exception:
      pass  # 실패하면 마지막 값 유지

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
      # HUD 표시용 속도: 클러스터 속도(계기판과 동일)를 우선 사용
      try:
        v_ego_cluster = float(cs.vEgoCluster)
        self._v_ego_cluster_seen = self._v_ego_cluster_seen or v_ego_cluster != 0.0
        sig.v_ego_display = v_ego_cluster if self._v_ego_cluster_seen else sig.v_ego
      except Exception:
        sig.v_ego_display = sig.v_ego
      # 크루즈 목표 속도 (km/h): 클러스터 우선, 없으면 controlsState 구 필드
      try:
        v_cruise_cluster = float(cs.vCruiseCluster)
        if v_cruise_cluster != 0.0:
          sig.v_cruise_kph = v_cruise_cluster
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
      controls_state = sm["controlsState"]
      sig.clamp_active = bool(getattr(controls_state, "clampSteerActive", False))
      if sig.v_cruise_kph == 0.0:
        try:
          sig.v_cruise_kph = float(controls_state.deprecated.vCruise)
        except Exception:
          pass
      # 곡률: 차량 모델 경로 곡률(실제 주행 곡률) 우선
      try:
        sig.curvature = float(controls_state.curvature)
      except Exception:
        pass
    except Exception:
      pass
    if sig.curvature == 0.0:
      try:
        sig.curvature = float(sm["modelV2"].action.desiredCurvature)
      except Exception:
        pass
    # 횡가속도 = v² × 곡률. 저속에서는 v²가 작아 자연스럽게 0에 가깝다.
    try:
      if math.isfinite(sig.v_ego) and math.isfinite(sig.curvature):
        sig.lat_accel = sig.v_ego * sig.v_ego * sig.curvature
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
    elif not self._engaged_prev and sig.engaged:
      self._engage_time = now
      self._engaged_prev = True
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
    if not ui_state.started:
      # offroad(책상)에서는 차량 신호가 없어 실시간 표정은 평소로 고정한다.
      # (낡은 carState/modelV2 값으로 후진·신호 대기 등이 뜨는 것을 막는다.)
      # 표정 테스트는 _update_state에서 먼저 처리되므로 그대로 동작한다.
      return FaceState.IDLE
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
    # 10초 경과: 파라미터를 0으로 되돌려 일반 화면으로 복귀.
    # write_robot_face_test는 미등록 키(재빌드 전)면 백킹 파일에 직접 쓴다.
    try:
      write_robot_face_test(ui_state.params, 0)
      self._params.store_pending(RobotFaceParamSnapshot(enabled=snap.enabled, test=0), now)
    except Exception:
      pass
    self._test_prev = 0
    self._test_since = None

  def _lean_offset(self, sig: _Signals, scale: float = 1.0) -> float:
    """커브 쏠림: 도는 방향의 반대로 얼굴이 미끄러지는 양 (px).
    양의 곡률 = 우회전 = 차체가 왼쪽으로 쏠림 (음의 x).
    scale을 곱해 작은 화면에서는 쏠림도 비례해 줄인다."""
    try:
      a_lat = sig.lat_accel
      if not math.isfinite(a_lat):
        return 0.0
      shift = -a_lat * LEAN_PER_LAT_ACCEL * scale
      max_px = LEAN_MAX_PX * scale
      return max(-max_px, min(max_px, shift))
    except Exception:
      return 0.0

  def _render(self, rect: rl.Rectangle):
    # started 게이트 없음: offroad(책상)에서도 얼굴을 표시한다.
    now = time.monotonic()
    state = self._face_state
    snap = self._params.value
    sig = self._sig

    # 화면 스케일: 절대 픽셀값은 1080 높이 기준이므로 rect 높이에 비례해
    # 조정한다. BIG_UI(1080)에서는 1.0, mici(240)에서는 약 0.22.
    scale = max(SCALE_MIN, min(SCALE_MAX, rect.height / REFERENCE_HEIGHT))

    # 배경: 순수 검은 화면 (테두리 없음 — 콤마 화면 자체가 프레임이다)
    rl.draw_rectangle_rec(rect, rl.BLACK)

    lean_x = self._lean_offset(sig, scale)

    # 눈 (인게이지 직후 0.8초는 별눈)
    if state == FaceState.ENGAGED and now - self._engage_time < STAR_FLASH_SEC:
      self._draw_star_eyes(rect, now, lean_x, scale)
    else:
      self._draw_eyes(rect, state, now, lean_x, scale)

    # 반짝이
    self._draw_sparkles(rect, state, now, lean_x, scale)

    # 깜빡이 << >>
    if state == FaceState.BLINKER_LEFT:
      self._draw_chevrons(rect, -1, now, lean_x, scale)
    elif state == FaceState.BLINKER_RIGHT:
      self._draw_chevrons(rect, 1, now, lean_x, scale)

    # 인게이지 HUD (핸들·현재 속도·목표 속도·차간거리)
    if state == FaceState.ENGAGED:
      try:
        self._draw_hud(rect, sig, scale)
      except Exception:
        pass

    # 횡가속도 표시 (커브 중, onroad에서만)
    try:
      if ui_state.started and abs(sig.lat_accel) >= LAT_ACCEL_SHOW_MIN:
        draw_text_ui_style(f"횡가속도 {abs(sig.lat_accel):.1f} m/s²",
                           rect.x + rect.width / 2.0, rect.y + rect.height * 0.78,
                           36.0 * scale, HUD_DIM, align="center_bottom")
    except Exception:
      pass

    # 테스트 모드 안내 문구
    if snap.test != 0:
      label = f"표정 테스트: {FACE_NAMES.get(state, '')}"
      if not snap.enabled and self._test_since is not None:
        remaining = max(0, int(math.ceil(TEST_PREVIEW_SEC - (now - self._test_since))))
        label += f" ({remaining}초 후 자동 복귀)"
      draw_text_ui_style(label, rect.x + rect.width / 2.0, rect.y + rect.height - 30.0 * scale,
                         40.0 * scale, rl.WHITE, align="center_bottom")

    # 안전 알림은 얼굴 위에 그대로 표시
    self._alert_renderer.render(rect)

  def _eye_transform(self, state: FaceState, now: float, eye_w: float, eye_h: float, scale: float = 1.0):
    """(scale_x, scale_y, off_x, off_y, gaze_x)를 반환한다.
    절대 픽셀 오프셋(20/25/30/6/4/18)은 1080 기준값이므로 scale을 곱한다."""
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
        return 1.0, 1.0, d * 20.0 * scale, 0.0, d * eye_w * 0.4
      return 1.0, 1.0, 25.0 * scale * math.sin(2.0 * math.pi * now / 0.8), 0.0, 0.0
    if state == FaceState.DEPARTING:
      return 1.05, 1.05, 0.0, -30.0 * scale * abs(math.sin(2.0 * math.pi * now / 0.8)), 0.0
    if state == FaceState.HARD_BRAKE:
      jx = 6.0 * scale * math.sin(2.0 * math.pi * now * 13.0)
      jy = 4.0 * scale * math.sin(2.0 * math.pi * now * 17.0 + 1.0)
      return 1.35, 1.35, jx, jy, 0.0
    if state == FaceState.DM_ALERT:
      return 1.2, 1.2, 0.0, 0.0, 25.0 * scale * math.sin(2.0 * math.pi * now / 0.7)
    if state == FaceState.DM_CLEAR:
      return 1.0, 0.70, 0.0, -eye_h * 0.03, 0.0
    if state == FaceState.CLAMP:
      nudge = 18.0 * scale * math.sin(2.0 * math.pi * now / 0.9)
      return 1.0, 1.0, nudge, 0.0, nudge * 0.5
    if state == FaceState.WAITING:
      breathe = 1.0 + 0.02 * math.sin(2.0 * math.pi * now / 2.0)
      return breathe, 0.55 * breathe, 0.0, 0.0, 0.0
    if state == FaceState.REVERSE:
      return 1.0, 0.85, 0.0, eye_h * 0.08, 0.0
    if state == FaceState.SLEEP:
      breathe_y = 6.0 * scale * math.sin(2.0 * math.pi * now / 2.5)
      return 1.0, 0.06, 0.0, breathe_y, 0.0
    return 1.0, 1.0, 0.0, 0.0, 0.0  # IDLE

  def _eye_geometry(self, rect: rl.Rectangle, state: FaceState, now: float, scale: float = 1.0):
    """눈 기본 크기·위치·변환을 계산한다.
    (eye_w, eye_h, half_span, scale_x, scale_y, off_x, off_y, gaze_x)를 반환."""
    eye_h = rect.height * 0.34
    eye_w = eye_h * 0.8  # 참고 디자인 비율: 세로가 가로의 약 1.25배
    gap = eye_w * 1.0  # 두 눈 사이 간격 ≈ 눈 한 개 너비
    half_span = eye_w / 2.0 + gap / 2.0

    scale_x, scale_y, off_x, off_y, gaze_x = self._eye_transform(state, now, eye_w, eye_h, scale)

    # 호흡: 급제동·운전자 주의·수면을 빼고 미세하게 커졌다 작아짐
    if state not in (FaceState.HARD_BRAKE, FaceState.DM_ALERT, FaceState.SLEEP):
      breathe = 1.0 + 0.02 * math.sin(2.0 * math.pi * now / BREATHE_PERIOD_SEC)
      scale_x *= breathe
      scale_y *= breathe
      # 시선 천천히 이동 (평소·인게이지에서만)
      if state in (FaceState.IDLE, FaceState.ENGAGED):
        gaze_x += 6.0 * scale * math.sin(2.0 * math.pi * now / 7.0)

    # 깜빡임 (급제동·운전자 주의·수면은 눈을 크게 뜨거나 감은 상태라 깜빡임 없음)
    if state not in (FaceState.HARD_BRAKE, FaceState.DM_ALERT, FaceState.SLEEP):
      t = now % BLINK_PERIOD_SEC
      if t > BLINK_PERIOD_SEC - BLINK_DURATION_SEC:
        p = (t - (BLINK_PERIOD_SEC - BLINK_DURATION_SEC)) / BLINK_DURATION_SEC
        scale_y *= 1.0 - 0.96 * math.sin(math.pi * p)

    return eye_w, eye_h, half_span, scale_x, scale_y, off_x, off_y, gaze_x

  def _draw_eyes(self, rect: rl.Rectangle, state: FaceState, now: float, lean_x: float, scale: float = 1.0) -> None:
    cx = rect.x + rect.width / 2.0 + lean_x
    cy = rect.y + rect.height / 2.0

    eye_w, eye_h, half_span, scale_x, scale_y, off_x, off_y, gaze_x = self._eye_geometry(rect, state, now, scale)

    center_color, edge_color, glow_color = EYE_PALETTES.get(state, EYE_PALETTES[FaceState.IDLE])
    # 중간색 = 중심색과 가장자리색의 평균
    mid_color = rl.Color((center_color.r + edge_color.r) // 2,
                         (center_color.g + edge_color.g) // 2,
                         (center_color.b + edge_color.b) // 2, 255)

    w = eye_w * scale_x
    h = max(eye_h * scale_y, 4.0 * scale)  # 완전히 사라지지 않게 최소 높이 유지
    base_y = cy - h / 2.0 + off_y

    for sign in (-1, 1):
      ex = cx + sign * half_span - w / 2.0 + off_x + gaze_x
      # 은은한 빛 번짐 (글로우)
      glow = rl.Rectangle(ex - w * 0.12, base_y - h * 0.08, w * 1.24, h * 1.16)
      rl.draw_rectangle_rounded(glow, 0.6, 16, glow_color)
      # 그라데이션 3겹: 가장자리 → 중간 → 중심 (밝은 중심이 은은하게 번짐)
      rl.draw_rectangle_rounded(rl.Rectangle(ex, base_y, w, h), 0.55, 16, edge_color)
      mid_w, mid_h = w * 0.82, h * 0.82
      rl.draw_rectangle_rounded(
        rl.Rectangle(ex + (w - mid_w) / 2.0, base_y + (h - mid_h) / 2.0, mid_w, mid_h),
        0.55, 16, mid_color)
      in_w, in_h = w * 0.64, h * 0.64
      rl.draw_rectangle_rounded(
        rl.Rectangle(ex + (w - in_w) / 2.0, base_y + (h - in_h) / 2.0, in_w, in_h),
        0.55, 16, center_color)
      if h < 20.0 * scale:
        continue  # 눈이 거의 감겼으면 그림자·하이라이트 생략
      # 윗꺼풀 그림자 띠 (깊이감)
      band_w, band_h = w * 0.9, h * 0.28
      rl.draw_rectangle_rounded(
        rl.Rectangle(ex + (w - band_w) / 2.0, base_y + h * 0.03, band_w, band_h),
        0.5, 12, SHADOW_BAND_COLOR)
      # 하이라이트: 큰 원 왼쪽 위 + 작은 점 오른쪽 아래 (유광 반짝임)
      rl.draw_circle(int(ex + w * 0.30), int(base_y + h * 0.28), w * 0.16, HIGHLIGHT_COLOR)
      rl.draw_circle(int(ex + w * 0.62), int(base_y + h * 0.48), w * 0.08, HIGHLIGHT_SMALL_COLOR)

  def _draw_star(self, cx: float, cy: float, radius: float, color: rl.Color, thickness: float) -> None:
    """속 빈 5각 별 테두리 (약간 기울어짐)."""
    points = []
    for i in range(10):
      r = radius if i % 2 == 0 else radius * 0.42
      angle = math.radians(-15.0 + i * 36.0 - 90.0)
      points.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    for i in range(10):
      x1, y1 = points[i]
      x2, y2 = points[(i + 1) % 10]
      rl.draw_line_ex(rl.Vector2(x1, y1), rl.Vector2(x2, y2), thickness, color)

  def _draw_star_eyes(self, rect: rl.Rectangle, now: float, lean_x: float, scale: float = 1.0) -> None:
    """인게이지 직후 별눈 플래시: 두 눈 자리에 큰 별 테두리 + 주변 반짝이."""
    cx = rect.x + rect.width / 2.0 + lean_x
    cy = rect.y + rect.height / 2.0
    eye_h = rect.height * 0.34
    eye_w = eye_h * 0.8
    gap = eye_w * 1.0
    half_span = eye_w / 2.0 + gap / 2.0
    radius = eye_h * 0.6
    for sign in (-1, 1):
      self._draw_star(cx + sign * half_span, cy, radius, STAR_COLOR, 8.0 * scale)
    # 별 주변 작은 반짝이
    for i, (dx, dy) in enumerate(((-half_span - eye_w * 0.7, -eye_h * 0.7),
                                  (half_span + eye_w * 0.7, -eye_h * 0.6),
                                  (0.0, -eye_h * 0.9))):
      tw = 0.5 + 0.5 * math.sin(2.0 * math.pi * now * 3.0 + i * 2.1)
      self._draw_sparkle(cx + dx, cy + dy, (8.0 + 6.0 * tw) * scale,
                         rl.Color(255, 255, 255, int(200 * tw)))

  def _draw_sparkle(self, cx: float, cy: float, size: float, color: rl.Color) -> None:
    """네 갈래 별(✦): 상하좌우 4개 삼각형으로 만든 반짝이."""
    w = size * 0.22
    c = rl.Vector2(cx, cy)
    # 위·아래 뾰족점 (길게)
    rl.draw_triangle(rl.Vector2(cx, cy - size), rl.Vector2(cx - w, cy), rl.Vector2(cx + w, cy), color)
    rl.draw_triangle(rl.Vector2(cx, cy + size), rl.Vector2(cx - w, cy), rl.Vector2(cx + w, cy), color)
    # 좌·우 뾰족점 (짧게)
    rl.draw_triangle(rl.Vector2(cx - size * 0.7, cy), rl.Vector2(cx, cy - w), rl.Vector2(cx, cy + w), color)
    rl.draw_triangle(rl.Vector2(cx + size * 0.7, cy), rl.Vector2(cx, cy - w), rl.Vector2(cx, cy + w), color)
    rl.draw_circle(int(cx), int(cy), max(w * 0.8, 1.0), color)

  def _draw_sparkles(self, rect: rl.Rectangle, state: FaceState, now: float, lean_x: float, scale: float = 1.0) -> None:
    """눈가에 드물게 떴다 사라지는 반짝이. 경고·수면 상태에서는 표시하지 않는다."""
    if state in (FaceState.HARD_BRAKE, FaceState.DM_ALERT, FaceState.SLEEP):
      return
    cx = rect.x + rect.width / 2.0 + lean_x
    cy = rect.y + rect.height / 2.0
    eye_h = rect.height * 0.34
    eye_w = eye_h * 0.8
    gap = eye_w * 1.0
    half_span = eye_w / 2.0 + gap / 2.0
    anchors = (
      (cx - half_span - eye_w * 0.55, cy - eye_h * 0.75),
      (cx + half_span + eye_w * 0.55, cy - eye_h * 0.65),
      (cx, cy - eye_h * 0.95),
    )
    for i, (ax, ay) in enumerate(anchors):
      phase = (now / SPARKLE_PERIOD_SEC + i / SPARKLE_COUNT) % 1.0
      tw = math.sin(math.pi * phase)  # 0→1→0, 사이클마다 한 번 떴다 사라짐
      if tw <= 0.05:
        continue
      size = (6.0 + 8.0 * tw) * scale
      self._draw_sparkle(ax, ay, size, rl.Color(255, 255, 255, int(220 * tw)))

  def _draw_chevrons(self, rect: rl.Rectangle, direction: int, now: float, lean_x: float, scale: float = 1.0) -> None:
    """깜빡이 << >>: 방향 쪽 눈 옆에 이중 꺾쇠를 표시한다. 깜빡이처럼 점멸."""
    # 깜빡이 점멸: 0.5초 켜짐 / 0.5초 꺼짐
    if int(now * 2.0) % 2 == 1:
      return
    cx = rect.x + rect.width / 2.0 + lean_x
    cy = rect.y + rect.height / 2.0
    eye_h = rect.height * 0.34
    eye_w = eye_h * 0.8
    gap = eye_w * 1.0
    half_span = eye_w / 2.0 + gap / 2.0
    # 꺾쇠 크기·위치: 방향 쪽 눈 바깥
    size = eye_h * 0.35
    base_x = cx + direction * (half_span + eye_w * 0.85)
    for k in range(2):
      # 두 개의 꺾쇠를 방향 쪽으로 나란히 배치
      ox = base_x + direction * k * size * 0.7
      tip_x = ox + direction * size * 0.45
      rl.draw_line_ex(rl.Vector2(ox, cy - size / 2.0), rl.Vector2(tip_x, cy), 10.0 * scale, CHEVRON_COLOR)
      rl.draw_line_ex(rl.Vector2(tip_x, cy), rl.Vector2(ox, cy + size / 2.0), 10.0 * scale, CHEVRON_COLOR)

  def _draw_hud(self, rect: rl.Rectangle, sig: _Signals, scale: float = 1.0) -> None:
    """인게이지 HUD: 핸들 아이콘·현재 속도·목표 속도·차간거리 (하단)."""
    try:
      is_metric = bool(ui_state.is_metric)
    except Exception:
      is_metric = True
    hud_y = rect.y + rect.height * 0.88
    cx = rect.x + rect.width / 2.0

    # 핸들 아이콘 (왼쪽): 원 + 스포크 3개, 녹색
    wheel_x = rect.x + rect.width * 0.15
    wheel_r = 35.0 * scale
    rl.draw_circle_lines(int(wheel_x), int(hud_y), wheel_r, HUD_GREEN)
    for angle_deg in (90.0, 210.0, 330.0):
      a = math.radians(angle_deg)
      rl.draw_line_ex(
        rl.Vector2(wheel_x, hud_y),
        rl.Vector2(wheel_x + wheel_r * math.cos(a), hud_y + wheel_r * math.sin(a)),
        6.0 * scale, HUD_GREEN)
    rl.draw_circle(int(wheel_x), int(hud_y), 7.0 * scale, HUD_GREEN)

    # 현재 속도 (가운데, 크게)
    speed_conv = MS_TO_KPH if is_metric else MS_TO_MPH
    speed_unit = "km/h" if is_metric else "mph"
    cur = max(0.0, sig.v_ego_display * speed_conv)
    draw_text_ui_style(f"{int(round(cur))}", cx - 60.0 * scale, hud_y + 10.0 * scale, 90.0 * scale, HUD_TEXT,
                       align="center_bottom")
    draw_text_ui_style(speed_unit, cx - 60.0 * scale, hud_y + 55.0 * scale, 30.0 * scale, HUD_DIM,
                       align="center_bottom")
    # 목표 속도 (현재 속도 오른쪽, 작게)
    if sig.v_cruise_kph > 0.0:
      target = sig.v_cruise_kph if is_metric else sig.v_cruise_kph * KPH_TO_MPH
      draw_text_ui_style("SET", cx + 130.0 * scale, hud_y - 25.0 * scale, 28.0 * scale, HUD_DIM,
                         align="center_bottom")
      draw_text_ui_style(f"{int(round(target))}", cx + 130.0 * scale, hud_y + 10.0 * scale, 55.0 * scale, HUD_TEXT,
                         align="center_bottom")

    # 차간거리 막대 (오른쪽): 4칸 중 채워진 칸 수 = 단계
    bar_x = rect.x + rect.width * 0.85
    bar_w, bar_h, bar_gap = 18.0 * scale, 26.0 * scale, 8.0 * scale
    for i in range(4):
      x = bar_x + i * (bar_w + bar_gap)
      color = HUD_GREEN if i < self._gap_level else rl.Color(255, 255, 255, 60)
      rl.draw_rectangle_rounded(
        rl.Rectangle(x, hud_y - bar_h / 2.0, bar_w, bar_h), 0.3, 8, color)
