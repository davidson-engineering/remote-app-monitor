"""``python -m sightglass`` runs the ``sightglass`` command."""

import sys

from .cli import main

sys.exit(main())
