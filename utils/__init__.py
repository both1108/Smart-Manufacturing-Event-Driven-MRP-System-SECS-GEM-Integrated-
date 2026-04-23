"""Utility helpers that don't fit any specific domain layer.

Keep this package small. Anything that grows domain-specific should
migrate to services/ or repositories/ — utils/ is intentionally a
thin shelf for cross-cutting primitives (time, id generation, etc.).
"""
