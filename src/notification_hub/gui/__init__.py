"""Desktop client support without importing the optional native GUI runtime."""

from .bridge import GuiBridge
from .controller import GuiController

__all__ = ["GuiBridge", "GuiController"]
