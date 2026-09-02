#!/usr/bin/env python
"""Django valdymo įrankis web sąsajai ir CLI komandoms."""
from __future__ import annotations

import os
import sys


def main() -> None:
    """Paleidžia Django komandų eilutės vykdymą."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "webapp.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
