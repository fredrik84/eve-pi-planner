"""The single APIRouter shared by every reactions submodule.

Lives in its own tiny module so the submodules (settings, graph, jobs, orders) can all register
their endpoints on the SAME router by importing it here, without importing the package __init__
(which imports them — that would be a circular import). __init__ mounts this router on the app.
"""
from fastapi import APIRouter

router = APIRouter()
