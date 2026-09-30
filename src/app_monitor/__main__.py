"""``python -m app_monitor`` runs the ``app-monitor`` command."""

import sys

from .cli import main

sys.exit(main())
