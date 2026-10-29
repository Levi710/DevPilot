"""Tests for local JWT signing and read-only GitHub App identity checks."""

import json
from pathlib import Path
import sys

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from devpilot.github_app_auth import (
    GitHubAppAuthenticationError,
    create_app_jwt,
    download_repository_archive,
    get_app_identity,
    resolve_default_branch_sha,
    verify_single_installation_repository,
)
from devpilot.github_app_config import GitHubAppConfiguration


@pytest.fixture
def app_config(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path = tmp_path / "app.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return GitHubAppConfiguration(app_id=123, installation_id=456, private_key_path=key_path)


def test_create_app_jwt_is_short_lived_and_identifies_only_configured_app(app_config):
    token = create_app_jwt(app_config, now=1_700_000_000)
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["iss"] == "123"
    assert claims["iat"] == 1_699_999_940
    assert claims["exp"] == 1_700_000_540


def test_identity_check_uses_get_app_and_rejects_mismatch(app_config):
    captured = {}

    class Response:
        def read(self):
            return json.dumps({"id": 123, "slug": "devpilot", "name": "DevPilot"}).encode()
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False

    def opener(request, timeout):
        captured["url"] = request.full_url
        captured["method"] = request.method
        captured["authorization"] = request.get_header("Authorization")
        assert timeout == 15
        return Response()

    identity = get_app_identity(app_config, opener=opener)
    assert identity.slug == "devpilot"
    assert captured["url"].endswith("/app")
    assert captured["method"] == "GET"
    assert captured["authorization"].startswith("Bearer ")

    class WrongResponse(Response):
        def read(self):
            return json.dumps({"id": 999, "slug": "other", "name": "Other"}).encode()

    with pytest.raises(GitHubAppAuthenticationError, match="did not match"):
        get_app_identity(app_config, opener=lambda *_args, **_kwargs: WrongResponse())


def test_installation_check_mints_memory_only_token_and_requires_one_repository(app_config):
    captured = []

    class Response:
        def __init__(self, payload):
            self.payload = payload
        def read(self):
            return json.dumps(self.payload).encode()
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False

    responses = iter([
        Response({"token": "ghs_test_token_not_persisted"}),
        Response({"repositories": [{"id": 42, "node_id": "R_42", "full_name": "Levi710/example", "default_branch": "main"}]}),
    ])
    def opener(request, timeout):
        captured.append((request.full_url, request.method, request.get_header("Authorization")))
        return next(responses)

    repository = verify_single_installation_repository(app_config, opener=opener)
    assert repository.full_name == "Levi710/example"
    assert captured[0][0].endswith("/app/installations/456/access_tokens")
    assert captured[0][1] == "POST"
    assert captured[1][0].endswith("/installation/repositories?per_page=100")
    assert captured[1][1] == "GET"
    assert captured[1][2] == "Bearer ghs_test_token_not_persisted"


def test_installation_check_rejects_multiple_repositories(app_config):
    class Response:
        def __init__(self, payload): self.payload = payload
        def read(self): return json.dumps(self.payload).encode()
        def __enter__(self): return self
        def __exit__(self, *_args): return False
    responses = iter([Response({"token": "ghs_test"}), Response({"repositories": [{}, {}]})])
    with pytest.raises(GitHubAppAuthenticationError, match="exactly one"):
        verify_single_installation_repository(app_config, opener=lambda *_args, **_kwargs: next(responses))


def test_resolves_default_branch_sha_with_ephemeral_installation_token(app_config):
    repository = type("Repository", (), {"full_name": "Levi710/example", "default_branch": "main"})()

    class Response:
        def __init__(self, payload): self.payload = payload
        def read(self, _size=-1): return json.dumps(self.payload).encode()
        def __enter__(self): return self
        def __exit__(self, *_args): return False

    responses = iter([Response({"token": "ghs_memory_only"}), Response({"sha": "a" * 40})])
    assert resolve_default_branch_sha(app_config, repository, opener=lambda *_args, **_kwargs: next(responses)) == "a" * 40


def test_downloads_bounded_archive_without_persisting_token(app_config, tmp_path):
    repository = type("Repository", (), {"full_name": "Levi710/example"})()
    destination = tmp_path / "archive.zip"

    class Response:
        def __init__(self, payload, headers=None):
            self.payload, self.headers = payload, headers or {}
            self.offset = 0
        def read(self, size=-1):
            if size < 0: size = len(self.payload) - self.offset
            value = self.payload[self.offset:self.offset + size]
            self.offset += len(value)
            return value
        def __enter__(self): return self
        def __exit__(self, *_args): return False

    responses = iter([Response(b'{"token": "ghs_memory_only"}'), Response(b"archive", {"Content-Length": "7"})])
    download_repository_archive(app_config, repository, "b" * 40, destination, opener=lambda *_args, **_kwargs: next(responses))
    assert destination.read_bytes() == b"archive"
