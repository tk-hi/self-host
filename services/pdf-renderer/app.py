"""pdf-renderer — internal (loopback-only) PDF rendering microservice.

POST /render        structured JSON -> A4 PDF (bytes) + saved copy
GET  /files/{name}  serve a previously rendered PDF
GET  /health        liveness for the supervisor / poll scripts

Templates live in templates/ as Jinja2 HTML + CSS; adding a doc_type means
adding a template file, not touching this module.
"""

import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Literal

import json

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field, field_validator
from weasyprint import HTML
from weasyprint.text.fonts import FontConfiguration

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "templates"
FONT_DIR = BASE_DIR / "fonts"
OUTPUT_DIR = Path(os.environ.get("PDF_OUTPUT_DIR", "/workspace/outputs/pdfs"))

DOC_TEMPLATES = {
    "workflow_atlas": "workflow_atlas.html.j2",
    "memo": "memo.html.j2",
}

app = FastAPI(title="pdf-renderer", docs_url=None, redoc_url=None)
env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=select_autoescape(["html", "j2"]),
)


class Meta(BaseModel):
    title: str
    subtitle: str = ""
    client_name: str = ""
    prepared_by: str = ""
    confidential_line: str = ""
    date: str = ""
    accent_color: str = "#1a5276"


class Workflow(BaseModel):
    code: str
    name: str
    today: list[str] = Field(default_factory=list)
    transformed: list[str] = Field(default_factory=list)


class Section(BaseModel):
    title: str
    intro_paragraphs: list[str] = Field(default_factory=list)
    # memo sections carry only paragraphs; workflow_atlas sections carry these:
    workflows: list[Workflow] = Field(default_factory=list)


class RenderRequest(BaseModel):
    doc_type: Literal["workflow_atlas", "memo"]
    meta: Meta
    sections: list[Section]

    # LLM tool-call parsers sometimes double-encode nested structures as JSON
    # strings; accept that shape rather than failing the render.
    @field_validator("meta", "sections", mode="before")
    @classmethod
    def _decode_json_strings(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                raise ValueError("field is a string but not valid JSON")
        return v


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "document"


# The accent color is interpolated into CSS; keep it to a strict token so a
# crafted payload cannot break out of the style block.
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{3,8}$")


@app.get("/health")
def health():
    return {"status": "ok", "templates": sorted(DOC_TEMPLATES)}


@app.post("/render")
def render(req: RenderRequest):
    if not HEX_COLOR.match(req.meta.accent_color):
        raise HTTPException(422, "accent_color must be a hex color like #1a5276")

    template = env.get_template(DOC_TEMPLATES[req.doc_type])
    html = template.render(
        meta=req.meta,
        sections=req.sections,
        font_dir=FONT_DIR.as_uri(),
    )

    font_config = FontConfiguration()
    pdf_bytes = HTML(string=html, base_url=str(BASE_DIR)).write_pdf(
        font_config=font_config
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"{slugify(req.meta.title)}-{stamp}.pdf"
    (OUTPUT_DIR / filename).write_bytes(pdf_bytes)

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Saved-Filename": filename,
        },
    )


@app.get("/files/{name}")
def get_file(name: str):
    # No path separators: names are exactly what /render produced.
    if "/" in name or "\\" in name or ".." in name or not name.endswith(".pdf"):
        raise HTTPException(400, "invalid filename")
    path = OUTPUT_DIR / name
    if not path.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(path, media_type="application/pdf", filename=name)
