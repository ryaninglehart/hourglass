"""The S3 path, exercised against a mock.

moto stands in for S3 in-process, so this runs in CI with no Docker, no
credentials and no network -- while still going through real boto3 calls. The
same code runs unchanged against LocalStack (``make localstack-up``) and
against AWS. If the client were faked instead, these tests would prove nothing
about the code that actually ships.
"""

from __future__ import annotations

import json
from datetime import date

import boto3
import pytest
from moto import mock_aws

from hourglass import ingest
from hourglass.config import S3Config

BUCKET = "hourglass-test-lake"
REGION = "us-west-2"


@pytest.fixture
def cfg() -> S3Config:
    return S3Config(bucket=BUCKET, region=REGION, endpoint_url=None, raw_prefix="raw")


@pytest.fixture
def extracts(tmp_path):
    d = tmp_path / "raw"
    d.mkdir()
    (d / "ehr_sessions.csv").write_text("session_id,client_id\nS1,C1\n", encoding="utf-8")
    (d / "payer_authorizations.csv").write_text("auth_id\nA1\n", encoding="utf-8")
    (d / "salesforce_clients.csv").write_text("client_id\nC1\n", encoding="utf-8")
    (d / "reference_centers.csv").write_text("center_id\nCTR-SD\n", encoding="utf-8")
    return d


class TestKeyLayout:
    def test_source_is_derived_from_filename(self):
        assert ingest._source_of("ehr_sessions.csv") == "ehr"
        assert ingest._source_of("payer_authorizations.csv") == "payer_api"
        assert ingest._source_of("salesforce_clients.csv") == "salesforce"
        assert ingest._source_of("reference_centers.csv") == "reference"

    def test_key_is_partitioned_by_source_and_ingest_date(self, cfg):
        key = ingest._key_for("ehr", "ehr_sessions.csv", date(2026, 8, 18), cfg)
        assert key == "raw/source=ehr/ingest_date=2026-08-18/ehr_sessions.csv"


class TestS3Backend:
    @mock_aws
    def test_creates_bucket_and_uploads(self, cfg, extracts):
        backend = ingest.S3Backend(cfg)
        manifest = ingest.land_extracts(
            raw_dir=extracts, ingest_date=date(2026, 8, 18), backend=backend)

        client = boto3.client("s3", region_name=REGION)
        keys = {o["Key"] for o in client.list_objects_v2(Bucket=BUCKET)["Contents"]}

        assert manifest["object_count"] == 4
        assert "raw/source=ehr/ingest_date=2026-08-18/ehr_sessions.csv" in keys
        assert "raw/source=payer_api/ingest_date=2026-08-18/payer_authorizations.csv" in keys

    @mock_aws
    def test_manifest_records_a_digest_per_object(self, cfg, extracts):
        backend = ingest.S3Backend(cfg)
        manifest = ingest.land_extracts(
            raw_dir=extracts, ingest_date=date(2026, 8, 18), backend=backend)
        for obj in manifest["objects"]:
            assert len(obj["sha256"]) == 64
            assert obj["bytes"] > 0

    @mock_aws
    def test_manifest_is_written_to_the_lake(self, cfg, extracts):
        backend = ingest.S3Backend(cfg)
        ingest.land_extracts(raw_dir=extracts, ingest_date=date(2026, 8, 18),
                             backend=backend)
        client = boto3.client("s3", region_name=REGION)
        body = client.get_object(
            Bucket=BUCKET,
            Key="raw/_manifests/ingest_date=2026-08-18/manifest.json",
        )["Body"].read()
        assert json.loads(body)["object_count"] == 4

    @mock_aws
    def test_round_trip_read(self, cfg, extracts):
        backend = ingest.S3Backend(cfg)
        ingest.land_extracts(raw_dir=extracts, ingest_date=date(2026, 8, 18),
                             backend=backend)
        text = backend.get_text("raw/source=ehr/ingest_date=2026-08-18/ehr_sessions.csv")
        assert text.startswith("session_id,client_id")

    @mock_aws
    def test_different_ingest_dates_do_not_collide(self, cfg, extracts):
        """A corrected re-extract lands beside the original, not on top of it."""
        backend = ingest.S3Backend(cfg)
        ingest.land_extracts(raw_dir=extracts, ingest_date=date(2026, 8, 18),
                             backend=backend)
        ingest.land_extracts(raw_dir=extracts, ingest_date=date(2026, 8, 19),
                             backend=backend)
        client = boto3.client("s3", region_name=REGION)
        keys = {o["Key"] for o in client.list_objects_v2(Bucket=BUCKET)["Contents"]}
        assert any("ingest_date=2026-08-18/ehr_sessions.csv" in k for k in keys)
        assert any("ingest_date=2026-08-19/ehr_sessions.csv" in k for k in keys)


class TestLocalBackend:
    def test_writes_the_same_key_layout(self, tmp_path, extracts):
        backend = ingest.LocalLakeBackend(root=tmp_path / "lake")
        ingest.land_extracts(raw_dir=extracts, ingest_date=date(2026, 8, 18),
                             backend=backend)
        expected = (tmp_path / "lake" / "raw" / "source=ehr"
                    / "ingest_date=2026-08-18" / "ehr_sessions.csv")
        assert expected.exists()

    def test_read_matches_write(self, tmp_path, extracts):
        backend = ingest.LocalLakeBackend(root=tmp_path / "lake")
        ingest.land_extracts(raw_dir=extracts, ingest_date=date(2026, 8, 18),
                             backend=backend)
        assert backend.get_text(
            "raw/source=ehr/ingest_date=2026-08-18/ehr_sessions.csv"
        ).startswith("session_id")


UNREACHABLE = S3Config(bucket="x", region="us-west-2",
                       endpoint_url="http://127.0.0.1:1", raw_prefix="raw")

_CREDENTIAL_VARS = (
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "AWS_PROFILE", "AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE",
    "AWS_DEFAULT_REGION", "AWS_REGION",
)


@pytest.fixture
def no_aws_credentials(monkeypatch, tmp_path):
    """A machine that has never been told about AWS.

    The environment variables are cleared *and* HOME is redirected, because
    botocore reads `~/.aws/credentials` as well. Without both, this fixture
    asserts nothing on a developer's laptop with a configured profile.
    """
    for var in _CREDENTIAL_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


@pytest.fixture
def dummy_aws_credentials(monkeypatch):
    for var in _CREDENTIAL_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")


class TestBackendSelection:
    """Which backend you get, and why.

    Every test here pins the credential environment. The version that did not
    was the project's only defect that depended on the machine it ran on: it
    passed in CI and on the author's container, both of which had credentials
    available, and failed on a clean laptop. A test whose outcome is decided by
    ambient state is not testing the code. See INC-007.
    """

    def test_falls_back_when_the_endpoint_is_unreachable(
            self, dummy_aws_credentials):
        """Credentials present, nothing listening. The connectivity path."""
        backend = ingest.make_backend(prefer_s3=True, cfg=UNREACHABLE)
        assert isinstance(backend, ingest.LocalLakeBackend)

    def test_falls_back_when_there_are_no_credentials_at_all(
            self, no_aws_credentials):
        """The commonest way a reviewer runs this, and the one that broke.

        botocore signs a request before it opens a socket, so with nothing to
        sign with it raises `NoCredentialsError` at signing and never reaches
        the connection error the fallback was written to catch. Somebody who
        has never configured AWS is not misconfigured — they are not using it —
        so the local mirror is correct.
        """
        backend = ingest.make_backend(prefer_s3=True, cfg=UNREACHABLE)
        assert isinstance(backend, ingest.LocalLakeBackend)

    def test_partial_credentials_raise_rather_than_fall_back(
            self, monkeypatch, tmp_path):
        """Half-configured is a mistake; unconfigured is a choice.

        Somebody who set an access key and no secret meant to use S3 and got
        it wrong. Falling back silently would hide that behind a run that
        looks like it worked, which is the failure class this whole project
        argues against.
        """
        for var in _CREDENTIAL_VARS:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")   # and no secret
        with pytest.raises(RuntimeError, match="partially configured"):
            ingest.make_backend(prefer_s3=True, cfg=UNREACHABLE)

    def test_rejected_credentials_still_raise(self, dummy_aws_credentials):
        """Guards the fallbacks above from becoming a blanket except.

        A rejected key is a misconfiguration and must not be papered over by
        writing to the local disk and reporting success.
        """
        class Rejecting:
            def head_bucket(self, **_):
                raise ingest.ClientError(
                    {"Error": {"Code": "InvalidAccessKeyId"}}, "HeadBucket")
            create_bucket = head_bucket

        monkey = ingest.S3Backend.__new__(ingest.S3Backend)
        monkey.cfg = UNREACHABLE
        monkey.client = Rejecting()
        with pytest.raises(ingest.ClientError):
            monkey.ensure_bucket()

    def test_local_can_be_forced(self):
        assert isinstance(ingest.make_backend(prefer_s3=False), ingest.LocalLakeBackend)
