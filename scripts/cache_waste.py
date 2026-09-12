"""Compatibility entry point for the existing cache measurement report."""

import sys

from kandev_pacer.cache_waste import main

if __name__ == "__main__":
    main(sys.argv[1:])
