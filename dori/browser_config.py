"""Choose an installed browser without changing fingerprints or personal profiles."""
import os


def browser_channel():
    channel=os.getenv('DORI_BROWSER_CHANNEL','chromium').strip().lower()
    if channel not in ('chromium','chrome'):
        raise ValueError('DORI_BROWSER_CHANNEL must be chromium or chrome')
    return channel


def launch_options(visible=False, interactive=False):
    # Interactive sessions (user-controlled browser viewer) always run non-headless so
    # that the site sees a real browser. DORI_HEADLESS only applies to automated crawls.
    headless = False if (visible or interactive) else os.getenv('DORI_HEADLESS','true').lower()!='false'
    options={'headless':headless}
    if browser_channel()=='chrome':options['channel']='chrome'
    if os.getenv('DORI_BROWSER_PROXY'):
        options['proxy']={'server':os.environ['DORI_BROWSER_PROXY']}
    return options
