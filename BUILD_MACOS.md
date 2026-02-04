# Building DropLyx for macOS

## Prerequisites

- macOS 10.14 (Mojave) or later
- Python 3.8 or higher
- Homebrew (recommended)

## System Dependencies

Install Python and required libraries:

```bash
# Install Homebrew if not already installed
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Install Python 3
brew install python@3.11

# Install dependencies for pystray (system tray support)
brew install pygobject3 gtk+3
```

## Build Steps

### 1. Clone Repository

```bash
cd ~/Downloads
git clone https://github.com/yourusername/droplyx.git
cd droplyx
```

### 2. Create Virtual Environment

```bash
python3 -m venv venv_macos
source venv_macos/bin/activate
```

### 3. Install Python Dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
pip install pyinstaller
```

### 4. Build Application

```bash
pyinstaller DropLyx_macos.spec --clean --noconfirm
```

### 5. Application Location

The macOS application bundle will be at:
```
dist/DropLyx.app
```

## Running the Application

### Option 1: Double-click
Simply double-click `DropLyx.app` in Finder

### Option 2: Command Line
```bash
open dist/DropLyx.app
```

### Option 3: Move to Applications
```bash
mv dist/DropLyx.app /Applications/
```

Then launch from Applications folder or Spotlight.

## Creating a .icns Icon (Optional)

To create a proper macOS icon:

```bash
# Create iconset directory
mkdir lyx_icon.iconset

# Generate different sizes (requires imagemagick)
brew install imagemagick

# Convert PNG to different sizes
sips -z 16 16     lyx-icon.png --out lyx_icon.iconset/icon_16x16.png
sips -z 32 32     lyx-icon.png --out lyx_icon.iconset/icon_16x16@2x.png
sips -z 32 32     lyx-icon.png --out lyx_icon.iconset/icon_32x32.png
sips -z 64 64     lyx-icon.png --out lyx_icon.iconset/icon_32x32@2x.png
sips -z 128 128   lyx-icon.png --out lyx_icon.iconset/icon_128x128.png
sips -z 256 256   lyx-icon.png --out lyx_icon.iconset/icon_128x128@2x.png
sips -z 256 256   lyx-icon.png --out lyx_icon.iconset/icon_256x256.png
sips -z 512 512   lyx-icon.png --out lyx_icon.iconset/icon_256x256@2x.png
sips -z 512 512   lyx-icon.png --out lyx_icon.iconset/icon_512x512.png
sips -z 1024 1024 lyx-icon.png --out lyx_icon.iconset/icon_512x512@2x.png

# Create .icns file
iconutil -c icns lyx_icon.iconset -o lyx_icon.icns

# Update spec file to use the icon
# Change: icon=None
# To:     icon='lyx_icon.icns'
```

Then rebuild with PyInstaller.

## macOS-Specific Features

### Menu Bar Integration
DropLyx appears in the macOS menu bar (top-right) thanks to pystray.

### LSUIElement Setting
The app runs as a menu bar utility without appearing in the Dock, configured in the spec file:
```python
'LSUIElement': '1'
```

### Retina Display Support
High-resolution displays are supported:
```python
'NSHighResolutionCapable': 'True'
```

## File Detection on macOS

macOS uses different methods than Windows/Linux:

- Uses `psutil` to inspect open file descriptors
- Checks `/proc` equivalent on macOS
- Falls back to `lsof` command if available

The code in `DropLyx.py` has platform-specific sections:
```python
if sys.platform == "darwin":  # macOS
    # macOS-specific file detection
```

## Notarization (For Distribution)

If you want to distribute the app outside the App Store, you need to notarize it:

### 1. Sign the Application

```bash
# Get your Developer ID
security find-identity -v -p codesigning

# Sign the app
codesign --deep --force --verify --verbose --sign "Developer ID Application: Your Name" dist/DropLyx.app
```

### 2. Create DMG (Optional)

```bash
# Install create-dmg
brew install create-dmg

# Create DMG
create-dmg \
  --volname "DropLyx Installer" \
  --window-pos 200 120 \
  --window-size 800 400 \
  --icon-size 100 \
  --icon "DropLyx.app" 200 190 \
  --hide-extension "DropLyx.app" \
  --app-drop-link 600 185 \
  "DropLyx-Installer.dmg" \
  "dist/"
```

### 3. Notarize with Apple

```bash
# Upload for notarization
xcrun notarytool submit DropLyx-Installer.dmg --apple-id "your@email.com" --team-id "TEAMID" --password "app-specific-password" --wait

# Staple the notarization
xcrun stapler staple DropLyx-Installer.dmg
```

## Troubleshooting

### "DropLyx.app is damaged and can't be opened"

This happens with unsigned apps on macOS Catalina+. To bypass:

```bash
xattr -cr /Applications/DropLyx.app
```

Or in System Preferences:
1. Go to Security & Privacy
2. Click "Open Anyway" for DropLyx

### Menu Bar Icon Not Showing

Some macOS versions hide menu bar icons when space is limited. Try:
- Reduce number of menu bar icons
- Install Bartender app to manage icons
- Check pystray compatibility with your macOS version

### Permission Errors

Grant necessary permissions:
1. System Preferences → Security & Privacy
2. Privacy tab
3. Grant "Full Disk Access" to DropLyx (for file monitoring)

### Python Version Issues

If you have multiple Python versions:

```bash
# Use specific Python version
/usr/local/bin/python3.11 -m venv venv_macos
source venv_macos/bin/activate
which python  # Verify correct version
python --version
```

## Creating Universal Binary (Intel + Apple Silicon)

To create a universal binary that works on both Intel and Apple Silicon:

```bash
# Build on Apple Silicon Mac
arch -arm64 pyinstaller DropLyx_macos.spec --clean --noconfirm
mv dist/DropLyx.app dist/DropLyx-arm64.app

# Build for Intel (x86_64)
arch -x86_64 pyinstaller DropLyx_macos.spec --clean --noconfirm
mv dist/DropLyx.app dist/DropLyx-x86_64.app

# Merge into universal binary using lipo
# (More complex - usually done via separate builds and lipo command)
```

Or use GitHub Actions to build both architectures automatically.

## Platform Differences

### Windows vs macOS

| Feature | Windows | macOS |
|---------|---------|-------|
| File Detection | Window titles (pygetwindow) | Open file descriptors |
| System Tray | Windows notification area | Menu bar |
| Icon Format | .ico | .icns |
| Executable | .exe | .app bundle |

### Known Limitations on macOS

- **Sandbox Restrictions**: Newer macOS versions have strict sandboxing
- **Gatekeeper**: Unsigned apps show security warnings
- **File Access**: May need "Full Disk Access" permission for monitoring
- **LyX Path Detection**: Assumes standard LyX installation in `/Applications/LyX.app`

## Distribution Checklist

Before distributing your macOS app:

- [ ] Sign with Apple Developer certificate
- [ ] Notarize with Apple
- [ ] Test on Intel Mac
- [ ] Test on Apple Silicon Mac
- [ ] Test on latest macOS version
- [ ] Create DMG installer
- [ ] Write installation instructions
- [ ] Document permission requirements

## Running from Source (Alternative)

If building fails, users can run from source:

```bash
git clone https://github.com/yourusername/droplyx.git
cd droplyx
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python DropLyx.py
```

## Links

- [PyInstaller macOS Documentation](https://pyinstaller.org/en/stable/usage.html#macos-specific-options)
- [Apple Developer - Notarization](https://developer.apple.com/documentation/security/notarizing_macos_software_before_distribution)
- [pystray macOS Support](https://github.com/moses-palmer/pystray)
