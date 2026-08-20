"""Land raw source extracts in the S3 data lake.

The same boto3 code path serves three environments and the only thing that
changes is an environment variable:

    unset AWS_ENDPOINT_URL           -> real AWS S3
    AWS_ENDPOINT_URL=http://...:4566 -> LocalStack in Docker
    (pytest with moto)               -> in-process mock, used by the test suite

Objects are written under a Hive-style partition layout::

    raw/source=<system>/ingest_date=<YYYY-MM-DD>/<filename>

Partitioning by ingest date rather than by business date is deliberate. It
means a late-arriving or corrected extract lands in its own partition instead
of silently overwriting the earlier one, so the lake keeps the history of what
was received and when. Business-date partitioning belongs downstream, after
the data has been conformed.

Each run also writes a manifest recording byte counts and SHA-256 digests.
``changed_since`` compares a new manifest against the previous one, so a run can
tell which extracts actually changed -- the input to skipping unchanged work.
The pipeline currently logs the answer rather than acting on it, because a full
reload at this size costs under two seconds and skipping is not yet worth the
branch. The digests are here so that decision can be revisited with data.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from .config import LAKE, RAW, S3, S3Config, ensure_dirs

try:  # boto3 is required for the S3 path but the local path works without it
    import boto3
    from botocore.exceptions import (
        ClientError,
        EndpointConnectionError,
        NoCredentialsError,
        PartialCredentialsError,
    )
    BOTO3_AVAILABLE = True
except ImportError:  # pragma: no cover
    BOTO3_AVAILABLE = False
    ClientError = EndpointConnectionError = Exception  # type: ignore
    NoCredentialsError = PartialCredentialsError = Exception  # type: ignore


# ---------------------------------------------------------------------------


@dataclass
class LandedObject:
    source: str
    key: str
    filename: str
    bytes: int
    sha256: str
    ingest_date: str
    backend: str


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _source_of(filename: str) -> str:
    """Map an extract filename to the system it came from."""
    if filename.startswith("ehr_"):
        return "ehr"
    if filename.startswith("payer_"):
        return "payer_api"
    if filename.startswith("salesforce_"):
        return "salesforce"
    return "reference"


def _key_for(source: str, filename: str, ingest_date: date, cfg: S3Config) -> str:
    return f"{cfg.raw_prefix}/source={source}/ingest_date={ingest_date.isoformat()}/{filename}"


# ---------------------------------------------------------------------------
# backends
# ---------------------------------------------------------------------------


class LocalLakeBackend:
    """Filesystem mirror of the lake.

    Kept so the project runs end to end with no Docker and no credentials.
    It writes the identical key layout, so switching backends changes where
    bytes live and nothing else.
    """

    name = "local"

    def __init__(self, root: Path = LAKE) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def ensure_bucket(self) -> None:
        return None

    def put(self, src: Path, key: str) -> None:
        dest = self.root / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

    def get_text(self, key: str) -> str:
        return (self.root / key).read_text(encoding="utf-8")

    def exists(self, key: str) -> bool:
        return (self.root / key).exists()


class S3Backend:
    """Real S3 API. Works against AWS or LocalStack unchanged."""

    name = "s3"

    def __init__(self, cfg: S3Config = S3) -> None:
        if not BOTO3_AVAILABLE:  # pragma: no cover
            raise RuntimeError("boto3 is not installed; use LocalLakeBackend")
        self.cfg = cfg
        # A fresh Session rather than `boto3.client(...)`, which goes through a
        # module-level default session that resolves credentials once per
        # process and caches them. Two consequences, and neither is only a test
        # concern: a long-running process cannot pick up rotated credentials,
        # and the first caller in a process silently decides the credentials
        # every later caller gets. That second one is how a test that pinned
        # its own environment still inherited the previous test's, which made
        # the credential handling below unverifiable. See INC-007.
        self.client = boto3.session.Session().client(
            "s3", region_name=cfg.region, endpoint_url=cfg.endpoint_url
        )

    def ensure_bucket(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.cfg.bucket)
        except ClientError:
            kwargs = {"Bucket": self.cfg.bucket}
            # us-east-1 is the one region that rejects an explicit constraint.
            if self.cfg.region != "us-east-1":
                kwargs["CreateBucketConfiguration"] = {
                    "LocationConstraint": self.cfg.region
                }
            self.client.create_bucket(**kwargs)

    def put(self, src: Path, key: str) -> None:
        self.client.upload_file(str(src), self.cfg.bucket, key)

    def get_text(self, key: str) -> str:
        obj = self.client.get_object(Bucket=self.cfg.bucket, Key=key)
        return obj["Body"].read().decode("utf-8")

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.cfg.bucket, Key=key)
            return True
        except ClientError:
            return False


def make_backend(prefer_s3: bool = True, cfg: S3Config = S3):
    """Pick a backend, falling back to the filesystem if S3 is unreachable.

    The fallback is intentional. A reviewer cloning this repo without Docker
    should still be able to run the pipeline end to end; they simply get the
    local lake and a line in the log saying so.
    """
    if prefer_s3 and BOTO3_AVAILABLE:
        try:
            backend = S3Backend(cfg)
            backend.ensure_bucket()
            return backend
        except NoCredentialsError:
            # No AWS credentials anywhere -- no environment variables, no
            # ~/.aws, no instance role. That is not a misconfiguration, it is
            # somebody who has never configured AWS and does not intend to, so
            # the local mirror is the right answer and this is the commonest
            # way a reviewer will run this project.
            #
            # It has to be caught separately because botocore signs a request
            # before it opens a socket. With nothing to sign with it raises
            # here, at signing, and never reaches the connection error the
            # clause below was written to catch -- so a clean machine got an
            # exception where a machine with any credentials at all, even
            # rejected ones, fell back correctly. See INC-007.
            print("  note: no AWS credentials configured; using the local "
                  "lake mirror.")
        except PartialCredentialsError as exc:
            # Half-configured is different from unconfigured, and the
            # difference is worth a raise. Somebody who set an access key and
            # no secret meant to use S3 and got it wrong, and falling back
            # silently would hide the mistake behind a working run.
            raise RuntimeError(
                f"AWS credentials are partially configured ({exc}). This is a "
                f"misconfiguration rather than an absence, so the local lake "
                f"fallback is not applied -- fix the credentials, or unset "
                f"them entirely to run against the local mirror."
            ) from exc
        except (EndpointConnectionError, ConnectionError, OSError) as exc:
            # Only a *connectivity* failure falls back. An auth or permission
            # error must not: silently writing to the local disk and reporting
            # success is precisely the class of failure this project exists to
            # argue against.
            print(f"  note: S3 endpoint unreachable ({type(exc).__name__}); "
                  f"using the local lake mirror.")
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch",
                        "ExpiredToken", "UnauthorizedOperation"}:
                raise
            print(f"  note: S3 unavailable ({code or 'ClientError'}); "
                  f"using the local lake mirror.")
    return LocalLakeBackend()


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


def land_extracts(
    raw_dir: Path = RAW,
    ingest_date: date | None = None,
    backend=None,
) -> dict:
    """Copy every extract in ``raw_dir`` into the lake and write a manifest."""
    ensure_dirs()
    ingest_date = ingest_date or date.today()
    backend = backend or make_backend()
    backend.ensure_bucket()

    landed: list[LandedObject] = []
    for path in sorted(raw_dir.glob("*.csv")):
        source = _source_of(path.name)
        key = _key_for(source, path.name, ingest_date, S3)
        backend.put(path, key)
        landed.append(
            LandedObject(
                source=source,
                key=key,
                filename=path.name,
                bytes=path.stat().st_size,
                sha256=_sha256(path),
                ingest_date=ingest_date.isoformat(),
                backend=backend.name,
            )
        )

    manifest = {
        "ingest_date": ingest_date.isoformat(),
        "generated_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "bucket": S3.bucket,
        "backend": backend.name,
        "endpoint_url": S3.endpoint_url,
        "object_count": len(landed),
        "total_bytes": sum(o.bytes for o in landed),
        "objects": [asdict(o) for o in landed],
    }

    manifest_key = f"{S3.raw_prefix}/_manifests/ingest_date={ingest_date.isoformat()}/manifest.json"
    manifest_path = LAKE / "_local_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    backend.put(manifest_path, manifest_key)

    return manifest


def changed_since(previous: dict | None, current: dict) -> dict[str, str]:
    """Which extracts differ from the last run, by content digest.

    Returns a filename -> reason map: ``new``, ``changed``, or absent when the
    bytes are identical. Comparing digests rather than modification times
    matters because a re-exported file with an identical payload has a new
    mtime and the same hash, and re-processing it is pure waste.
    """
    if not previous:
        return {o["filename"]: "new" for o in current["objects"]}

    before = {o["filename"]: o["sha256"] for o in previous.get("objects", [])}
    out: dict[str, str] = {}
    for obj in current["objects"]:
        name, digest = obj["filename"], obj["sha256"]
        if name not in before:
            out[name] = "new"
        elif before[name] != digest:
            out[name] = "changed"
    return out


def read_previous_manifest(path: Path = LAKE / "_local_manifest.json") -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


if __name__ == "__main__":  # pragma: no cover
    m = land_extracts()
    print(f"backend={m['backend']}  bucket={m['bucket']}  objects={m['object_count']}  "
          f"bytes={m['total_bytes']:,}")
    for o in m["objects"]:
        print(f"  s3://{m['bucket']}/{o['key']}  ({o['bytes']:,} B)")
