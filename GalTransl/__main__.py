import argparse
from GalTransl.i18n import get_text,GT_LANG
from GalTransl.Service import JobSpec, run_job
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

    state = run_job(
        JobSpec(
            project_dir=project_dir,
            config_file_name=config_file_name,
            translator=translator,
        )
    )
    return state.success


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

    success = worker(args.project_dir, "config.yaml", args.translator)
    return 0 if success else 1


if __name__ == "__main__":
    main()
