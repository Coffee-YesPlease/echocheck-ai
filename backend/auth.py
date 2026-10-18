"""Password and opaque-token primitives. No built-in users or credentials."""
from __future__ import annotations
import hashlib
import hmac
import secrets

PASSWORD_ITERATIONS = 600_000

def token():
    return secrets.token_urlsafe(32)

def token_hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()

def hash_password(password, prototype=False):
    minimum = 6 if prototype else 12
    if not isinstance(password, str) or not minimum <= len(password) <= 1024:
        raise ValueError(f'Password must contain {minimum} to 1024 characters')
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, PASSWORD_ITERATIONS)
    return f'pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}'

def verify_password(password, encoded):
    try:
        algorithm, iterations, salt, expected = encoded.split('$')
        if algorithm != 'pbkdf2_sha256' or not 600_000 <= int(iterations) <= 2_000_000 or not isinstance(password, str) or len(password) > 1024:
            return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError, AttributeError):
        return False

# Used for missing usernames so authentication does the same expensive operation.
DUMMY_PASSWORD_HASH = hash_password(token())
