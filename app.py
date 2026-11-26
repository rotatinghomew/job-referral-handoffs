#!/usr/bin/env python3
"""Conventional entrypoint: python3 app.py"""
from employment.server import create_server, main

__all__ = ["create_server", "main"]

if __name__ == "__main__":
    main()
