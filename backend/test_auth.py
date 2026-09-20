"""Unit tests for auth.py's password hashing and session-token logic -
pure functions, no HTTP/DB involved, so these run independent of the API
tests validating correctness (not just "does it not crash")."""

import time

import pytest

import auth


def test_hash_password_roundtrips_with_verify_password():
    hashed = auth.hash_password("correct horse battery staple")
    assert auth.verify_password("correct horse battery staple", hashed) is True


def test_verify_password_rejects_wrong_password():
    hashed = auth.hash_password("correct horse battery staple")
    assert auth.verify_password("wrong password", hashed) is False


def test_hash_password_uses_a_fresh_salt_each_time():
    """Two hashes of the same password must differ (random salt) - otherwise
    identical passwords would be trivially detectable from stored hashes."""
    a = auth.hash_password("same password")
    b = auth.hash_password("same password")
    assert a != b
    assert auth.verify_password("same password", a) is True
    assert auth.verify_password("same password", b) is True


def test_verify_password_rejects_malformed_stored_hash():
    assert auth.verify_password("anything", "not-a-real-hash") is False
    assert auth.verify_password("anything", "") is False


def test_session_token_roundtrips_to_the_same_user_id(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_SECRET_KEY_PATH", tmp_path / "secret")
    token = auth.create_session_token(42)
    assert auth.verify_session_token(token) == 42


def test_session_token_rejects_tampering(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_SECRET_KEY_PATH", tmp_path / "secret")
    token = auth.create_session_token(1)
    # Flip the last character - corrupts the signature, must not verify as
    # some other valid user id.
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert auth.verify_session_token(tampered) is None


def test_session_token_rejects_garbage_input(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_SECRET_KEY_PATH", tmp_path / "secret")
    assert auth.verify_session_token("not a real token") is None
    assert auth.verify_session_token("") is None


def test_session_token_rejects_expired_token(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_SECRET_KEY_PATH", tmp_path / "secret")
    monkeypatch.setattr(auth, "SESSION_MAX_AGE_SECONDS", -1)  # already expired the instant it's minted
    token = auth.create_session_token(7)
    assert auth.verify_session_token(token) is None


def test_secret_key_persists_across_calls(tmp_path, monkeypatch):
    """The signing secret must be stable within a run (and across a server
    restart) or every previously issued session token would stop verifying."""
    monkeypatch.setattr(auth, "_SECRET_KEY_PATH", tmp_path / "secret")
    key_a = auth._get_secret_key()
    key_b = auth._get_secret_key()
    assert key_a == key_b


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
