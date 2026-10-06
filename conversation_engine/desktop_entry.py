"""Windowed desktop entry point, also used by the PyInstaller bundle."""
import os
from pathlib import Path
import sys


def main():
    # Resolve the explicit startup directory before runtime_paths is imported.
    for index, arg in enumerate(sys.argv[1:], 1):
        if arg == '--data-root' and index + 1 < len(sys.argv):
            os.environ['WECHAT_ANALYZER_DATA'] = sys.argv[index + 1]
        elif arg.startswith('--data-root='):
            os.environ['WECHAT_ANALYZER_DATA'] = arg.split('=', 1)[1]
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parent / 'scripts'))
    from desktop_assistant import main as desktop_main
    return desktop_main()


if __name__ == '__main__':
    # Frozen ProcessPoolExecutor children must enter the spawn worker instead of
    # opening another desktop window or parsing multiprocessing's private flags.
    import multiprocessing
    multiprocessing.freeze_support()
    raise SystemExit(main())
