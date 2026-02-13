"""
Docker execution utilities for the test runner framework.
"""

import subprocess
import logging
from typing import Tuple, Optional, Dict
from pathlib import Path

logger = logging.getLogger(__name__)


# =============================================================================
# Docker Image Management
# =============================================================================


def check_image_exists(image_name: str) -> bool:
    """
    Check if docker image exists locally.

    Args:
        image_name: Docker image name

    Returns:
        True if image exists
    """
    result = subprocess.run(["docker", "images", "-q", image_name], capture_output=True, text=True)
    return bool(result.stdout.strip())


def build_docker_image(dockerfile_path: str, image_name: str, context_dir: Path, verbose: bool = False) -> bool:
    """
    Build docker image from dockerfile.

    Args:
        dockerfile_path: Path to Dockerfile
        image_name: Image name to build
        context_dir: Build context directory
        verbose: Verbose output

    Returns:
        True if build succeeded
    """
    dockerfile_abs = Path(dockerfile_path).resolve()

    if not dockerfile_abs.exists():
        logger.error(f"Dockerfile not found: {dockerfile_abs}")
        return False

    logger.info(f"Building image {image_name} from {dockerfile_abs}...")

    cmd = ["docker", "build", "-f", str(dockerfile_abs), "-t", image_name, str(context_dir)]

    if verbose:
        logger.debug(f"Build command: {' '.join(cmd)}")

    result = subprocess.run(cmd, capture_output=not verbose, text=True)

    if result.returncode != 0:
        logger.error(f"Failed to build image {image_name}")
        if not verbose and result.stderr:
            logger.error(f"Error: {result.stderr}")
        return False

    logger.info(f"Successfully built image {image_name}")
    return True


def cleanup_docker_image(image_name: str, verbose: bool = False) -> bool:
    """
    Remove docker image.

    Args:
        image_name: Image name to remove
        verbose: Verbose output

    Returns:
        True if cleanup succeeded
    """
    if verbose:
        logger.info(f"Cleaning up image {image_name}...")

    result = subprocess.run(["docker", "rmi", "-f", image_name], capture_output=True, text=True)

    if result.returncode != 0:
        if verbose:
            logger.warning(f"Failed to remove image {image_name}: {result.stderr}")
        return False

    if verbose:
        logger.info(f"Removed image {image_name}")
    return True


class DockerRunner:
    """
    Runs commands in a one-shot Docker container (docker run --rm).

    Unlike DockerExecutor which uses docker exec on an existing container,
    this class creates a new container for each command execution.
    """

    def __init__(
        self,
        image_name: str,
        volumes: Optional[Dict[str, str]] = None,
        enable_docker_socket: bool = False,
        use_host_network: bool = False,
        extra_env: Optional[Dict[str, str]] = None,
    ):
        """
        Initialize DockerRunner.

        Args:
            image_name: Docker image to use
            volumes: Volume mappings {host_path: container_path}
            enable_docker_socket: If True, mount /var/run/docker.sock to enable
                                  testcontainers and other Docker-in-Docker use cases
            use_host_network: If True, use --network=host for container networking.
                              Required when testcontainers spawn containers that need
                              to be accessible from the test container.
            extra_env: Additional environment variables to pass to the container
        """
        self.image_name = image_name
        self.volumes = volumes or {}
        self.enable_docker_socket = enable_docker_socket
        self.use_host_network = use_host_network
        self.extra_env = extra_env or {}
        self._container_counter = 0

    def _generate_container_name(self) -> str:
        """Generate a unique container name for tracking."""
        import time

        self._container_counter += 1
        # Use image name (sanitized) + timestamp + counter for uniqueness
        sanitized_image = self.image_name.replace("/", "_").replace(":", "_")
        return f"runner_{sanitized_image}_{int(time.time())}_{self._container_counter}"

    def _kill_container(self, container_name: str) -> None:
        """Kill and remove a container by name."""
        try:
            # First try to kill
            subprocess.run(
                ["docker", "kill", container_name],
                capture_output=True,
                timeout=30,
            )
            logger.debug(f"Killed container: {container_name}")
        except Exception as e:
            logger.warning(f"Failed to kill container {container_name}: {e}")

        try:
            # Then remove (in case --rm didn't work due to kill)
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True,
                timeout=30,
            )
        except Exception:
            pass  # Container might already be removed

    def run(
        self,
        script: str,
        timeout: Optional[int] = None,
        extra_volumes: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, str, str]:
        """
        Run a bash script in a new container.

        Args:
            script: Bash script to execute
            timeout: Timeout in seconds (None for no timeout)
            extra_volumes: Additional volume mappings for this run

        Returns:
            Tuple of (return_code, stdout, stderr)
        """
        container_name = self._generate_container_name()
        # Use --init to properly reap zombie child processes (e.g., plugin processes)
        cmd = ["docker", "run", "--rm", "--init", "--name", container_name]

        # Use host network if enabled (for testcontainers that spawn accessible containers)
        if self.use_host_network:
            cmd.extend(["--network", "host"])

        # Mount Docker socket if enabled (for testcontainers/e2e tests)
        if self.enable_docker_socket:
            cmd.extend(["-v", "/var/run/docker.sock:/var/run/docker.sock"])

        # Add environment variables
        for key, value in self.extra_env.items():
            cmd.extend(["-e", f"{key}={value}"])

        # Add volumes
        all_volumes = {**self.volumes, **(extra_volumes or {})}
        for host_path, container_path in all_volumes.items():
            cmd.extend(["-v", f"{host_path}:{container_path}"])

        cmd.extend([self.image_name, "bash", "-c", script])

        logger.debug(f"Running: docker run --rm --name {container_name} ... {self.image_name}")

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            logger.error(f"Container {container_name} timed out after {timeout}s, killing...")
            self._kill_container(container_name)
            return -1, "", f"Container timed out after {timeout} seconds"
        except Exception as e:
            logger.error(f"Container failed: {e}")
            # Also try to kill on other exceptions
            self._kill_container(container_name)
            return -1, "", str(e)


class DockerExecutor:
    """
    Executes commands in Docker containers.
    """

    def __init__(self, container_name: str):
        """
        Initialize DockerExecutor.

        Args:
            container_name: Name or ID of the Docker container
        """
        self.container_name = container_name

    def exec_command(self, command: str, timeout: int = 300, workdir: Optional[str] = None) -> Tuple[int, str, str]:
        """
        Execute a command in the container.

        Args:
            command: Command to execute
            timeout: Timeout in seconds
            workdir: Working directory inside container

        Returns:
            Tuple of (return_code, stdout, stderr)
        """
        cmd = ["docker", "exec"]

        if workdir:
            cmd.extend(["-w", workdir])

        cmd.extend([self.container_name, "bash", "-c", command])

        logger.debug(f"Executing: {' '.join(cmd)}")

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            logger.error(f"Command timed out after {timeout}s: {command[:100]}...")
            return -1, "", f"Command timed out after {timeout} seconds"
        except Exception as e:
            logger.error(f"Command failed: {e}")
            return -1, "", str(e)

    def exec_script(self, script: str, timeout: int = 300, workdir: Optional[str] = None) -> Tuple[int, str, str]:
        """
        Execute a multi-line script in the container.

        Args:
            script: Bash script content
            timeout: Timeout in seconds
            workdir: Working directory inside container

        Returns:
            Tuple of (return_code, stdout, stderr)
        """
        # Write script to a temp file in container and execute
        escaped_script = script.replace("'", "'\\''")

        # Create and execute script
        setup_cmd = f"cat > /tmp/test_script.sh << 'SCRIPT_EOF'\n{script}\nSCRIPT_EOF\nchmod +x /tmp/test_script.sh && bash /tmp/test_script.sh"

        return self.exec_command(setup_cmd, timeout=timeout, workdir=workdir)

    def copy_from(self, container_path: str, local_path: Path) -> bool:
        """
        Copy file from container to local path.

        Args:
            container_path: Path inside container
            local_path: Local destination path

        Returns:
            True if successful, False otherwise
        """
        # Ensure parent directory exists
        local_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = ["docker", "cp", f"{self.container_name}:{container_path}", str(local_path)]

        logger.debug(f"Copying: {' '.join(cmd)}")

        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            logger.warning(f"Failed to copy {container_path}: {result.stderr.decode()}")
            return False
        return True

    def copy_to(self, local_path: Path, container_path: str) -> bool:
        """
        Copy file from local path to container.

        Args:
            local_path: Local source path
            container_path: Destination path inside container

        Returns:
            True if successful, False otherwise
        """
        cmd = ["docker", "cp", str(local_path), f"{self.container_name}:{container_path}"]

        logger.debug(f"Copying: {' '.join(cmd)}")

        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            logger.warning(f"Failed to copy to {container_path}: {result.stderr.decode()}")
            return False
        return True

    def file_exists(self, container_path: str) -> bool:
        """
        Check if a file exists in the container.

        Args:
            container_path: Path inside container

        Returns:
            True if file exists, False otherwise
        """
        returncode, _, _ = self.exec_command(f"test -f {container_path}")
        return returncode == 0

    def is_running(self) -> bool:
        """
        Check if the container is running.

        Returns:
            True if container is running, False otherwise
        """
        cmd = ["docker", "inspect", "-f", "{{.State.Running}}", self.container_name]
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.returncode == 0 and result.stdout.strip() == "true"
