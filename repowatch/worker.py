import argparse
import asyncio
from pathlib import Path

from .store import Store
from .workflow import Worker


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    worker = Worker(Store(args.database))
    asyncio.run(worker.tick() if args.once else worker.run())


if __name__ == "__main__":
    main()
