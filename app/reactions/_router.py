"""The single APIRouter shared by every reactions submodule.

Lives in its own tiny module so the submodules (settings, graph, jobs, orders) can all register
their endpoints on the SAME router by importing it here, without importing the package __init__
(which imports them — that would be a circular import). __init__ mounts this router on the app.
"""
from fastapi import APIRouter, Depends

from app.groups import require_page

# Every endpoint on this router is account-scoped Reactions data, so the group page gate applies
# to all of them (no public routes here — verified). require_page is a no-op unless a group
# manager has actually restricted their members' pages.
router = APIRouter(dependencies=[Depends(require_page("reactions"))])
