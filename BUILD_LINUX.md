# Building DropLyx for Linux

## Prerequisites

- Python 3.8 or higher
- pip package manager
- GTK3 libraries (for pystray)

## System Dependencies

Install GTK3 and development libraries:

```bash
# Ubuntu/Debian
sudo apt update
sudo apt install python3-pip python3-venv libgtk-3-dev libappindicator3-dev

# Fedora
sudo dnf install python3-pip python3-virtualenv gtk3-devel libappindicator-gtk3-devel

# Arch Linux
sudo pacman -S python-pip gtk3 libappindicator-gtk3
```

## Build Steps

1. **Create Virtual Environment**:
   ```bash
   cd /path/to/droplyx
   python3 -m venv venv_linux
   source venv_linux/bin/activate
   ```

2. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   pip install pyinstaller
   ```

3. **Compile Binary**:
   ```bash
   pyinstaller DropLyx_linux.spec --clean --noconfirm
   ```

4. **Binary Location**:
   The compiled binary will be at: `dist/DropLyx`

5. **Make Executable** (if needed):
   ```bash
   chmod +x dist/DropLyx
   ```

## Testing

Run the binary:
```bash
./dist/DropLyx
```

## Known Issues

### PyGetWindow Not Available on Linux
The `pygetwindow` library is Windows-specific. The Linux version uses different methods:
- For Wayland: Uses `wl-clipboard` and process inspection
- For X11: Uses `xdotool` or `wmctrl` for window management

### System Tray Icons
Linux requires:
- GTK3-based systems work natively with pystray
- KDE Plasma may require additional configuration
- Some minimal window managers may not support system tray

### File Detection Methods
Linux version uses:
1. `/proc/[pid]/fd/` for open file descriptors
2. `lsof` command if available
3. Window title parsing via X11/Wayland tools

## Distribution

The compiled binary can be distributed as:
- Standalone executable
- AppImage (recommended)
- .deb package
- Flatpak

### Creating AppImage

```bash
# Install appimagetool
wget https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage
chmod +x appimagetool-x86_64.AppImage

# Create AppDir structure
mkdir -p DropLyx.AppDir/usr/bin
cp dist/DropLyx DropLyx.AppDir/usr/bin/
cp lyx-icon.png DropLyx.AppDir/droplyx.png

# Create desktop file
cat > DropLyx.AppDir/droplyx.desktop << EOF
[Desktop Entry]
Name=DropLyx
Exec=DropLyx
Icon=droplyx
Type=Application
Categories=Utility;
EOF

# Create AppRun
cat > DropLyx.AppDir/AppRun << 'EOF'
#!/bin/bash
SELF=$(readlink -f "$0")
HERE=${SELF%/*}
export PATH="${HERE}/usr/bin:${PATH}"
exec "${HERE}/usr/bin/DropLyx" "$@"
EOF
chmod +x DropLyx.AppDir/AppRun

# Build AppImage
./appimagetool-x86_64.AppImage DropLyx.AppDir DropLyx-x86_64.AppImage
```

## Platform-Specific Notes

### Ubuntu/Debian
Works out of the box with GNOME and Unity desktop environments.

### Fedora
Requires SELinux permissions if enforcing. May need:
```bash
sudo setsebool -P allow_execmod on
```

### Arch Linux
Works with most desktop environments. For Wayland sessions, ensure:
```bash
pacman -S wl-clipboard
```

## Troubleshooting

**Binary won't start**:
- Check dependencies: `ldd dist/DropLyx`
- Run with console output: Change `console=False` to `console=True` in spec file

**System tray icon not showing**:
- Check if your desktop environment supports system tray
- For GNOME: Install `gnome-shell-extension-appindicator`

**File detection issues**:
- Ensure LyX is installed and windows are properly titled
- Check `/proc/[pid]/fd/` permissions

**Permission errors**:
- Ensure the binary has execute permissions
- Check parent directory write permissions for lock files

## Cross-Compilation from Windows

If you want to compile for Linux from Windows, use WSL2:

```bash
# In PowerShell/CMD
wsl --install Ubuntu-22.04

# Then follow the build steps above within WSL
```

Note: WSL networking must be functional for pip to download packages.
