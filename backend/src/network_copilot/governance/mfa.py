"""RFC 6238 TOTP with encrypted secrets, replay prevention and one-use recovery."""
import base64
import hashlib
import hmac
import secrets
import struct
import time

from sqlalchemy import update
from werkzeug.security import check_password_hash, generate_password_hash

from ..auth.model import User
from ..credentials.service import get_cipher
from ..extensions import db


def totp(secret, counter):
    digest = hmac.new(base64.b32decode(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 15
    return f"{(struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7fffffff) % 1000000:06d}"


def verify(user, code, pending=False):
    encrypted = user.mfa_pending_secret if pending else user.mfa_secret
    if not encrypted or not isinstance(code, str):
        return False
    secret = get_cipher().decrypt(encrypted)
    counter = int(time.time() // 30)
    for candidate in (counter, counter - 1, counter + 1):
        if candidate > user.mfa_last_counter and hmac.compare_digest(totp(secret, candidate), code):
            claimed = db.session.execute(update(User).where(User.id == user.id, User.mfa_last_counter < candidate)
                                         .values(mfa_last_counter=candidate))
            if claimed.rowcount != 1:
                db.session.rollback()
                return False
            db.session.commit()
            return True
    return False


def recovery_codes(user):
    codes = [secrets.token_hex(12) for _ in range(8)]
    user.recovery_hashes = [generate_password_hash(code) for code in codes]
    return codes


def recover(user, code):
    if not isinstance(code, str):
        return False
    original = list(user.recovery_hashes or [])
    for stored in original:
        if check_password_hash(stored, code):
            remaining = [entry for entry in original if entry != stored]
            claimed = db.session.execute(update(User).where(User.id == user.id, User.recovery_hashes == original)
                                         .values(recovery_hashes=remaining, token_version=User.token_version + 1))
            db.session.commit()
            return claimed.rowcount == 1
    return False
