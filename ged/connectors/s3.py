"""Connecteur S3 : Amazon S3 et stockages compatibles (MinIO, Garage, OVH, Scaleway, Ceph…).

Droits : les politiques S3 (IAM, bucket policy) ne disent pas QUI dans l'entreprise peut lire
un document ; ils sont définis préfixe par préfixe (« dossier ») dans la source.

Identité : la clé de l'objet (un « déplacement » S3 est une copie + suppression : nouvel objet).
Compte : clé d'accès en lecture seule (s3:ListBucket + s3:GetObject sur le bucket/préfixe).
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import PurePosixPath

from ged.connectors.ecm import indexable
from ged.connectors.filesystem import DEFAULT_EXTENSIONS
from ged.connectors.rules import FolderRules
from ged.models import Acl, DocumentRef


class S3Connector:
    def __init__(self, name: str, bucket: str, prefix: str = "", *, endpoint_url: str | None = None,
                 region: str | None = None, access_key_id: str | None = None, secret_access_key: str | None = None,
                 path_style: bool = False, acl_rules: dict[str, list[str]] | None = None,
                 extensions: frozenset[str] = DEFAULT_EXTENSIONS, client=None):
        import boto3
        from botocore.config import Config
        from botocore.exceptions import BotoCoreError, ClientError

        self._errors = (BotoCoreError, ClientError)
        self.name = name
        self.bucket = bucket
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        self.endpoint_url = endpoint_url or None
        self.access_key_id = access_key_id
        self.extensions = frozenset(e.lower() for e in extensions)
        self._rules = FolderRules(acl_rules)
        self._s3 = client or boto3.client(
            "s3", endpoint_url=self.endpoint_url, region_name=region or None,
            aws_access_key_id=access_key_id or None, aws_secret_access_key=secret_access_key or None,
            config=Config(s3={"addressing_style": "path" if path_style else "auto"},
                          retries={"max_attempts": 5, "mode": "standard"}, connect_timeout=10, read_timeout=60),
        )
        try:  # échoue tout de suite si le bucket, la clé ou les droits sont faux
            self._s3.list_objects_v2(Bucket=bucket, Prefix=self.prefix, MaxKeys=1)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "NoSuchBucket":
                raise FileNotFoundError(f"Bucket introuvable : {bucket}") from exc
            if code in ("AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"):
                raise PermissionError(f"S3 refuse l'accès ({code}) : clé d'accès, secret ou droits s3:ListBucket") from exc
            raise

    def describe(self) -> dict:
        where = self.endpoint_url or "Amazon S3"
        return {
            "type": "Stockage S3" + ("" if self.endpoint_url else " (Amazon)"),
            "location": f"{where} · s3://{self.bucket}/{self.prefix}",
            "account": self.access_key_id or "(identité de l'environnement)",
            "rules": self._rules.describe(),
            "extensions": sorted(self.extensions),
        }

    def list_documents(self) -> Iterator[DocumentRef]:
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                rel = key[len(self.prefix):]
                if not rel or key.endswith("/"):
                    continue  # « dossier » vide créé par une console S3
                if any(part.startswith(".") for part in PurePosixPath(rel).parts[:-1]):
                    continue
                if not indexable(PurePosixPath(rel).name, self.extensions):
                    continue
                yield DocumentRef(self.name, key, rel, obj["LastModified"].timestamp(), int(obj.get("Size", 0)))

    def read_bytes(self, ref: DocumentRef) -> bytes:
        return self._s3.get_object(Bucket=self.bucket, Key=ref.doc_id)["Body"].read()

    def locate(self, doc_id: str, hint_path: str | None = None) -> DocumentRef | None:
        if not doc_id.startswith(self.prefix):
            return None
        try:
            head = self._s3.head_object(Bucket=self.bucket, Key=doc_id)
        except self._errors:
            return None
        return DocumentRef(self.name, doc_id, doc_id[len(self.prefix):], head["LastModified"].timestamp(),
                           int(head.get("ContentLength", 0)))

    def acl(self, ref: DocumentRef) -> Acl:
        return self._rules.acl(ref.path)
