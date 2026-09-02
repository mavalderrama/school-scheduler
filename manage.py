#!/usr/bin/env python
"""Utilidad de línea de comandos de Django (migrate, makemigrations, changepassword, ...)."""

from __future__ import annotations

import sys


def main() -> None:
    from app.django_bootstrap import setup_django

    setup_django()
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
