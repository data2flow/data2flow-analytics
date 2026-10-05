"""템플릿 사용 설명서(GUIDE.md) 해석과 9항목 검사(ANA-01.06, BR-ANA-01).

설명서는 템플릿 코드와 같은 폴더에서 버전 관리한다. 제목(##)으로 항목을 나눈다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SECTIONS = {
    "한 줄 요약": "summary",
    "언제 쓰나": "whenToUse",
    "언제 쓰면 안 되나": "whenNotToUse",
    "필요한 데이터": "dataRequirements",
    "파라미터": "params",
    "결과 읽는 법": "howToRead",
    "주의점": "caveats",
    "사용 예시": "examples",
    "알고리즘": "algorithm",
    "참고 문헌": "references",
}
_LIST_SECTIONS = {"whenToUse", "whenNotToUse", "dataRequirements", "caveats", "examples", "references"}
_PARAM_LINE = re.compile(r"^`(?P<name>[A-Za-z0-9_]+)`\s*[:：]\s*(?P<desc>.+)$")


@dataclass
class Guide:
    title: str
    markdown: str
    sections: dict[str, object] = field(default_factory=dict)

    def to_json(self) -> dict:
        s = self.sections
        return {
            "summary": s.get("summary", ""),
            "whenToUse": s.get("whenToUse", []),
            "whenNotToUse": s.get("whenNotToUse", []),
            "dataRequirements": s.get("dataRequirements", []),
            "params": [{"name": n, "description": d} for n, d in (s.get("params") or {}).items()],
            "howToRead": s.get("howToRead", ""),
            "caveats": s.get("caveats", []),
            "examples": [{"text": e} for e in s.get("examples", [])],
            "algorithm": s.get("algorithm", ""),
            "references": s.get("references", []),
        }

    @property
    def questions(self) -> list[str]:
        return list(self.sections.get("whenToUse", []))[:3]

    def data_numbers(self) -> dict[str, float]:
        """'필요한 데이터'의 수치(최소 기간·최소 포인트·누락률 경고·실패). API 응답과 같아야 한다(TC-ANA-158)."""
        out: dict[str, float] = {}
        patterns = {
            "minPeriodDays": r"최소 기간\s*[:：]\s*([\d.]+)\s*일",
            "minPoints": r"최소 포인트\s*[:：]\s*([\d,]+)",
            "missingWarn": r"누락률 경고\s*[:：]\s*([\d.]+)\s*%",
            "missingFail": r"누락률 실패\s*[:：]\s*([\d.]+)\s*%",
        }
        for line in self.sections.get("dataRequirements", []):
            for key, pat in patterns.items():
                m = re.search(pat, str(line))
                if m:
                    value = float(m.group(1).replace(",", ""))
                    out[key] = value / 100 if key.startswith("missing") else value
        return out


def parse_guide(markdown: str) -> Guide:
    title = ""
    current: str | None = None
    buf: dict[str, list[str]] = {}
    for raw in markdown.splitlines():
        line = raw.rstrip()
        if line.startswith("# ") and not title:
            title = line[2:].strip()
            continue
        if line.startswith("## "):
            current = SECTIONS.get(line[3:].strip())
            if current:
                buf.setdefault(current, [])
            continue
        if current is not None:
            buf[current].append(line)
    sections: dict[str, object] = {}
    for key, lines in buf.items():
        if key in _LIST_SECTIONS:
            sections[key] = [ln.strip()[2:].strip() for ln in lines if ln.strip().startswith(("- ", "* "))]
        elif key == "params":
            params: dict[str, str] = {}
            for ln in lines:
                text = ln.strip()
                if text.startswith(("- ", "* ")):
                    m = _PARAM_LINE.match(text[2:].strip())
                    if m:
                        params[m.group("name")] = m.group("desc").strip()
            sections[key] = params
        else:
            sections[key] = "\n".join(ln for ln in lines).strip()
    return Guide(title=title, markdown=markdown, sections=sections)


def missing_items(guide: Guide, param_names: list[str]) -> list[str]:
    """빠진 필수 항목 목록. 비어 있으면 등록할 수 있다(BR-ANA-01)."""
    s = guide.sections
    missing: list[str] = []
    if not str(s.get("summary", "")).strip():
        missing.append("summary")
    if len(s.get("whenToUse", []) or []) < 3:
        missing.append("whenToUse>=3")
    if not s.get("whenNotToUse"):
        missing.append("whenNotToUse")
    if not s.get("dataRequirements"):
        missing.append("dataRequirements")
    documented = s.get("params") or {}
    if "params" not in s:
        missing.append("params")
    for name in param_names:
        if name not in documented:
            missing.append(f"params:{name}")
    if not str(s.get("howToRead", "")).strip():
        missing.append("howToRead")
    if not s.get("caveats"):
        missing.append("caveats")
    if not s.get("examples"):
        missing.append("examples")
    if not str(s.get("algorithm", "")).strip():
        missing.append("algorithm")
    if not s.get("references"):
        missing.append("references")
    return missing
