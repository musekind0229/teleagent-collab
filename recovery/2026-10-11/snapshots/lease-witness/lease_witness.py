def classify_state(state: str) -> str:
    if state == "busy":
        return "hold"
    elif state == "available":
        return "ready"
    else:
        return "unknown"
