#ifndef RUNNER_GEODOMDESK_WINDOW_ICONS_H_
#define RUNNER_GEODOMDESK_WINDOW_ICONS_H_

#include <windows.h>

#include "resource.h"

// Window icons belong to the window, rather than the first monitor's class icon.
class GeodomDeskWindowIcons {
 public:
  GeodomDeskWindowIcons() = default;
  GeodomDeskWindowIcons(const GeodomDeskWindowIcons&) = delete;
  GeodomDeskWindowIcons& operator=(const GeodomDeskWindowIcons&) = delete;

  ~GeodomDeskWindowIcons() {
    if (big_) DestroyIcon(big_);
    if (small_) DestroyIcon(small_);
  }

  void Update(HWND window, UINT dpi) {
    UpdateIcon(window, dpi, 32, ICON_BIG, big_);
    UpdateIcon(window, dpi, 16, ICON_SMALL, small_);
  }

 private:
  static void UpdateIcon(HWND window, UINT dpi, int logical_size, WPARAM kind,
                         HICON& current) {
    const int size = MulDiv(logical_size, dpi, 96);
    HICON icon = static_cast<HICON>(LoadImageW(
        GetModuleHandleW(nullptr), MAKEINTRESOURCEW(IDI_APP_ICON), IMAGE_ICON,
        size, size, 0));
    if (!icon) return;
    SendMessageW(window, WM_SETICON, kind, reinterpret_cast<LPARAM>(icon));
    if (current) DestroyIcon(current);
    current = icon;
  }

  HICON big_ = nullptr;
  HICON small_ = nullptr;
};

#endif  // RUNNER_GEODOMDESK_WINDOW_ICONS_H_
