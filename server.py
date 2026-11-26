#!/usr/bin/env python3
"""Compatibility entrypoint: python3 server.py"""
from employment.server import create_server, main

__all__ = ["create_server", "main"]

if __name__ == "__main__":
    main()
