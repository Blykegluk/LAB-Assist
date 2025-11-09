from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import httpx


class SupabaseStorageClient:
    def __init__(self) -> None:
        self.base_url: Optional[str] = os.getenv("SUPABASE_URL")
        self.service_key: Optional[str] = os.getenv("SUPABASE_SERVICE_ROLE")
        self.bucket: str = os.getenv("SUPABASE_BUCKET", "generated")

    def is_configured(self) -> bool:
        return bool(self.base_url and self.service_key and self.bucket)

    def upload_bytes(self, object_name: str, data: bytes, content_type: str = "application/pdf") -> str:
        """
        Uploads bytes to Supabase Storage (upsert) and returns a public URL.
        Assumes the bucket is public. If not public, this will still upload but the URL may not be accessible.
        """
        if not self.is_configured():
            raise RuntimeError("Supabase Storage non configuré")

        # Endpoint: POST /storage/v1/object/{bucket}/{object}
        url = f"{self.base_url.rstrip('/')}/storage/v1/object/{self.bucket}/{object_name.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {self.service_key}",
            "Content-Type": content_type,
            "x-upsert": "true",
        }
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(url, headers=headers, content=data)
            if resp.status_code not in (200, 201):
                raise RuntimeError(f"Echec upload Supabase: {resp.status_code} {resp.text}")

        # Public URL (bucket public)
        public_url = f"{self.base_url.rstrip('/')}/storage/v1/object/public/{self.bucket}/{object_name.lstrip('/')}"
        return public_url

    def upload_file(self, file_path: Path, object_name: Optional[str] = None, content_type: str = "application/pdf") -> str:
        object_name = object_name or file_path.name
        data = file_path.read_bytes()
        return self.upload_bytes(object_name=object_name, data=data, content_type=content_type)


