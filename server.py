"""Run `python server.py` (asks for a password on first start). Settings: ytweb/config.py."""
import sys

from ytweb import auth, config
from ytweb.web import app  # noqa: F401  (uvicorn server:app)

if __name__ == "__main__":
    if not config.AUTH_PATH.exists():
        try:
            auth.setup_password()
        except (KeyboardInterrupt, EOFError):
            print("\nPassword setup cancelled.")
            sys.exit(1)
    try:
        auth.load_auth()
    except auth.AuthFileDamaged:
        print(auth.AUTH_DAMAGED_MESSAGE, file=sys.stderr)
        sys.exit(1)

    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=8765, reload=False, proxy_headers=config.TRUST_PROXY_HEADERS)
