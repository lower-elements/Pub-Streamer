import os
import sys

import wx

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from pubstreamer.config import Config
from pubstreamer.i18n import setup as i18n_setup
from pubstreamer.ui.app import PubStreamerFrame


def main():
    config = Config("config.ini")
    i18n_setup(config.language)
    app = wx.App(False)
    frame = PubStreamerFrame(None, config)
    frame.Show()
    app.MainLoop()


if __name__ == "__main__":
    main()
