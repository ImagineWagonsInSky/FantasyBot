"""`python -m fantasybot` (how the Docker image runs it)."""

import sys

from .cli import main

sys.exit(main())
