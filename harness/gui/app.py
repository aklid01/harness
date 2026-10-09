"""Desktop entry point: `harness-gui` (and later `harness`)."""

import sys

from PySide6.QtWidgets import QApplication

from .main_window import APP_VERSION, MainWindow


def main():
    from .. import config as config_mod
    from .. import paths as paths_mod
    from .. import session as session_mod

    paths_mod.init()
    paths_mod.migrate_providers()
    config_mod.reload()
    session_mod.migrate_root_sessions()
    app = QApplication(sys.argv)
    app.setApplicationName("Harness")
    app.setApplicationVersion(APP_VERSION)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
