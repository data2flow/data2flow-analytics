"""템플릿 자동 등록(ANA-01.01·01.02·01.06).

`templates/builtin/{key}/template.py` + `GUIDE.md`를 서비스 시작 때 찾아 등록한다. 설명서가 9항목을 갖추지 못하면
그 템플릿만 등록하지 않고 구조화 로그 한 줄을 남긴다(나머지 템플릿과 서비스는 정상 동작, TC-ANA-028·029).
환경변수 `DATA2FLOW_ANALYTICS_TEMPLATE_DIRS`(경로 여러 개는 `:`)로 폴더를 더 줄 수 있다(새 템플릿 추가 배포, TC-ANA-002).
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from ..common.errors import BusinessError, ErrorCode
from .base import Template
from .guide import Guide, missing_items, parse_guide

log = logging.getLogger("data2flow_analytics.templates")

BUILTIN_DIR = Path(__file__).parent / "builtin"
_KEY = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def semver_key(version: str) -> tuple[int, int, int]:
    m = _SEMVER.match(version)
    if not m:
        return (0, 0, 0)
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


@dataclass
class Registered:
    template: Template
    guide: Guide

    @property
    def key(self) -> str:
        return self.template.key

    @property
    def version(self) -> str:
        return self.template.version


class TemplateRegistry:
    def __init__(self) -> None:
        self._by_key: dict[str, dict[str, Registered]] = {}
        self.rejected: dict[str, list[str]] = {}
        self.dirs: list[Path] = []

    # ---- 등록 ----
    def register(self, template: Template, guide_markdown: str, source: str = "") -> bool:
        problems: list[str] = []
        if not _KEY.match(template.key or ""):
            problems.append("key")
        if not _SEMVER.match(template.version or ""):
            problems.append("version")
        guide = parse_guide(guide_markdown)
        params = list(template.params_model.model_fields.keys())
        problems.extend(missing_items(guide, params))
        if problems:
            self.rejected[f"{template.key}@{template.version}"] = problems
            log.error("template=%s missing=[%s] source=%s", template.key, ", ".join(problems), source)
            return False
        self._by_key.setdefault(template.key, {})[template.version] = Registered(template, guide)
        return True

    def discover(self, *dirs: Path) -> TemplateRegistry:
        for base in dirs:
            base = Path(base)
            if base not in self.dirs:
                self.dirs.append(base)
            if not base.is_dir():
                continue
            for folder in sorted(p for p in base.iterdir() if p.is_dir() and (p / "template.py").exists()):
                self._load_folder(folder)
        return self

    def _load_folder(self, folder: Path) -> None:
        guide_file = folder / "GUIDE.md"
        try:
            module = _import(folder / "template.py")
        except Exception as exc:  # noqa: BLE001 — 한 템플릿 실패가 서비스를 막지 않는다
            self.rejected[folder.name] = [f"import:{type(exc).__name__}"]
            log.exception("template=%s missing=[import] source=%s", folder.name, folder)
            return
        templates = getattr(module, "TEMPLATES", None) or [getattr(module, "TEMPLATE", None)]
        for template in templates:
            if template is None:
                continue
            if not guide_file.exists():
                self.rejected[f"{template.key}@{template.version}"] = ["guide"]
                log.error("template=%s missing=[guide] source=%s", template.key, folder)
                continue
            template.guide_path = guide_file
            self.register(template, guide_file.read_text(encoding="utf-8"), str(folder))

    # ---- 조회 ----
    def keys(self) -> list[str]:
        return sorted(self._by_key)

    def versions(self, key: str) -> list[str]:
        return sorted(self._by_key.get(key, {}), key=semver_key)

    def get(self, key: str, version: str | None = None) -> Registered:
        versions = self._by_key.get(key)
        if not versions:
            raise BusinessError(ErrorCode.TEMPLATE_NOT_FOUND)
        if version is None:
            version = self.versions(key)[-1]
        reg = versions.get(version)
        if reg is None:
            raise BusinessError(ErrorCode.TEMPLATE_NOT_FOUND)
        return reg

    def current(self) -> list[Registered]:
        return [self.get(k) for k in self.keys()]

    def all_versions(self) -> list[Registered]:
        return [reg for k in self.keys() for reg in self._by_key[k].values()]


def _import(path: Path):
    name = "data2flow_analytics_tpl_" + re.sub(r"[^A-Za-z0-9_]", "_", str(path.parent.resolve()))
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def extra_dirs_from_env(value: str | None) -> list[Path]:
    if not value:
        return []
    return [Path(p) for p in value.split(":") if p.strip()]


def build_registry(extra_dirs: list[Path] | None = None) -> TemplateRegistry:
    return TemplateRegistry().discover(BUILTIN_DIR, *(extra_dirs or []))
