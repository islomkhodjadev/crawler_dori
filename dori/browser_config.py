"""Choose an installed browser without changing fingerprints or personal profiles."""
import os


def browser_channel():
    channel=os.getenv('DORI_BROWSER_CHANNEL','chromium').strip().lower()
    if channel not in ('chromium','chrome'):
        raise ValueError('DORI_BROWSER_CHANNEL must be chromium or chrome')
    return channel


def launch_options(visible=False):
    options={'headless':False if visible else os.getenv('DORI_HEADLESS','true').lower()!='false'}
    if browser_channel()=='chrome':options['channel']='chrome'
    if os.getenv('DORI_BROWSER_PROXY'):
        options['proxy']={'server':os.environ['DORI_BROWSER_PROXY']}
    return options
