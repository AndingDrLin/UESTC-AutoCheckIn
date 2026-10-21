"""Allow ``python -m app`` as a shortcut for ``python -m app.cli``."""
from .cli import main

raise SystemExit(main())
