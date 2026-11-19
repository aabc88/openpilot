"""Display-only model selection from the detected CarParams fingerprint."""

# The K8 PE OBJ uses width, length and height normalization in that order.
K8_EGO_SCALE = (1.88, 5.05, 1.48)

# Explicit families avoid matching unrelated names (for example IONIQ 6).
EGO_MODEL_FINGERPRINTS = {
    "HYUNDAI_IONIQ_5": "ioniq5",
    "HYUNDAI_IONIQ_5_PE": "ioniq5",
    "HYUNDAI_IONIQ_5_N": "ioniq5",
    "HYUNDAI_STARIA_4TH_GEN": "staria",
    "HYUNDAI_STARIA_EV": "staria",
    "HYUNDAI_TUCSON_4TH_GEN": "tucson",
    "KIA_CARNIVAL_4TH_GEN": "carnival",
    "KIA_EV9": "ev9",
    "KIA_K8_HEV_1ST_GEN": "k8",
    "KIA_SPORTAGE_5TH_GEN": "sportage",
}


def ego_model_name(car_fingerprint: str) -> str:
    return EGO_MODEL_FINGERPRINTS.get(car_fingerprint, "ev6")
