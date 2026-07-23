"""The single APIRouter shared by every industry submodule.

Lives in its own tiny module so the submodules can register their endpoints on the SAME router by
importing it here, without importing the package __init__ (which imports them — that would be a
circular import). __init__ mounts this router on the app. Mirrors app/reactions/_router.py.
"""
from fastapi import APIRouter

router = APIRouter()
