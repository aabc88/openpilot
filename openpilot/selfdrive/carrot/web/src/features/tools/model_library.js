"use strict";
const EN = {
  title: "External driving model", internal: "Internal model: CD210 (fixed)",
  library: "Driving model library", external_unavailable: "External host not connected",
  help: "One choice for eGPU and Jetson. Choosing saves the next-boot selection. Reboot comma and Jetson while parked with ignition off and internet available. First use downloads files; Jetson also prepares its engine.",
  change: "Change model", close: "Close", pending_apply: "Apply after restart",
  current: "This boot", next: "Next boot", saved: "Saved. Reboot comma and Jetson to apply.",
  noo: "NOO supported", no_noo: "NOO not supported", shared: "N16 supports navigation inputs. Flip Flop, Light speed and Light speed 2 do not.",
  internal_title: "Internal driving model",
  internal_current: "Running",
  internal_next: "Next ignition",
  internal_inactive: "No active internal model",
  internal_prepare: "Prepare",
  internal_select: "Select",
  internal_selected: "Selected",
  internal_ready: "Ready",
  internal_not_ready: "Needs preparation",
  internal_offroad: "Turn off the ignition and wait for offroad to change the internal model.",
  internal_cannot_prepare: "This device cannot prepare an internal model.",
  internal_preparing: "Downloading and preparing the internal model.",
  internal_saving: "Saving the next ignition model.",
  internal_saved: "Internal model saved for the next ignition.",
  internal_offline: "Unable to refresh model status. Reconnecting automatically.",
  internal_preparation_failed: "Preparation failed. Try preparing the model again.",
  internal_preparation_interrupted: "Preparation was interrupted. Turn off the ignition and try again.",
  internal_runtime_rejected: "Polaris Vega could not start. Prepare it again before selecting it.",
  internal_request_failed: "The internal model request failed. Check the model status and try again.",
};
const INTERNAL_ERRORS = {
  preparation_failed: "internal_preparation_failed",
  preparation_interrupted: "internal_preparation_interrupted",
  preparation_unavailable: "internal_cannot_prepare",
  offroad_required: "internal_offroad",
  runtime_rejected: "internal_runtime_rejected",
};
let externalState;
let internalState;
let internalFresh = true;
let internalAction = "";
let internalError = "";

function t(key) { return typeof getUIText === "function" ? getUIText(`model_library_${key}`, EN[key]) : EN[key]; }
function text(tag, value) { const e = document.createElement(tag); e.textContent = value; return e; }
function setText(el, value) { if (el.textContent !== value) el.textContent = value; }

async function action(kind, model) {
  try {
    const response = await postJson("/api/egpu/model/library", { action: kind, model });
    renderLibrary(response.library, true);
    if (kind === "select" && typeof showAppToast === "function") showAppToast(t("saved"));
    globalThis.CarrotEgpuModel?.refresh();
    return true;
  } catch (error) {
    if (typeof showAppToast === "function") showAppToast(error?.message || String(error), { tone: "error" });
    return false;
  }
}

function internalNotice() {
  if (!internalFresh) return "internal_offline";
  if (internalState.preparing || internalAction === "prepare") return "internal_preparing";
  if (internalAction === "select") return "internal_saving";
  const failure = internalState.error || internalError;
  if (failure) return Object.hasOwn(INTERNAL_ERRORS, failure) ? INTERNAL_ERRORS[failure] : "internal_request_failed";
  if (!internalState.can_change) return "internal_offroad";
  if (internalState.can_prepare === false && internalState.models.some(m => !m.ready)) return "internal_cannot_prepare";
  return "";
}

function internalOperation(model) {
  if (!model || !internalFresh || !internalState.can_change || internalState.preparing || internalAction) return "";
  if (!model.ready) return internalState.can_prepare === false ? "" : "prepare";
  return model.id === internalState.selected ? "" : "select";
}

async function changeInternal(modelId) {
  const kind = internalOperation(internalState?.models?.find(m => m.id === modelId));
  if (!kind) return;
  internalAction = kind;
  internalError = "";
  renderInternal(true);
  try {
    const response = await postJson("/api/egpu/model/internal", { action: kind, model: modelId });
    if (!response?.ok || !response.internal) throw new Error("Invalid internal model response");
    internalState = response.internal;
    internalFresh = true;
    if (kind === "select" && typeof showAppToast === "function") showAppToast(t("internal_saved"));
  } catch (error) {
    internalState = error?.payload?.internal || internalState;
    internalError = error?.payload?.error || "request_failed";
  } finally {
    internalAction = "";
    renderLibrary(externalState);
    globalThis.CarrotEgpuModel?.refresh();
  }
}

function internalName(id) {
  return internalState?.models?.find(model => model.id === id)?.name || id || "";
}

function renderInternal(supported) {
  const section = document.getElementById("internalModelLibrary");
  if (!section) return;
  section.hidden = !supported;
  if (!supported) return;
  setText(document.getElementById("internalModelLibraryTitle"), t("internal_title"));
  setText(document.getElementById("internalModelSummary"),
    `${t("internal_current")} · ${internalName(internalState.effective) || t("internal_inactive")} · ${t("internal_next")} · ${internalName(internalState.selected)}`);
  const choices = document.getElementById("internalModelChoices");
  const ids = JSON.stringify(internalState.models.map(m => m.id));
  if (choices.dataset.models !== ids) {
    choices.dataset.models = ids;
    choices.replaceChildren(...internalState.models.map(model => {
      const row = text("li", "");
      const label = text("div", "");
      label.append(text("strong", ""), text("small", ""));
      const button = text("button", "");
      button.className = "btn";
      button.type = "button";
      button.addEventListener("click", () => changeInternal(model.id));
      row.append(label, button);
      return row;
    }));
  }
  internalState.models.forEach((model, i) => {
    const row = choices.children[i];
    const button = row.querySelector("button");
    const label = t(!model.ready ? "internal_prepare"
      : model.id === internalState.selected ? "internal_selected" : "internal_select");
    setText(row.querySelector("strong"), model.name);
    setText(row.querySelector("small"), t(model.ready ? "internal_ready" : "internal_not_ready"));
    setText(button, label);
    button.setAttribute("aria-label", `${label} · ${model.name}`);
    button.disabled = !internalOperation(model);
  });
  const status = document.getElementById("internalModelStatus");
  const notice = internalNotice();
  status.hidden = !notice;
  setText(status, notice ? t(notice) : "");
}

export function renderLibrary(library, force = false, internal = internalState, fresh = internalFresh) {
  externalState = library;
  internalState = internal;
  internalFresh = fresh;
  const supported = Boolean(internal?.models?.length);
  if (!library?.models && !supported) return;
  const root = document.getElementById("externalModelLibrary");
  if (!root) return;
  root.hidden = false;
  const dialog = document.getElementById("modelLibraryDialog");
  const editor = document.getElementById("modelLibraryEditor");
  renderInternal(supported);
  if (!root.querySelector("button")) {
    const summary = text("div", "");
    summary.className = "model-library__summary";
    summary.append(text("strong", ""), text("p", ""));
    const button = text("button", "");
    button.id = "btnChangeModel";
    button.type = "button";
    button.className = "btn";
    button.setAttribute("aria-haspopup", "dialog");
    button.setAttribute("aria-controls", "modelLibraryDialog");
    button.addEventListener("click", () => dialog.showModal());
    root.append(summary, button);
    document.getElementById("btnCloseModelLibrary").addEventListener("click", () => dialog.close());
  }
  const effective = library?.models?.find(m => m.id === library.effective);
  const selected = library?.models?.find(m => m.id === library.selected);
  const effectiveName = effective?.name || library?.effective_name || library?.effective;
  setText(root.querySelector("strong"), supported ? t("library") : selected?.name || library.selected);
  const card = document.getElementById("egpuModelCard");
  const state = card && !card.hidden ? document.getElementById("egpuModelState").textContent
    : t(supported ? "external_unavailable" : "internal");
  const amount = card && !card.hidden ? document.getElementById("egpuModelAmount").textContent : "";
  const externalSummary = library?.reboot_required
    ? `${t("pending_apply")} · ${t("current")} ${effectiveName} · ${state}`
    : [state, amount].filter(Boolean).join(" · ");
  const externalChoice = library?.models && [selected?.name || library.selected, externalSummary].filter(Boolean).join(" · ");
  setText(root.querySelector("p"), supported
    ? [`${t("internal_next")} · ${internalName(internal.selected)}`, externalChoice].filter(Boolean).join(" / ")
    : externalSummary);
  root.dataset.state = card?.dataset.state || "";
  setText(root.querySelector("button"), t("change"));
  setText(document.getElementById("modelLibraryTitle"), t(supported ? "library" : "title"));
  setText(document.getElementById("btnCloseModelLibrary"), t("close"));
  editor.hidden = !library?.models;
  if (!library?.models) return;

  // Keep the submitted external choice stable until its response arrives.
  if (!force && editor.querySelector("select")?.disabled) return;
  const signature = JSON.stringify([library.models, library.selected, library.effective,
    library.effective_name, library.effective_noo, library.reboot_required, supported, t("title")]);
  if (!force && editor.dataset.signature === signature) return;
  editor.dataset.signature = signature;
  editor.replaceChildren(text(supported ? "h3" : "p", t(supported ? "title" : "internal")), text("p", t("help")));
  editor.append(text("p", `${t("current")} ${effectiveName} · ${t((effective?.noo ?? library.effective_noo) ? "noo" : "no_noo")}`));
  const label = text("label", t("next"));
  const select = document.createElement("select");
  select.setAttribute("aria-label", t("next"));
  for (const model of library.models) {
    const option = text("option", `${model.name} · ${t(model.noo ? "noo" : "no_noo")}`);
    option.value = model.id;
    select.append(option);
  }
  select.value = library.selected;
  label.append(select);
  select.addEventListener("change", async () => {
    select.disabled = true;
    if (!await action("select", select.value)) renderLibrary(library, true);
    select.disabled = false;
  });
  editor.append(label, text("p", t("shared")));
  if (library.reboot_required) editor.append(text("p", t("saved")));
}
