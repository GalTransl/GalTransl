import sys


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--extraction-python":
        from extraction_python import main as run_python
        return run_python(sys.argv[2:])
    from GalTransl.server import main as serve
    return serve()


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    raise SystemExit(main())
