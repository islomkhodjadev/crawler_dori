"""Choose an installed browser without changing fingerprints or personal profiles."""
import os
import shutil


def browser_channel():
    channel=os.getenv('DORI_BROWSER_CHANNEL','chromium').strip().lower()
    if channel not in ('chromium','chrome'):
        raise ValueError('DORI_BROWSER_CHANNEL must be chromium or chrome')
    return channel


def _has_display():
    """Return True when a graphical display is available (X11, Wayland, or macOS)."""
    import platform
    if platform.system()=='Darwin':
        return True
    return bool(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'))


def launch_options(visible=False, interactive=False):
    """Return Playwright launch kwargs.

    Interactive sessions (the UI browser viewer) need Chrome's full rendering
    engine so the site sees a genuine browser.  On a headless server there is
    no display, so we pass --headless=new: Chrome's modern headless mode shares
    the same renderer as visible Chrome (unlike the legacy --headless flag) and
    is not flagged as a bot by WAF tools such as SafeLine.
    """
    if visible or interactive:
        if _has_display():
            headless = False
            args = []
        else:
            # No display: use Chrome's new headless mode (same engine, no display needed).
            headless = True
            args = ['--headless=new']
    else:
        headless = os.getenv('DORI_HEADLESS','true').lower()!='false'
        args = []

    options: dict = {'headless': headless}
    if args:
        options['args'] = args
    if browser_channel()=='chrome':
        options['channel']='chrome'
    if os.getenv('DORI_BROWSER_PROXY'):
        options['proxy']={'server':os.environ['DORI_BROWSER_PROXY']}
    return options
