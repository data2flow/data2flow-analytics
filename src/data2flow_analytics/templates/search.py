"""템플릿 찾기: 질문으로 검색(ANA-01.07 KEYWORD 대체)과 실행 가능성 평가(ANA-01.04).

의미 검색(SEMANTIC)은 data2flow-ai 임베딩으로 core-api가 한다(API-ANA-03). 여기서는 AI가 꺼졌거나 응답이 없을 때 쓰는
키워드 검색만 한다: 질문과 설명서 '언제 쓰나' 문장의 글자 2-gram이 겹치는 비율로 점수를 매긴다.
"""

from __future__ import annotations

import re

from .registry import Registered

_WS = re.compile(r"[\s?!.,·\"'“”‘’()\[\]]+")


def _bigrams(text: str) -> set[str]:
    t = _WS.sub("", text.lower())
    if len(t) == 1:
        return {t}
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _highlight(sentence: str, query: str) -> tuple[int, int] | None:
    """문장에서 질문과 가장 길게 겹치는 구간(start, end)."""
    q = _WS.sub("", query.lower())
    s_low = sentence.lower()
    best: tuple[int, int] | None = None
    for size in range(len(q), 1, -1):
        for i in range(len(q) - size + 1):
            part = q[i:i + size]
            pos = s_low.find(part)
            if pos >= 0:
                return pos, pos + size
    return best


def keyword_search(registered: list[Registered], query: str, limit: int = 20) -> list[dict]:
    qgrams = _bigrams(query)
    if not qgrams:
        return []
    hits = []
    for reg in registered:
        sentences = list(reg.guide.sections.get("whenToUse", []))
        best_score, best_sentence = 0.0, None
        for sentence in sentences:
            grams = _bigrams(sentence)
            score = len(qgrams & grams) / len(qgrams)
            if score > best_score:
                best_score, best_sentence = score, sentence
        # 이름·요약이 겹치면 약하게 더한다(같은 점수일 때 순서 안정화)
        extra = len(qgrams & _bigrams(reg.template.name + " " + str(reg.guide.sections.get("summary", "")))) / len(qgrams)
        score = round(min(1.0, best_score + 0.1 * extra), 4)
        if score <= 0:
            continue
        item = {"key": reg.key, "score": score, "matchedQuestion": best_sentence, "mode": "KEYWORD"}
        if best_sentence:
            hl = _highlight(best_sentence, query)
            if hl:
                item["highlight"] = {"start": hl[0], "end": hl[1]}
        hits.append(item)
    hits.sort(key=lambda h: (-h["score"], h["key"]))
    return hits[:limit]


def evaluate_runnable(registered: list[Registered], semantics: set[str], has_numeric: bool = True) -> list[dict]:
    """조직(사용자 범위)의 측정 항목 의미 태그로 실행 가능 여부를 정한다(TC-ANA-018)."""
    out = []
    for reg in registered:
        tpl = reg.template
        custom = getattr(tpl, "runnable_with", None)
        if custom is not None:
            runnable, missing = custom(semantics, has_numeric)
        else:
            missing = []
            for role in tpl.roles:
                if not role.required:
                    continue
                if role.semantic:
                    if role.semantic not in semantics:
                        missing.append({"role": role.name, "semantic": role.semantic})
                elif not has_numeric:
                    missing.append({"role": role.name, "semantic": None})
            runnable = not missing
        item = {"key": reg.key, "runnable": runnable, "missingRoles": missing}
        if not runnable:
            item["reason"] = "데이터 없음"
        out.append(item)
    return out
