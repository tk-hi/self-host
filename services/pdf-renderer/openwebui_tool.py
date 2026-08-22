"""
title: PDF Generator
author: self-host
description: Render a structured workflow atlas or memo into a typeset A4 PDF and return a download link.
required_open_webui_version: 0.5.0
version: 0.1.0
license: MIT
"""

import json

import requests
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        PDF_RENDERER_URL: str = Field(
            default="http://127.0.0.1:8090",
            description="Base URL of the internal pdf-renderer service",
        )
        OPENWEBUI_URL: str = Field(
            default="http://127.0.0.1:8080",
            description="Base URL of this Open WebUI instance (loopback)",
        )
        OPENWEBUI_API_KEY: str = Field(
            default="",
            description="API key used to upload finished PDFs to Open WebUI's file storage so users get a clickable link",
        )

    def __init__(self):
        self.valves = self.Valves()

    def generate_pdf(self, payload_json: str) -> str:
        """
        Render a structured document into a typeset A4 PDF and return a markdown download link.

        The payload_json argument must be a JSON string with this shape:
        {
          "doc_type": "workflow_atlas" | "memo",
          "meta": {
            "title": str, "subtitle": str, "client_name": str,
            "prepared_by": str, "confidential_line": str, "date": str,
            "accent_color": "#hex"
          },
          "sections": [
            {
              "title": str,
              "intro_paragraphs": [str, ...],
              "workflows": [            // workflow_atlas only; omit for memo
                { "code": str, "name": str,
                  "today": [str, ...], "transformed": [str, ...] }
              ]
            }
          ]
        }

        :param payload_json: The document as a JSON string (see schema above).
        :return: A markdown link to download the rendered PDF, or an error message.
        """
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError as e:
            return f"Error: payload_json is not valid JSON ({e})"

        try:
            r = requests.post(
                f"{self.valves.PDF_RENDERER_URL}/render", json=payload, timeout=120
            )
        except requests.RequestException as e:
            return f"Error: pdf-renderer unreachable ({e})"
        if r.status_code != 200:
            return f"Error: pdf-renderer returned {r.status_code}: {r.text[:500]}"

        filename = r.headers.get("X-Saved-Filename", "document.pdf")

        if not self.valves.OPENWEBUI_API_KEY:
            return (
                f"PDF rendered and saved on the server as {filename}, but no "
                "OPENWEBUI_API_KEY is configured in the tool valves, so no "
                "download link could be created."
            )

        try:
            up = requests.post(
                f"{self.valves.OPENWEBUI_URL}/api/v1/files/",
                headers={"Authorization": f"Bearer {self.valves.OPENWEBUI_API_KEY}"},
                files={"file": (filename, r.content, "application/pdf")},
                timeout=60,
            )
            up.raise_for_status()
            file_id = up.json()["id"]
        except (requests.RequestException, KeyError, ValueError) as e:
            return (
                f"PDF rendered and saved as {filename}, but uploading it to "
                f"Open WebUI file storage failed ({e})."
            )

        return (
            f"The PDF has been generated. Give the user this download link "
            f"exactly as-is: [Download {filename}](/api/v1/files/{file_id}/content)"
        )
