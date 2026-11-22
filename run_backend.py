import sys


def _tolerate_unencodable_output():
    # 打包版由 Tauri 以 stdout=NUL 启动，Python 按系统代码页（中文 Windows 是 GBK）编码输出；
    # 翻译模板会打印模型输出，遇到 ♪、♡、emoji 就抛 UnicodeEncodeError，整批翻译被判失败。
    # 保留原编码（控制台里中文照常可读），只把编不出的字符换成 '?'。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, OSError, ValueError):
            pass  # 无控制台时为 None，或已被替换成不支持 reconfigure 的对象


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--apply-update":
        import application_update
        return application_update.main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "--extraction-python":
        from extraction_python import main as run_python
        return run_python(sys.argv[2:])
    _tolerate_unencodable_output()
    from GalTransl.server import main as serve
    return serve()


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    raise SystemExit(main())
