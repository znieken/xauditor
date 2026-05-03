"""Auth: bcrypt passwords, JWT, and FastAPI dependencies."""

from xauditor_portal.auth.deps import (
    current_user,
    current_user_allow_mcp_path,
    forbid_must_change_password,
    get_portal_settings,
    get_session,
    require_admin,
    require_authenticated,
    require_writer,
)
from xauditor_portal.auth.passwords import (
    PasswordPolicyError,
    hash_password,
    validate_password_policy,
    verify_password,
)
from xauditor_portal.auth.roles import ROLE_VALUES, Role, coerce_role
from xauditor_portal.auth.tokens import (
    COOKIE_NAME,
    TokenError,
    TokenPayload,
    clear_session_cookie,
    decode_token,
    encode_token,
    set_session_cookie,
)

__all__ = [
    "COOKIE_NAME",
    "PasswordPolicyError",
    "ROLE_VALUES",
    "Role",
    "TokenError",
    "TokenPayload",
    "clear_session_cookie",
    "coerce_role",
    "current_user",
    "current_user_allow_mcp_path",
    "decode_token",
    "encode_token",
    "forbid_must_change_password",
    "get_portal_settings",
    "get_session",
    "hash_password",
    "require_admin",
    "require_authenticated",
    "require_writer",
    "set_session_cookie",
    "validate_password_policy",
    "verify_password",
]
