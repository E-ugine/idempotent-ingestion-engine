import re

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

bearer_scheme = HTTPBearer()

# account_id has no local FK to validate against (it's owned by an external
# service), so this is a shape check only: non-empty, opaque identifier.
_ACCOUNT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def get_current_account_id(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> str:
    """
    Stub auth dependency. Real JWT verification (signature, expiry,
    issuer/audience claims) is separate follow-up work -- for now the
    bearer token's subject is trusted as-is, and only shape-validated.
    """
    account_id = credentials.credentials.strip()

    if not account_id or not _ACCOUNT_ID_PATTERN.match(account_id):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed account identity.",
        )

    return account_id
