"""API(8080). 분석 API는 M6에서 채운다. 응답 형식은 다른 서비스와 같은 {header, response}(design/api-rules.md)."""

from fastapi import FastAPI

from . import __version__

app = FastAPI(title="data2flow-analytics", version=__version__, docs_url=None, redoc_url=None)
