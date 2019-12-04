import sys
import json
import asyncio
from os import environ
from ast import literal_eval
from pathlib import Path
from argparse import ArgumentParser, Action

from . import __version__, Robot


class EnvDefault(Action):
    def __init__(self, envvar, required=True, default=None, **kwargs):
        default = environ.get(envvar, default)
        if required and default is not None:
            required = False
        super().__init__(default=default, required=required, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)


def load_scripts(robot, scripts):
    robot.load(Path(".").joinpath("scripts"))
    load_external_scripts(robot)
    for path in scripts:
        robot.load(path.absolute())
    robot.emit("scripts-loaded")


def load_external_scripts(robot):
    fp = Path(".").joinpath("external-scripts.json").absolute()
    if not fp.exists():
        return

    data = fp.read_bytes()
    try:
        robot.load_external_scripts(json.loads(data))
    except Exception:
        robot.logger.error("Error parsing JSON data from external-scripts.json",
                           exc_info=True)
        sys.exit(1)


def main(args):
    if not args.disable_httpd:
        httpd = True
    else:
        try:
            httpd = literal_eval(environ.get("HUBOT_HTTPD", "False"))
        except ValueError:
            httpd = False

    robot = Robot(args.adapter, httpd, args.name, args.alias)
    scripts = args.scripts or list()

    if args.config_check:
        load_scripts(robot, scripts)
        return print("Config checked, OK!")

    try:
        import uvloop
        if hasattr(uvloop, 'install'):
            uvloop.install()
        else:
            asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except ImportError:
        robot.logger.debug("uvloop not available.")
    except Exception:
        robot.logger.error("Error to setup uvloop with asyncio", exc_info=True)

    robot.adapter.once("connected", lambda: load_scripts(robot, scripts))
    try:
        robot.run()
    except KeyboardInterrupt:
        pass
    finally:
        loop = robot._loop
        to_cancel = {t for t in asyncio.Task.all_tasks(loop) if not t.done()}
        for task in to_cancel:
            task.cancel()
        coro = asyncio.gather(*to_cancel, loop=loop, return_exceptions=True)
        loop.run_until_complete(coro)
        for task in to_cancel:
            if task.exception() is not None:
                loop.call_exception_handler(dict(
                    message="unhandled exception during exiting.",
                    exception=task.exception(),
                    task=task
                ))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


if __name__ == '__main__':
    arg = ArgumentParser(prog="aiohubot", description=__doc__)
    arg.add_argument("-a", "--adapter", type=str, default="shell",
                     action=EnvDefault, envvar="HUBOT_ADAPTER",
                     help=r"The Adapter to be use")
    arg.add_argument("-d", "--disable-httpd", default=False,
                     action="store_true", help=r"Disable HTTP server")
    arg.add_argument("-l", "--alias", type=str, default="/",
                     action=EnvDefault, envvar="HUBOT_ALIAS",
                     help=r"Enable replacing the robot's name with alias")
    arg.add_argument("-n", "--name", type=str, default="Hubot",
                     action=EnvDefault, envvar="HUBOT_NAME",
                     help=r"The name of the robot in chat")
    arg.add_argument("-r", "--require", type=Path, dest="scripts", nargs="*",
                     help=r"Alternative scripts path")
    arg.add_argument("-t", "--config-check", action="store_true",
                     help=r"Test aiohubot's config to make sure it won't fail at startup")
    arg.add_argument("-v", "--version", action="version", version=__version__,
                     help=r"Displays the version of hubot installed")

    main(arg.parse_args())
