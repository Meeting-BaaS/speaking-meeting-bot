"""Process management utilities."""

import subprocess
import time
from typing import Optional

from app.utils.pipecat_logger import logger


def terminate_process_gracefully(
    process: subprocess.Popen, timeout: float = 2.0
) -> bool:
    """
    Terminate a process gracefully by first sending SIGTERM, waiting for it to exit,
    and then forcefully killing it if needed.

    Args:
        process: The process to terminate
        timeout: How long to wait for graceful termination before force killing

    Returns:
        True if process was terminated gracefully, False if it had to be force-killed
    """
    if process.poll() is not None:
        # Process is already terminated
        return True

    # Send SIGTERM
    try:
        process.terminate()

        # Wait for process to exit
        for _ in range(int(timeout * 10)):  # Check 10 times per second
            if process.poll() is not None:
                return True
            time.sleep(0.1)

        # Process didn't exit gracefully, force kill it
        process.kill()
        process.wait(1.0)  # Wait up to 1 second for it to be killed
        return False
    except Exception as e:
        logger.error(f"Error terminating process: {e}")
        # Try one last time with kill
        try:
            process.kill()
        except Exception as e:
            logger.error(f"Final kill attempt failed: {e}")
            pass
        return False
