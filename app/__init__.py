"""Speaking Meeting Bot API package."""


def get_application():
    """Get FastAPI application instance."""
    from app.main import create_app

    return create_app()

# App initialization is handled in app.main
