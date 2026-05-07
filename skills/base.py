"""Technical implementation for Hummingbot Gateway V2.1."""

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict


class SkillBase(ABC):
    """Abstract base class for agent skills running inside the Gateway runtime.

    A skill is a deterministic, side-effect-free advisor that the agent
    framework consults before committing to an action. The contract is:

        * ``name`` uniquely identifies the skill within the agent.
        * ``logger()`` returns a namespaced logger for diagnostic output.
        * ``evaluate(context)`` consumes a context dictionary and returns
          a structured result dictionary. Subclasses must implement this.

    Skills must not perform I/O directly. Required market data, portfolio
    state, or external lookups are injected via the constructor (typically
    as callables) so the skill remains unit-testable without a live
    exchange connection.
    """

    def __init__(self, name: str) -> None:
        """Initialize the skill with a stable identifier.

        :param name: Non-empty string identifying the skill within the
            agent. Also used as the suffix for the skill-scoped logger.
        :raises ValueError: If ``name`` is empty or not a string.
        """
        if not isinstance(name, str) or not name:
            raise ValueError("Skill name must be a non-empty string.")
        self._name: str = name
        self._logger: logging.Logger = logging.getLogger(f"skills.{name}")

    @property
    def name(self) -> str:
        """Return the skill's stable identifier."""
        return self._name

    def logger(self) -> logging.Logger:
        """Return the skill-scoped logger for diagnostic output."""
        return self._logger

    @abstractmethod
    def evaluate(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Compute the skill's output for a given decision context.

        :param context: Dictionary describing the decision context. The
            keys required are skill-specific and documented on each
            concrete subclass.
        :return: Dictionary describing the skill's verdict. The schema
            is skill-specific.
        """
        raise NotImplementedError
