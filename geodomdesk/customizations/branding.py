"""Brand assets are kept separate from source transformations."""


def assets(root):
    result = {}
    for name in ['flutter/windows/runner/resources/app_icon.ico', 'res/icon.ico',
                 'flutter/assets/icon.ico']:
        result[name] = (root/'assets/geodomdesk.ico').read_bytes()
    for name in ['res/128x128@2x.png', 'flutter/assets/icon.png']:
        result[name] = (root/'assets/geodomdesk.png').read_bytes()
    for name in ['logo.png', 'logo_light.png', 'logo_dark.png']:
        result['flutter/assets/'+name] = (root/'assets/geodom-logo.png').read_bytes()
    return result
