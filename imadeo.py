"""Desktop executable entry point. SPDX-License-Identifier: GPL-3.0-or-later"""

import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="ImadeoCovert image and video converter")
    parser.add_argument("--self-test", type=Path, metavar="NEW_DIRECTORY",
                        help="Run isolated offline package checks and write report.json")
    args = parser.parse_args()
    if args.self_test is not None:
        from portable_self_test import run
        return run(args.self_test)
    from formatix import App
    App().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
