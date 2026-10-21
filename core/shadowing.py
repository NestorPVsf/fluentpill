"""shadowing.py — score a spoken repetition against its target phrase.

Pure and Qt-free. Words are compared after lowercasing and dropping
punctuation (apostrophes stay: "don't", "wanna" are single words).
"""

import re
from difflib import SequenceMatcher

GREEN_FROM = 85
AMBER_FROM = 60
MAX_NOTES = 3                      # words listed in the feedback line


def words(text: str) -> list:
    t = (text or "").lower().replace("’", "'").replace("‘", "'")
    return re.sub(r"[^\w\s']", " ", t).replace("_", " ").split()


def score(target: str, heard: str) -> dict:
    """{'score': 0-100, 'missing': [...], 'changed': [(said, wanted)], 'extra': [...]}"""
    want, said = words(target), words(heard)
    if not want:
        return {"score": 0, "missing": [], "changed": [], "extra": []}
    sm = SequenceMatcher(None, want, said, autojunk=False)
    missing, changed, extra = [], [], []
    for op, a0, a1, b0, b1 in sm.get_opcodes():
        if op == "delete":
            missing += want[a0:a1]
        elif op == "insert":
            extra += said[b0:b1]
        elif op == "replace":
            # pair them up; leftovers on either side are missing / extra
            n = min(a1 - a0, b1 - b0)
            changed += [(said[b0 + i], want[a0 + i]) for i in range(n)]
            missing += want[a0 + n:a1]
            extra += said[b0 + n:b1]
    return {"score": round(sm.ratio() * 100), "missing": missing,
            "changed": changed, "extra": extra}


def band(points: int) -> str:
    return "green" if points >= GREEN_FROM else "amber" if points >= AMBER_FROM else "red"


def feedback(result: dict, heard: str = "") -> str:
    """One short Spanish line: '82 · faltó: would · dijiste want por wanna'."""
    pts = result["score"]
    if not words(heard):
        return "0 · no se oyó nada"
    if pts == 100:
        return "100 · perfecto"
    parts = []
    if result["missing"]:
        parts.append("faltó: " + ", ".join(result["missing"][:MAX_NOTES]))
    if result["changed"]:
        parts.append("dijiste " + ", ".join(f"{s} por {w}" for s, w in result["changed"][:MAX_NOTES]))
    if result["extra"] and not parts:
        parts.append("sobró: " + ", ".join(result["extra"][:MAX_NOTES]))
    return f"{pts} · " + " · ".join(parts) if parts else f"{pts}"
