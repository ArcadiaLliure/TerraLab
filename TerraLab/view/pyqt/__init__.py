"""PyQt presentation adapters.

Concrete Qt modules are intentionally not imported here: importing a neutral
router or inspecting this namespace must not load a toolkit until the
``qpainter`` backend has actually been selected.
"""

__all__ = ()
