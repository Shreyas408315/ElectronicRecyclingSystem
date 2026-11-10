from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from database.connection import initialize_database
from ui.main_window import MainWindow
from ui.auth_dialogs import FirstRunAdministratorDialog, LoginDialog
from database.connection import get_db
from services.security_service import is_session_locked, needs_first_run_setup

def main():
    initialize_database()
    app = QApplication([])
    app.setApplicationName("Electronic Recycling Factory")
    app.setStyle("Fusion")
    with get_db() as session:
        first_run = needs_first_run_setup(session)
    if first_run:
        setup = FirstRunAdministratorDialog()
        if setup.exec() != setup.DialogCode.Accepted:
            return
    login = LoginDialog()
    if login.exec() != login.DialogCode.Accepted:
        return
    window = MainWindow(current_user=login.authenticated_session)
    window.show()

    # The session state is memory-only.  Locking returns to the login screen
    # rather than retaining a recoverable credential or token on disk.
    def check_session_lock():
        nonlocal window
        if not is_session_locked() or not window.isVisible():
            return
        window.clear_protected_page_state()
        window.hide()
        relogin = LoginDialog()
        if relogin.exec() == relogin.DialogCode.Accepted:
            window.close()
            window = MainWindow(current_user=relogin.authenticated_session)
            window.show()
        else:
            window.close()

    lock_timer = QTimer(app)
    lock_timer.timeout.connect(check_session_lock)
    lock_timer.start(30_000)
    app.exec()

if __name__ == "__main__":
    main()
