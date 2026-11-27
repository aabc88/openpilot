from __future__ import annotations

import math
from typing import TYPE_CHECKING

import pyray as rl

from cluster_config import AMBER, DESIGN_WIDTH, MAX_SPEED_KPH, RED, WHITE, ClusterTheme
from cluster_display import CLUSTER_LANGUAGE_KO, display_speed, format_radar_distance, speed_unit
from cluster_models import ClusterUiState

if TYPE_CHECKING:
    from cluster_renderer import ClusterUiRenderer


_LEFT = 60.0
_RIGHT = DESIGN_WIDTH - 64.0


def draw_surround_hud(
    renderer: ClusterUiRenderer,
    state: ClusterUiState,
    theme: ClusterTheme,
    signal_lights: tuple[bool, bool] | None = None,
) -> None:
    """Draw the surround driving readouts in the renderer's HUD coordinates."""
    korean = renderer.language == CLUSTER_LANGUAGE_KO
    text = (242, 243, 245) if theme.is_dark else (31, 34, 37)
    muted = (194, 198, 205) if theme.is_dark else (55, 60, 64)
    assist = (136, 211, 222) if theme.is_dark else (33, 77, 87)
    draw = renderer._draw_text
    measure = renderer._measure_text

    clock = state.center_clock_text or ""
    draw(clock, _LEFT, 62.0, 25.0, muted)
    gear_x = _LEFT + (measure(clock, 25.0)[0] + 24.0 if clock else 0.0)
    draw((state.gear_text or "").strip().upper()[:2], gear_x, 62.0, 30.0, text)

    metric = renderer.is_metric
    kph = display_speed(state.display_speed_kph if state.display_speed_kph is not None else state.speed_kph, True)
    speed_value = round(display_speed(min(MAX_SPEED_KPH, max(0.0, kph)), metric))
    speed = str(speed_value)
    speed_font = 180.0 if len(speed) <= 2 else 141.0
    limit = round(display_speed(state.speed_limit_kph or 0, metric))
    if limit > 0 and speed_value > limit:
        over_limit_color = (255, 190, 79) if theme.is_dark else (119, 64, 0)
        speed_color = RED if speed_value >= limit + display_speed(10.0, metric) else over_limit_color
    else:
        speed_color = text
    draw(speed, _LEFT, 229.0, speed_font, speed_color)
    speed_unit_x = _LEFT + measure(speed, speed_font)[0] + 12.0
    draw(speed_unit(metric), speed_unit_x, 286.0, 24.0, muted)

    cruise_x = _LEFT
    if limit > 0:
        center = rl.Vector2(_LEFT + 34.0, 387.0)
        rl.draw_circle_v(center, 34.0, rl.Color(*RED, 255))
        rl.draw_circle_v(center, 28.0, rl.Color(*WHITE, 255))
        limit_label = str(limit)
        draw(limit_label, center.x, center.y, 33.0 if len(limit_label) <= 2 else 27.0, (34, 36, 38), anchor="center")
        cruise_x += 94.0
    if renderer._cruise_set_visible(state):
        label = "설정" if korean else "Set"
        draw(label, cruise_x, 389.0, 24.0, muted)
        value_x = cruise_x + measure(label, 24.0)[0] + 12.0
        cruise_color = assist if state.cruise_display_state == "engaged" else muted
        draw(renderer._cruise_set_speed_text(state), value_x, 385.0, 38.0, cruise_color)

    assist_active = bool(state.lfa_active) or state.cruise_display_state == "engaged"
    if assist_active or state.cruise_display_state == "paused":
        if assist_active:
            label, color = ("주행 보조" if korean else "Drive assist"), assist
        else:
            label, color = ("보조 대기" if korean else "Assist standby"), muted
        width = measure(label, 27.0)[0]
        rl.draw_circle_v(rl.Vector2(_RIGHT - width - 15.0, 69.0), 4.5, rl.Color(*color, 255))
        draw(label, _RIGHT, 69.0, 27.0, color, anchor="right")

    if state.left_blindspot or state.right_blindspot:
        if state.left_blindspot and state.right_blindspot:
            label = "양쪽 사각지대" if korean else "Blind spots: both sides"
        elif state.left_blindspot:
            label = "좌측 사각지대" if korean else "Left blind spot"
        else:
            label = "우측 사각지대" if korean else "Right blind spot"
        draw(label, _RIGHT, 113.0, 26.0, AMBER if theme.is_dark else (119, 64, 0), anchor="right")

    if not theme.is_dark:
        # Use the existing selected lead. Adjacent vehicles and raw radar points
        # are useful for the scene but must not provide the following distance.
        lead = next(
            (vehicle for vehicle in state.detected_vehicles
             if vehicle.primary and vehicle.label.upper() == "L1"
             and math.isfinite(vehicle.longitudinal_m) and vehicle.longitudinal_m > 0.0),
            None,
        )
        if lead is not None:
            label = "앞차 거리" if korean else "Lead distance"
            distance, unit = format_radar_distance(lead.longitudinal_m, metric).split(maxsplit=1)
            draw(label, _RIGHT, 337.0, 23.0, muted, anchor="right")
            draw(unit, _RIGHT, 402.0, 24.0, muted, anchor="right")
            distance_x = _RIGHT - measure(unit, 24.0)[0] - 9.0
            draw(distance, distance_x, 388.0, 56.0, text, anchor="right")

    lights = renderer._turn_signal_lights(state) if signal_lights is None else signal_lights
    renderer._draw_turn_signal("left", lights[0])
    renderer._draw_turn_signal("right", lights[1])

    # Keep existing slowdown and speed-camera cues in the lower left strip.
    rl.rl_push_matrix()
    rl.rl_translatef(_LEFT - 140.0, 256.0, 0.0)
    try:
        next_x = renderer._draw_clean_slowdown_badge(state, theme) or 140.0
        renderer._draw_clean_safety_chip(state, theme, next_x)
    finally:
        rl.rl_pop_matrix()
