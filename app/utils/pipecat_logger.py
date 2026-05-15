import sys

from loguru import logger


# def configure_logger(level="INFO"):
def configure_logger(level="INFO"):
    # Remove default logger
    logger.remove()

    # Configure format with consistent colors
    log_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
        "<level>{message}</level>"
    )

    # Add new handler with custom format (to stderr)
    logger.add(
        sys.stderr,
        format=log_format,
        level=level,
        colorize=True,
    )

    # Also store logs in a file
    logger.add(
        "server.log",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} | {message}",
        level=level,
        rotation="10 MB",
    )

    return logger
