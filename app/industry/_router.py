"""The single APIRouter shared by every industry submodule.

Lives in its own tiny module so the submodules can register their endpoints on the SAME router by
importing it here, without importing the package __init__ (which imports them — that would be a
circular import). __init__ mounts this router on the app. Mirrors app/reactions/_router.py.
"""
from fastapi import APIRouter, Depends

from app.groups import require_page

# The account-scoped Industry endpoints. Gated by the group page registry — a no-op unless a
# group manager has restricted their members.
router = APIRouter(dependencies=[Depends(require_page("industry"))])

# The customer build-status link (/api/industry/build-status/{id}) must NOT be gated: it is opened
# by people with no account and therefore no group, so any page gate would 403 every customer.
# It lives on its own router rather than being an exception on the gated one, because FastAPI
# router-level dependencies apply to every route and cannot be waived per endpoint.
public_router = APIRouter()
