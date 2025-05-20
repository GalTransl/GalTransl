import argparse, traceback
from asyncio import get_event_loop, run, new_event_loop, set_event_loop
from GalTransl.ConfigHelper import CProjectConfig
from GalTransl.Runner import run_galtransl
from GalTransl.i18n import get_text,GT_LANG
from GalTransl import (
    PROGRAM_SPLASH,
    TRANSLATOR_SUPPORTED,
    GALTRANSL_VERSION,
    AUTHOR,
    CONTRIBUTORS,
    LOGGER,
    DEBUG_LEVEL,
)


def worker(project_dir: str, config_file_name: str, translator: str, show_banner=True):
    if not project_dir or not isinstance(project_dir, str):
        LOGGER.error(get_text("error_project_path_empty", GT_LANG))
        return False
    if not config_file_name or not isinstance(config_file_name, str):
        LOGGER.error(get_text("error_config_file_empty", GT_LANG))
        return False
    if not translator or not isinstance(translator, str):
        LOGGER.error(get_text("error_translator_empty", GT_LANG))
        return False

    if show_banner:
        print(PROGRAM_SPLASH)
        print(f"GalTransl Core version: {GALTRANSL_VERSION}")
        print(f"Author: {AUTHOR}")
        print(f"Contributors: {CONTRIBUTORS}")

    try:
        cfg = CProjectConfig(project_dir, config_file_name)
        LOGGER.setLevel(DEBUG_LEVEL[cfg.getCommonConfigSection().get("loggingLevel", "info")])
    except Exception as ex:
        LOGGER.error(get_text("error_loading_config", GT_LANG, str(ex)))
        return False

    try:
        loop = get_event_loop()
    except RuntimeError:
        LOGGER.info(get_text("error_creating_event_loop", GT_LANG))
        loop = new_event_loop()
        set_event_loop(loop)

    try:
        run(run_galtransl(cfg, translator))
    except KeyboardInterrupt:
        loop.stop()
    except RuntimeError as ex:
        LOGGER.error(get_text("program_error", GT_LANG, ex))
    except BaseException as ex:
        LOGGER.error(get_text("error_unexpected", GT_LANG, str(ex)))
        LOGGER.debug("Detailed error information:", exc_info=True)
        traceback.print_exception(type(ex), ex, ex.__traceback__)
    finally:
        try:
            loop.close()
        except Exception as ex:
            LOGGER.error(get_text("error_closing_event_loop", GT_LANG, str(ex)))
        return True


def main() -> int:
    parser = argparse.ArgumentParser("GalTransl")
    parser.add_argument("--project_dir", "-p", help="project folder", required=True)
    parser.add_argument(
        "--translator",
        "-t",
        choices=TRANSLATOR_SUPPORTED.keys(),
        help="choose which Translator to use",
        required=True,
    )
    parser.add_argument(
        "--debug-level",
        "-l",
        choices=DEBUG_LEVEL.keys(),
        help="debug level",
        default="info",
    )
    parser.add_argument(
        "--language",
        "-lang",
        choices=["zh-cn", "en"],
        help="UI language",
        default="zh-cn",
    )
    args = parser.parse_args()
    # logging level
    LOGGER.setLevel(DEBUG_LEVEL[args.debug_level])

    print(PROGRAM_SPLASH)
    print(f"GalTransl Core version: {GALTRANSL_VERSION}")
    print(f"Author: {AUTHOR}")
    print(f"Contributors: {CONTRIBUTORS}")

    return worker(args.project_dir, "config.yaml", args.translator, ui_lang=args.language)


if __name__ == "__main__":
    main()
