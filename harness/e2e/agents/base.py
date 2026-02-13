"""Abstract base class for agent frameworks."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Type


class AgentFramework(ABC):
    """Abstract base class for agent framework implementations.

    Each agent framework (e.g., Claude Code, OpenHands) should implement this
    interface to provide agent-specific configuration for:
    - Container mounts (credentials, binaries)
    - Container initialization scripts
    - Command building for run/resume operations
    """

    FRAMEWORK_NAME: str = "unknown"

    def __init__(self, **kwargs):
        """Initialize the framework.

        Args:
            **kwargs: Framework-specific options (e.g., reasoning_effort for Codex).
                      Subclasses should override to handle their specific options.
        """
        # Base class ignores unknown kwargs for forward compatibility
        pass

    @abstractmethod
    def get_container_mounts(self) -> List[str]:
        """Return Docker volume mount arguments for the agent.

        Returns:
            List of -v arguments for docker run (e.g., ["-v", "/src:/dst:ro"])
        """

    @abstractmethod
    def get_container_init_script(self, agent_name: str) -> str:
        """Return Python script for container initialization.

        This script runs as root inside the container after launch to set up
        agent-specific directories, credentials, and tools.

        Args:
            agent_name: Git user name for agent commits

        Returns:
            Python script as a string
        """

    @abstractmethod
    def build_run_command(
        self,
        model: str,
        session_id: str,
        prompt_path: str,
    ) -> str:
        """Build the shell command to run the agent.

        Args:
            model: Model identifier (e.g., "claude-sonnet-4-5-20250929")
            session_id: Session ID for conversation tracking
            prompt_path: Path to prompt file inside container

        Returns:
            Shell command string to execute
        """

    @abstractmethod
    def build_resume_command(
        self,
        model: str,
        session_id: str,
        message_path: str,
    ) -> str:
        """Build the shell command to resume an existing session.

        Args:
            model: Model identifier
            session_id: Session ID to resume
            message_path: Path to message file inside container

        Returns:
            Shell command string to execute
        """

    def get_container_env_vars(self) -> List[str]:
        """Return Docker environment variable arguments.

        Override this method to pass environment variables to the container.

        Returns:
            List of -e arguments for docker run (e.g., ["-e", "KEY=value"])
        """
        return []


# Registry of available agent frameworks
_FRAMEWORK_REGISTRY: Dict[str, Type[AgentFramework]] = {}


def register_framework(name: str):
    """Decorator to register an agent framework class.

    Args:
        name: Framework name for registration (e.g., "claude-code")

    Returns:
        Class decorator
    """

    def decorator(cls: Type[AgentFramework]) -> Type[AgentFramework]:
        _FRAMEWORK_REGISTRY[name] = cls
        return cls

    return decorator


def get_agent_framework(name: str, **kwargs) -> AgentFramework:
    """Factory function to get an agent framework instance.

    Args:
        name: Framework name (e.g., "claude-code")
        **kwargs: Additional arguments passed to the framework constructor.
                  For Codex: reasoning_effort ("low", "medium", "high")

    Returns:
        AgentFramework instance

    Raises:
        ValueError: If framework is not supported
    """
    # Import implementations to trigger registration
    from harness.e2e.agents import claude_code  # noqa: F401
    from harness.e2e.agents import codex  # noqa: F401
    from harness.e2e.agents import gemini  # noqa: F401
    from harness.e2e.agents import openhands  # noqa: F401

    if name not in _FRAMEWORK_REGISTRY:
        available = ", ".join(_FRAMEWORK_REGISTRY.keys()) or "none"
        raise ValueError(f"Unknown agent framework: {name}. Available: {available}")

    return _FRAMEWORK_REGISTRY[name](**kwargs)
