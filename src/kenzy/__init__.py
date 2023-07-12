import os


__app_name__ = "kenzy"
__app_title__ = "KENZY"

with open(os.path.join(os.path.dirname(__file__), "VERSION"), "r", encoding="UTF-8") as fp:
    __version__ = fp.readline().strip()

VERSION = [(int(x) if x.isnumeric() else x) for x in __version__.split(".")]