# Portions derived from lidseeker (https://github.com/IvoryCobra-VC/lidseeker), MIT License.
"""bcrypt password hashing (hashes are compatible with lidseeker's, so its users import as-is)."""
import bcrypt

from ..errors import ApiError

MIN_PASSWORD_LEN = 8


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def check_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def validate(password: str) -> None:
    if len(password or "") < MIN_PASSWORD_LEN:
        raise ApiError(400, "weak_password",
                       f"Password must be at least {MIN_PASSWORD_LEN} characters.")


# A real hash to compare against when the username doesn't exist, so a login for an unknown user
# costs the same bcrypt time as a wrong password (no user enumeration by timing).
_DUMMY_HASH = bcrypt.hashpw(b"continuo-dummy", bcrypt.gensalt(rounds=12)).decode()


def burn_time(password: str) -> None:
    check_password(password, _DUMMY_HASH)
