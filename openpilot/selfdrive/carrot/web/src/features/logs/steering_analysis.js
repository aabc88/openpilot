import { createSummarySection } from "./route_summary/components.js";

const findings = {
  target: ["모델 목표와 제어 목표가 달랐습니다.", "곡률 제한, 경로 보정과 모델 목표 전달 과정을 확인하세요."],
  tracking: ["차량 추정 곡률이 제어 목표를 따라가지 못했습니다.", "조향 포화, 응답 지연, 조향각 보정과 차량 모델을 확인하세요."],
  both: ["모델 목표와 제어 목표 사이, 제어 목표와 차량 반응 사이에 모두 차이가 있었습니다.", "목표 곡률을 바꾼 제한·보정과 차량 추종 오차를 순서대로 확인하세요."],
  combined: ["각 단계의 차이는 작았지만 모델과 차량 사이의 오차가 허용 범위를 넘었습니다.", "세 곡률을 같은 시점에서 대조하세요."],
};
let pending = false;
let lastResult = "";

function append(parent, tag, text) {
  const node = document.createElement(tag);
  node.textContent = text;
  parent.append(node);
  return node;
}

function addLogLink(parent, segment, label) {
  const link = append(parent, "a", label);
  link.href = `/api/dashcam/download/${encodeURIComponent(segment)}/rlog`;
}

function openAnalysis(result) {
  openAppDialog({
    mode: "alert",
    html: true,
    title: "최근 주행 조향 분석",
    messageHtml: '<div id="latestSteeringReport" class="route-summary"></div>',
    confirmLabel: "닫기",
  });
  const root = document.getElementById("latestSteeringReport");
  if (!root) return;

  const { section } = createSummarySection("모델과 차량 반응 비교");
  root.append(section);
  append(section, "p", `${result.route} · 분석 로그 ${result.processed}/${result.total}개`);
  addLogLink(section, result.latestSegment, "최근 분석 로그 다운로드");
  if (!result.samples) {
    append(section, "p", "비교할 수 있는 자동 조향 표본이 부족합니다.");
    return;
  }

  const score = 100 * result.matched / result.samples;
  append(section, "strong", `일치율 ${score.toFixed(1)}% (${result.matched}/${result.samples}개 표본)`);
  append(section, "p", `평균 횡가속 환산 오차 ${(result.absoluteError / result.samples).toFixed(2)} m/s²`);
  append(section, "p", "자동 조향 중 시속 18 km 이상에서 운전자 조향 개입과 직진에 가까운 표본을 제외합니다. 차량 반응에는 조향각과 차량 모델로 추정한 곡률을 사용합니다.");
  append(section, "p", "곡률에 속도의 제곱을 곱해 횡가속으로 환산합니다. 오차가 0.25 m/s²와 모델 목표 크기의 20% 중 큰 값 이내면 일치로 집계합니다.");
  if (!result.incidents.length) {
    append(section, "p", "허용 오차를 3표본 이상 연속으로 넘긴 구간이 없습니다.");
    return;
  }

  const details = createSummarySection("차이가 컸던 구간");
  root.append(details.section);
  for (const { segment, startSec, endSec, peak, stage } of result.incidents) {
    const block = append(details.section, "div", "");
    append(block, "strong", `구간 ${segment.split("--").at(-1)} · 구간 시작 후 ${startSec.toFixed(2)}~${endSec.toFixed(2)}초`);
    append(block, "p", `최대 오차 시점 ${peak.timeSec.toFixed(2)}초 · ${peak.speedKph.toFixed(0)} km/h · 횡가속 환산 오차 ${peak.error.toFixed(2)} m/s²`);
    append(block, "p", `곡률(1/m) · 모델 ${peak.model.toFixed(5)} / 제어 목표 ${peak.command.toFixed(5)} / 차량 추정 ${peak.actual.toFixed(5)}`);
    append(block, "p", findings[stage].join(" "));
    addLogLink(block, segment, "이 구간의 전체 로그 다운로드");
  }
}

export async function refreshSteeringAnalysis() {
  const root = document.getElementById("dashcamSteeringAnalysis");
  if (!root || pending) return;
  pending = true;
  try {
    const response = await fetch("/api/dashcam/steering-analysis", { cache: "no-store" });
    if (!response.ok) throw new Error("steering-analysis-unavailable");
    const result = await response.json();
    const signature = JSON.stringify(result);
    if (signature === lastResult) return;
    lastResult = signature;
    root.replaceChildren();
    if (result.latestSegment) {
      const label = result.samples
        ? `최근 주행 조향 분석 · 일치율 ${(100 * result.matched / result.samples).toFixed(1)}%`
        : "최근 주행 조향 분석 · 표본 부족";
      const button = append(root, "button", label);
      button.type = "button";
      button.className = "smallBtn";
      button.addEventListener("click", () => openAnalysis(result));
    } else {
      const label = result.status === "unavailable" ? "최근 주행 로그를 읽지 못했습니다."
        : result.status === "analyzing" ? "최근 주행 조향을 분석 중입니다."
        : "주행 종료 후 조향 분석이 표시됩니다.";
      append(root, "span", label);
    }
  } catch {
    lastResult = "";
    root.textContent = "최근 주행 조향 분석을 불러오지 못했습니다.";
  } finally {
    pending = false;
  }
}
