#!/usr/bin/env python3
"""Pre-tool hook - pass through"""
import sys
import json

if __name__ == '__main__':
    # Read stdin, pass through to stdout
    data = sys.stdin.read()
    if data:
        print(data, end='')
    sys.exit(0)
